"""Read-only evidence collection for AI-assisted drug interpretation.

This module never writes to S1/S2/Top10/GraphBuild tables or the graph. It joins
three existing, already-audited sources:

1. the frozen Top10 ranking CSV (authoritative rank and screening score),
2. the KG06 EvidencePackage (GraphBuild-scoped targets, pathways, S2 records),
3. the S1 limma table (logFC / P.Value / FDR for target genes).

Evidence classes are assigned here, before any model sees the data:
A = direct project evidence (target assertions, S2 records, S1 DEG overlaps)
B = indirect association (target -> S1 pathway / public pathway overlap)
C = model hypothesis (added later by the validator)
D = missing evidence (deterministic evidence_gaps)
"""
from __future__ import annotations

import copy
import csv
import hashlib
import json
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import kg06
from app import s1_genes
from app.core.config import PROJECT_ROOT, settings
from app.models import AnalysisRun, GraphBuild, Project
from app.s1_genes import _read_rows

TOP10_PATH = PROJECT_ROOT / "experiments" / "final_results" / "results" / "final_top10.csv"
TOP10_MANIFEST_PATH = PROJECT_ROOT / "experiments" / "final_results" / "results" / "final_data_manifest.json"
GRADE_MATRIX_PATH = (
    PROJECT_ROOT / "experiments" / "final_results" / "final05_scientific_audit" / "results" / "evidence_grade_matrix.csv"
)
S1_RESULTS_PATH = s1_genes.RESULTS_PATH

LAYER_DIRECT_PATHS = {"direct_target", "s1_deg_overlap"}
LAYER_INDIRECT_PATHS = {"public_pathway", "s1_enrichment_overlap"}

# Phase F columns that are KG GraphBuild-derived statistics.  They may only
# enter the Top10 AI input when the Phase F manifest was produced for exactly
# the GraphBuild that backs this bundle (Stage 2A-4.1.1).
PHASE_F_KG_STAT_FIELDS = (
    "kg_mapping_status",
    "kg_supported_route_count",
    "kg_unique_target_gene_count",
    "kg_unique_pathway_count",
)
# Phase F columns from independent experiments (external S1 / PRISM /
# stability audit); they do not depend on any GraphBuild.
PHASE_F_INDEPENDENT_FIELDS = (
    "rank",
    "external_s1_median_rank",
    "external_s1_conditions",
    "erpos_mean_sensitivity",
    "erpos_median_sensitivity",
    "n_erpos_lines",
    "erneg_mean_sensitivity",
    "n_erneg_lines",
    "mcf7_sensitivity",
    "stability_grade",
)

TARGET_PREDICATES = {
    "TARGET",
    "INHIBITOR",
    "ACTIVATOR",
    "ANTAGONIST",
    "BINDER",
    "AGONIST",
    "BLOCKER",
    "ENZYME",
    "TRANSPORTER",
    "INHIBITION",
    "ACTIVATION",
}
S2_PREDICATES = {"HAS_S2_CONDITION"}
MAPPING_PREDICATES = {"REFERENCES_DRUG", "HAS_CANDIDATE"}
DEG_PREDICATES = {"HAS_DEG"}
PATHWAY_PREDICATES = {"HAS_ENRICHED_GENE", "HAS_ENRICHED_PATHWAY"}


def _evidence_kind(predicate: str | None, route_types: set[str]) -> str:
    """Classify an evidence record by its predicate, falling back to route type."""
    value = str(predicate or "").upper()
    if value in S2_PREDICATES:
        return "s2"
    if value in MAPPING_PREDICATES:
        return "mapping"
    if value in DEG_PREDICATES:
        return "s1_deg"
    if value in PATHWAY_PREDICATES:
        return "pathway"
    if value in TARGET_PREDICATES:
        return "target"
    if "direct_target" in route_types:
        return "target"
    if "s1_deg_overlap" in route_types:
        return "s1_deg"
    if route_types & LAYER_INDIRECT_PATHS:
        return "pathway"
    return "other"



class EvidenceError(RuntimeError):
    """Evidence is missing, inconsistent, or fails its hash verification."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_frozen_top10() -> tuple[list[dict[str, str]], str]:
    """Load the frozen Top10 ranking and verify it against its manifest hash."""
    if not TOP10_PATH.is_file() or not TOP10_MANIFEST_PATH.is_file():
        raise EvidenceError("冻结 Top10 文件或清单缺失")
    actual = sha256_file(TOP10_PATH)
    manifest = json.loads(TOP10_MANIFEST_PATH.read_text(encoding="utf-8"))
    if manifest.get("status") != "PASS":
        raise EvidenceError("Top10 清单状态不是 PASS")
    rel_native = str(TOP10_PATH.relative_to(PROJECT_ROOT))
    expected = (manifest.get("output_sha256") or {}).get(rel_native) or (manifest.get("output_sha256") or {}).get(
        TOP10_PATH.relative_to(PROJECT_ROOT).as_posix()
    )
    if not expected or actual != expected:
        raise EvidenceError("冻结 Top10 文件 SHA-256 与清单不一致")
    with TOP10_PATH.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if len(rows) != 10:
        raise EvidenceError(f"冻结 Top10 行数异常：{len(rows)}")
    return rows, actual


def find_top10_row(pert_id: str, rows: list[dict[str, str]] | None = None) -> dict[str, str]:
    rows = rows or load_frozen_top10()[0]
    row = next((item for item in rows if item.get("pert_id") == pert_id), None)
    if row is None:
        raise LookupError("该药物不在当前冻结 Top10 中")
    return row


def load_grade_matrix_row(pert_id: str) -> dict[str, str] | None:
    if not GRADE_MATRIX_PATH.is_file():
        return None
    with GRADE_MATRIX_PATH.open(encoding="utf-8-sig", newline="") as stream:
        return next((row for row in csv.DictReader(stream) if row.get("pert_id") == pert_id), None)


def resolve_graph_build(db: Session, project_id: str, requested_id: str | None) -> GraphBuild:
    """Only a fully materialized Neo4j build may back mechanism evidence."""
    if settings.graph_source != "neo4j":
        raise EvidenceError("机制证据需要运行时图谱源为 neo4j")
    if requested_id:
        build = db.scalar(
            select(GraphBuild).where(GraphBuild.project_id == project_id, GraphBuild.graph_build_id == requested_id)
        )
        if build is None:
            raise LookupError("该 GraphBuild 不属于当前项目")
    else:
        build = db.scalar(
            select(GraphBuild)
            .where(
                GraphBuild.project_id == project_id,
                GraphBuild.graph_source == "neo4j",
                GraphBuild.status == "materialized",
            )
            .order_by(GraphBuild.created_at.desc(), GraphBuild.graph_build_id.desc())
            .limit(1)
        )
    if build is None:
        raise EvidenceError("当前项目没有已完成的正式 GraphBuild")
    if build.status != "materialized" or build.graph_source != "neo4j":
        raise EvidenceError("所选 GraphBuild 未完成导入，不能作为机制证据来源")
    return build


def _project_bundle_dir(db: Session, project: Project, build: GraphBuild) -> Path:
    run = db.get(AnalysisRun, build.analysis_run_id)
    relative = run.parameters.get("bundle_dir") if run is not None else None
    if not isinstance(relative, str):
        raise EvidenceError("GraphBuild 缺少 GraphBundle 路径")
    bundle_dir = (PROJECT_ROOT / relative).resolve()
    project_root = (PROJECT_ROOT / "data" / "processed" / "projects" / project.project_id).resolve()
    if not bundle_dir.is_relative_to(project_root):
        raise EvidenceError("GraphBundle 路径越出当前项目")
    return bundle_dir


def _number(value: Any) -> float | None:
    if value in (None, "", "NA", "NaN", "Inf", "-Inf"):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _relation_from_actions(actions: list[str]) -> tuple[str, str]:
    """Never infer direction from a generic TARGET assertion."""
    upper = {str(action).upper() for action in actions}
    inhibition = upper & {"INHIBITOR", "ANTAGONIST", "BLOCKER", "INHIBITION"}
    activation = upper & {"ACTIVATOR", "AGONIST", "ACTIVATION"}
    binding = upper & {"BINDER", "BINDING", "BIND"}
    if inhibition and activation:
        return "association", "conflicting_source_actions"
    if inhibition:
        return "inhibition", "source_reported"
    if activation:
        return "activation", "source_reported"
    if binding:
        return "binding", "source_reported"
    return "association", "no_direction_in_source"


def _gene_symbols(paths: list[dict[str, Any]]) -> dict[str, str]:
    symbols: dict[str, str] = {}
    for path in paths:
        for node in path.get("nodes", []):
            if node.get("type") == "Gene" and node.get("id") and node.get("label"):
                symbols.setdefault(str(node["id"]), str(node["label"]))
        if path.get("target_gene_id"):
            for node in path.get("nodes", []):
                if node.get("type") == "Gene" and node.get("id") == path.get("target_gene_id"):
                    symbols.setdefault(str(path["target_gene_id"]), str(node.get("label") or ""))
    return symbols


def _deg_index(paths: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for path in paths:
        if path.get("route_type") != "s1_deg_overlap":
            continue
        assertions = path.get("disease_expression_assertions") or []
        if assertions and isinstance(assertions[0], dict):
            index[str(path.get("target_gene_id"))] = assertions[0]
    return index


SEMANTIC_CACHE_FIELDS = (
    "project_id",
    "graph_build_id",
    "top10_sha256",
    "s1_sha256",
    "candidate",
    "compound",
    "targets",
    "pathways",
    "s2_conditions",
    "coverage",
    "evidence_gaps",
    "counts",
    "allowed_evidence_ids",
    "allowed_path_ids",
    "path_route_types",
    "evidence_references",
    "evidence_layers",
    "evidence_meta",
    "path_meta",
    "rank_evidence_id",
    "grade_matrix",
    "phase_f",
    "phase_f_provenance",
    "exp006",
)


def _sorted_strings(value: Any) -> list[Any]:
    return sorted(value, key=lambda item: str(item or "")) if isinstance(value, list) else []


def _normalise_target(target: dict[str, Any]) -> dict[str, Any]:
    item = copy.deepcopy(target)
    item["reported_actions"] = _sorted_strings(item.get("reported_actions"))
    item["evidence_ids"] = _sorted_strings(item.get("evidence_ids"))
    item["path_ids"] = _sorted_strings(item.get("path_ids"))
    if isinstance(item.get("s1_deg"), dict):
        item["s1_deg"] = copy.deepcopy(item["s1_deg"])
        item["s1_deg"]["evidence_ids"] = _sorted_strings(item["s1_deg"].get("evidence_ids"))
    return item


def _normalise_pathway(pathway: dict[str, Any]) -> dict[str, Any]:
    item = copy.deepcopy(pathway)
    for field in ("route_types", "path_ids", "evidence_ids"):
        item[field] = _sorted_strings(item.get(field))
    return item


def _normalise_condition(condition: dict[str, Any]) -> dict[str, Any]:
    item = copy.deepcopy(condition)
    item["evidence_ids"] = _sorted_strings(item.get("evidence_ids"))
    return item


def _normalise_reference(reference: dict[str, Any]) -> dict[str, Any]:
    item = copy.deepcopy(reference)
    item["gene_ids"] = _sorted_strings(item.get("gene_ids"))
    target_genes = item.get("target_genes")
    if isinstance(target_genes, list):
        genes = []
        for gene in target_genes:
            if not isinstance(gene, dict):
                continue
            gene_item = copy.deepcopy(gene)
            gene_item["path_ids"] = _sorted_strings(gene_item.get("path_ids"))
            genes.append(gene_item)
        item["target_genes"] = sorted(
            genes,
            key=lambda gene: (str(gene.get("gene_id") or ""), str(gene.get("symbol") or "")),
        )
    return item


def _normalise_evidence_meta(meta: dict[str, Any]) -> dict[str, Any]:
    item = copy.deepcopy(meta)
    for field in ("path_ids", "route_types", "gene_ids", "gene_symbols"):
        item[field] = _sorted_strings(item.get(field))
    target_genes = item.get("target_genes")
    if isinstance(target_genes, list):
        genes = []
        for gene in target_genes:
            if not isinstance(gene, dict):
                continue
            gene_item = copy.deepcopy(gene)
            gene_item["path_ids"] = _sorted_strings(gene_item.get("path_ids"))
            genes.append(gene_item)
        item["target_genes"] = sorted(
            genes,
            key=lambda gene: (str(gene.get("gene_id") or ""), str(gene.get("symbol") or "")),
        )
    return item


def _normalise_path_meta(meta: dict[str, Any]) -> dict[str, Any]:
    item = copy.deepcopy(meta)
    item["evidence_ids"] = _sorted_strings(item.get("evidence_ids"))
    return item


def _semantic_evidence_payload(bundle: dict[str, Any]) -> dict[str, Any]:
    """Return the explanation-relevant bundle with only confirmed unordered lists canonicalised.

    `targets` are canonically ordered before the Prompt already sorts them.
    `evidence_references` are sorted by evidence identity and are not used as an
    ordered source by the Prompt or validator. Nested ID/tag lists are set-like.
    `pathways`, `s2_conditions`, and `evidence_gaps` retain their source order:
    the Prompt truncates the first items and the deterministic result exposes
    gap order, so those lists can carry explanatory meaning.
    """
    payload = {field: copy.deepcopy(bundle.get(field)) for field in SEMANTIC_CACHE_FIELDS}
    if isinstance(payload.get("targets"), list):
        targets = []
        for target in payload["targets"]:
            if isinstance(target, dict):
                targets.append(_normalise_target(target))
        payload["targets"] = sorted(
            targets,
            key=lambda target: (str(target.get("gene_id") or ""), str(target.get("symbol") or "")),
        )
    if isinstance(payload.get("pathways"), list):
        payload["pathways"] = [
            _normalise_pathway(pathway) if isinstance(pathway, dict) else pathway
            for pathway in payload["pathways"]
        ]
    if isinstance(payload.get("s2_conditions"), list):
        payload["s2_conditions"] = [
            _normalise_condition(condition) if isinstance(condition, dict) else condition
            for condition in payload["s2_conditions"]
        ]
    if isinstance(payload.get("allowed_evidence_ids"), list):
        payload["allowed_evidence_ids"] = _sorted_strings(payload["allowed_evidence_ids"])
    if isinstance(payload.get("allowed_path_ids"), list):
        payload["allowed_path_ids"] = _sorted_strings(payload["allowed_path_ids"])
    if isinstance(payload.get("evidence_references"), list):
        references = [
            _normalise_reference(reference) if isinstance(reference, dict) else reference
            for reference in payload["evidence_references"]
        ]
        payload["evidence_references"] = sorted(
            references,
            key=lambda reference: (
                str(reference.get("evidence_id") or ""),
                str(reference.get("layer") or ""),
            ),
        )
    if isinstance(payload.get("evidence_meta"), dict):
        payload["evidence_meta"] = {
            str(key): _normalise_evidence_meta(value) if isinstance(value, dict) else value
            for key, value in payload["evidence_meta"].items()
        }
    if isinstance(payload.get("path_meta"), dict):
        payload["path_meta"] = {
            str(key): _normalise_path_meta(value) if isinstance(value, dict) else value
            for key, value in payload["path_meta"].items()
        }
    return payload


def semantic_evidence_sha256(bundle: dict[str, Any]) -> str:
    """Stable cache identity for all explanation-relevant semantic evidence.

    KG06's raw `package_sha256` and the derived `evidence_input_sha256` are
    deliberately excluded because KG06 includes `generated_at_utc` in the
    package before hashing. They remain available separately for provenance.
    """
    return hashlib.sha256(
        json.dumps(
            _semantic_evidence_payload(bundle),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _phase_f_kg_stats_allowed(
    provenance: dict[str, Any] | None, package_graph_build_id: Any
) -> bool:
    """Fail-closed gate: KG-derived Phase F statistics require a proven,
    hash-validated Phase F manifest for exactly this bundle's GraphBuild.

    Missing provenance, a missing manifest GraphBuild ID, a mismatch, or a
    failed hash validation all keep the KG statistics out of the AI input.
    """
    if not isinstance(provenance, dict):
        return False
    if provenance.get("kg_fields_available") is not True:
        return False
    if provenance.get("graph_build_matches") is not True:
        return False
    manifest_build_id = str(provenance.get("manifest_graph_build_id") or "").strip()
    package_build_id = str(package_graph_build_id or "").strip()
    return bool(manifest_build_id) and manifest_build_id == package_build_id


def assemble_bundle(
    *,
    top10_row: dict[str, str],
    top10_sha256: str,
    package: dict[str, Any],
    s1_rows: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None,
    grade_row: dict[str, str] | None,
    phase_f_row: dict[str, Any] | None,
    exp006_row: dict[str, Any] | None,
    s1_sha256: str = "",
    phase_f_provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pure join of already-audited inputs into a model-ready evidence bundle."""
    paths = list(package.get("paths") or [])
    screening = package.get("screening_evidence") or {}
    symbols = _gene_symbols(paths)
    degs = _deg_index(paths)
    s1_by_symbol = {str(row["gene_symbol"]).upper(): row for row in (s1_rows or [])}

    # --- targets: direct Drug->Gene assertions only -------------------------
    target_paths: dict[str, list[dict[str, Any]]] = {}
    for path in paths:
        if path.get("route_type") == "direct_target" and path.get("target_gene_id"):
            target_paths.setdefault(str(path["target_gene_id"]), []).append(path)
    targets = []
    for gene_id, gene_paths in sorted(target_paths.items(), key=lambda item: symbols.get(item[0], item[0])):
        actions = [action for path in gene_paths for action in (path.get("reported_actions") or [])]
        relation, relation_basis = _relation_from_actions(actions)
        evidence_ids = sorted({eid for path in gene_paths for eid in (path.get("evidence_ids") or [])})
        path_ids = sorted({str(path.get("path_id")) for path in gene_paths if path.get("path_id")})
        symbol = symbols.get(gene_id) or gene_id
        deg = degs.get(gene_id)
        s1_row = s1_by_symbol.get(str(symbol).upper())
        s1 = None
        if deg:
            s1 = {
                "gene_symbol": deg.get("gene_symbol") or symbol,
                "logFC": _number(deg.get("logFC")),
                "p_value": _number((s1_row or {}).get("P.Value")),
                "fdr": _number(deg.get("adj.P.Val")),
                "t": _number(deg.get("t")),
                "direction": str(deg.get("direction") or "").upper() or None,
                "evidence_ids": sorted(
                    {eid for path in paths if path.get("route_type") == "s1_deg_overlap" and path.get("target_gene_id") == gene_id for eid in (path.get("evidence_ids") or [])}
                ),
            }
        targets.append(
            {
                "gene_id": gene_id,
                "symbol": symbol,
                "relation": relation,
                "relation_basis": relation_basis,
                "reported_actions": sorted(set(actions)),
                "evidence_ids": evidence_ids,
                "path_ids": path_ids,
                "s1_deg": s1,
            }
        )

    # --- pathways: target -> pathway intersections ---------------------------
    pathway_map: dict[tuple[str, str], dict[str, Any]] = {}
    for path in paths:
        if path.get("route_type") not in LAYER_INDIRECT_PATHS or not path.get("pathway_id"):
            continue
        key = (str(path["pathway_id"]), str(path.get("target_gene_id") or ""))
        entry = pathway_map.setdefault(
            key,
            {
                "pathway_id": str(path["pathway_id"]),
                "label": path.get("pathway_label") or str(path["pathway_id"]),
                "target_gene_id": path.get("target_gene_id"),
                "target_symbol": symbols.get(str(path.get("target_gene_id"))) or path.get("target_gene_id"),
                "route_types": set(),
                "adjusted_p": [],
                "path_ids": set(),
                "evidence_ids": set(),
            },
        )
        entry["route_types"].add(str(path["route_type"]))
        entry["adjusted_p"].extend(_number(value) for value in (path.get("pathway_adjusted_p_values") or []))
        entry["path_ids"].update(str(pid) for pid in (path.get("path_ids") or [path.get("path_id")]) if pid)
        entry["evidence_ids"].update(str(eid) for eid in (path.get("evidence_ids") or []) if eid)
    pathways = []
    for entry in sorted(pathway_map.values(), key=lambda item: (item["pathway_id"], item["target_gene_id"] or "")):
        adjusted = [value for value in entry["adjusted_p"] if value is not None]
        deg = degs.get(str(entry["target_gene_id"]))
        pathways.append(
            {
                "pathway_id": entry["pathway_id"],
                "label": entry["label"],
                "target_gene_id": entry["target_gene_id"],
                "target_symbol": entry["target_symbol"],
                "route_types": sorted(entry["route_types"]),
                "adjusted_p": min(adjusted) if adjusted else None,
                "s1_direction": str((deg or {}).get("direction") or "").upper() or None,
                "path_ids": sorted(entry["path_ids"]),
                "evidence_ids": sorted(entry["evidence_ids"]),
            }
        )

    # --- S2 conditions -------------------------------------------------------
    conditions = []
    for item in (screening.get("s2_conditions") or []):
        props = item.get("condition") or {}
        conditions.append(
            {
                "condition_key": item.get("condition_key"),
                "cell_id": props.get("cell_id"),
                "pert_type": props.get("pert_type"),
                "dose": props.get("dose"),
                "time": props.get("time"),
                "WTCS_median": _number(props.get("WTCS_median")),
                "n_signatures": props.get("n_signatures"),
                "evidence_ids": [str(eid) for eid in (item.get("evidence_ids") or [])],
                "evidence_complete": item.get("evidence_complete"),
            }
        )

    allowed_evidence = {eid for path in paths for eid in (path.get("evidence_ids") or [])}
    allowed_evidence.update(eid for item in conditions for eid in item["evidence_ids"])
    real_evidence_count = len(allowed_evidence)
    path_route_types = {str(path["path_id"]): str(path.get("route_type")) for path in paths if path.get("path_id")}
    target_relation_by_gene = {str(target["gene_id"]): str(target["relation"]) for target in targets}

    # Path-level metadata used to bind claims to the correct evidence type.
    path_meta: dict[str, dict[str, Any]] = {}
    for path in paths:
        path_id = path.get("path_id")
        if not path_id:
            continue
        gene_id = str(path.get("target_gene_id") or "")
        deg = degs.get(gene_id)
        path_meta[str(path_id)] = {
            "route_type": str(path.get("route_type")),
            "target_gene_id": gene_id or None,
            "target_symbol": symbols.get(gene_id),
            "pathway_id": path.get("pathway_id"),
            "evidence_ids": [str(eid) for eid in (path.get("evidence_ids") or [])],
            "relation": target_relation_by_gene.get(gene_id, "association")
            if path.get("route_type") == "direct_target"
            else "association",
            "s1_direction": str((deg or {}).get("direction") or "").upper() or None,
        }

    references: dict[str, dict[str, Any]] = {}
    for path in paths:
        path_layer = "A" if path.get("route_type") in LAYER_DIRECT_PATHS else "B"
        for edge in path.get("edges") or []:
            for source in edge.get("sources") or []:
                eid = str(source.get("evidence_id") or "")
                if not eid:
                    continue
                existing = references.get(eid)
                if existing is None or (path_layer == "A" and existing["layer"] != "A"):
                    references[eid] = {
                        "evidence_id": eid,
                        "path_id": path.get("path_id"),
                        "source_release": source.get("source_release") or edge.get("source_release"),
                        "source_record_id": source.get("source_record_id") or edge.get("source_record_id"),
                        "predicate": source.get("predicate"),
                        "subject_id": source.get("subject_id"),
                        "object_id": source.get("object_id"),
                        "source_url": source.get("source_url"),
                        "verified_in_postgres": bool(source.get("verified_in_postgres")),
                        "layer": path_layer,
                    }
    for item in conditions:
        for eid in item["evidence_ids"]:
            references.setdefault(
                eid,
                {
                    "evidence_id": eid,
                    "path_id": None,
                    "source_release": "Project S2 LINCS results",
                    "source_record_id": None,
                    "predicate": "HAS_S2_CONDITION",
                    "verified_in_postgres": bool(item.get("evidence_complete")),
                    "layer": "A",
                },
            )

    # A frozen-rank reference lets screening claims cite the authoritative ranking
    # without treating it as a mechanism fact.
    rank_evidence_id = f"TOP10:{top10_row.get('pert_id')}"
    references[rank_evidence_id] = {
        "evidence_id": rank_evidence_id,
        "path_id": None,
        "source_release": "experiments/final_results/results/final_top10.csv",
        "source_record_id": top10_row.get("pert_id"),
        "predicate": "FROZEN_TOP10_RANK",
        "verified_in_postgres": False,
        "verified_in_source": True,
        "layer": "A",
        "rank": int(float(top10_row.get("rank") or 0)),
    }
    allowed_evidence.add(rank_evidence_id)

    reference_list = sorted(references.values(), key=lambda item: (item["layer"], item["evidence_id"]))
    evidence_layers = {ref["evidence_id"]: ref["layer"] for ref in reference_list}

    evidence_meta: dict[str, dict[str, Any]] = {}
    for evidence_id, reference in references.items():
        path_ids = sorted(
            {str(path.get("path_id")) for path in paths if evidence_id in (path.get("evidence_ids") or []) and path.get("path_id")}
        )
        route_types = {path_route_types.get(path_id, "") for path_id in path_ids if path_route_types.get(path_id)}
        kind = _evidence_kind(reference.get("predicate"), route_types)
        s1_direction = None
        for path_id in path_ids:
            candidate_direction = (path_meta.get(path_id) or {}).get("s1_direction")
            if candidate_direction:
                s1_direction = candidate_direction
                break
        # Structured gene entities keep the evidence-to-target binding explicit.
        # Only direct_target paths define a target gene; pathway/S1 paths are
        # additional context and must never be used to invent a drug target.
        target_genes: list[dict[str, Any]] = []
        gene_ids: list[str] = []
        gene_symbols: list[str] = []
        seen_genes: set[str] = set()
        for path_id in path_ids:
            path_entry = path_meta.get(path_id) or {}
            gene_id = str(path_entry.get("target_gene_id") or "")
            if not gene_id:
                continue
            if gene_id not in gene_ids:
                gene_ids.append(gene_id)
            symbol = str(path_entry.get("target_symbol") or "")
            if symbol and symbol not in gene_symbols:
                gene_symbols.append(symbol)
            if path_entry.get("route_type") == "direct_target" and gene_id not in seen_genes:
                seen_genes.add(gene_id)
                target_genes.append(
                    {
                        "gene_id": gene_id,
                        "symbol": symbol or None,
                        "path_ids": [path_id],
                    }
                )
            elif path_entry.get("route_type") == "direct_target":
                for item in target_genes:
                    if item["gene_id"] == gene_id and path_id not in item["path_ids"]:
                        item["path_ids"].append(path_id)
        evidence_meta[evidence_id] = {
            "kind": "rank" if evidence_id == rank_evidence_id else kind,
            "predicate": reference.get("predicate"),
            "path_ids": path_ids,
            "route_types": sorted(route_types),
            "s1_direction": s1_direction,
            "verified_in_postgres": bool(reference.get("verified_in_postgres")),
            "target_genes": target_genes,
            "gene_ids": gene_ids,
            "gene_symbols": gene_symbols,
        }
    # Keep the public evidence reference list aligned with the internal binding
    # metadata, without adding any new scientific assertion.
    for reference in reference_list:
        meta = evidence_meta.get(str(reference.get("evidence_id"))) or {}
        reference["target_genes"] = meta.get("target_genes") or []
        reference["gene_ids"] = meta.get("gene_ids") or []
    evidence_input_sha256 = hashlib.sha256(
        json.dumps(
            {
                "top10_sha256": top10_sha256,
                "s1_sha256": s1_sha256,
                "package_sha256": package.get("package_sha256"),
                "graph_build_id": package.get("graph_build_id"),
                "grade_row": grade_row,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()

    counts = {
        "direct_target_paths": sum(1 for path in paths if path.get("route_type") == "direct_target"),
        "s1_deg_paths": sum(1 for path in paths if path.get("route_type") == "s1_deg_overlap"),
        "public_pathway_paths": sum(1 for path in paths if path.get("route_type") == "public_pathway"),
        "s1_enrichment_paths": sum(1 for path in paths if path.get("route_type") == "s1_enrichment_overlap"),
        "s2_conditions": len(conditions),
        "unique_evidence": real_evidence_count,
        "verified_references": sum(1 for ref in reference_list if ref.get("verified_in_postgres")),
    }

    exp006 = {
        key: (exp006_row or {}).get(key)
        for key in ("rank", "drug_name", "wtcs_median_B", "sig_id", "distil_cc_q75")
        if exp006_row
    } if exp006_row else None
    phase_f = {
        key: (phase_f_row or {}).get(key)
        for key in PHASE_F_INDEPENDENT_FIELDS
        if phase_f_row
    } if phase_f_row else None
    if phase_f is not None and _phase_f_kg_stats_allowed(
        phase_f_provenance, package.get("graph_build_id")
    ):
        phase_f.update(
            {key: phase_f_row.get(key) for key in PHASE_F_KG_STAT_FIELDS}
        )

    return {
        "project_id": package.get("project_id"),
        "graph_build_id": package.get("graph_build_id"),
        "top10_sha256": top10_sha256,
        "candidate": {
            "drug_id": top10_row.get("pert_id"),
            "drug_name": top10_row.get("drug_name"),
            "rank": int(float(top10_row.get("rank") or 0)),
            "score_name": "WTCS median B",
            "score_value": _number(top10_row.get("WTCS_median")),
            "n_signatures": _number(top10_row.get("n_signatures")),
            "n_dose_conditions": _number(top10_row.get("n_24h_dose_conditions")),
            "negative_dose_fraction": _number(top10_row.get("negative_dose_fraction")),
            "primary_dose": top10_row.get("dose"),
            "cell_id": top10_row.get("cell_id"),
            "pert_time": top10_row.get("time"),
        },
        "compound": {
            "mapped": bool((package.get("coverage") or {}).get("compound_mapped")),
            "compound_id": (package.get("candidate") or {}).get("compound_id"),
            "mapping_status": (package.get("candidate") or {}).get("mapping_status"),
        },
        "targets": targets,
        "pathways": pathways,
        "s2_conditions": conditions,
        "coverage": package.get("coverage") or {},
        "evidence_gaps": package.get("evidence_gaps") or [],
        "counts": counts,
        "allowed_evidence_ids": sorted(allowed_evidence),
        "allowed_path_ids": sorted(path_route_types),
        "path_route_types": path_route_types,
        "evidence_references": reference_list,
        "evidence_layers": evidence_layers,
        "evidence_meta": evidence_meta,
        "path_meta": path_meta,
        "rank_evidence_id": rank_evidence_id,
        "s1_sha256": s1_sha256,
        "grade_matrix": grade_row,
        "phase_f": phase_f,
        "phase_f_provenance": phase_f_provenance,
        "exp006": exp006,
        "evidence_input_sha256": evidence_input_sha256,
        "package_sha256": package.get("package_sha256"),
    }


def collect_evidence(db: Session, project_id: str, drug_id: str, graph_build_id: str | None = None) -> dict[str, Any]:
    """Resolve project/build, fetch the KG06 package, and assemble the bundle."""
    project = db.get(Project, project_id)
    if project is None:
        raise LookupError("项目不存在")
    top10_rows, top10_sha = load_frozen_top10()
    top10_row = find_top10_row(drug_id, top10_rows)
    build = resolve_graph_build(db, project_id, graph_build_id)
    bundle_dir = _project_bundle_dir(db, project, build)
    package = kg06.build_evidence_package(
        db, project, build, bundle_dir, drug_id,
        expected_graph_build_id=build.graph_build_id,
    )
    phase_f_row, phase_f_provenance = kg06._read_manifested_phase_f_row(
        drug_id, project, build.graph_build_id
    )
    exp006_row, _ = kg06._load_exp006_row(drug_id, project)
    if not S1_RESULTS_PATH.is_file():
        raise EvidenceError("S1 limma 结果文件缺失，无法生成可追溯解释")
    s1_sha256 = sha256_file(S1_RESULTS_PATH)
    return assemble_bundle(
        top10_row=top10_row,
        top10_sha256=top10_sha,
        package=package,
        s1_rows=_read_rows(),
        grade_row=load_grade_matrix_row(drug_id),
        phase_f_row=phase_f_row,
        exp006_row=exp006_row,
        s1_sha256=s1_sha256,
        phase_f_provenance=phase_f_provenance,
    )
