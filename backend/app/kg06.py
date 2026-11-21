"""KG06: bounded, GraphBuild-scoped retrieval of auditable mechanism evidence."""
from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import AnalysisRun, EvidenceRecord, GraphBuild, Project, S2ConditionResult


TARGET_QUERY = """
MATCH (ctx:DiseaseContext {project_slug: $project_slug})-[hc:HAS_CANDIDATE]->(c:Candidate {pert_id: $pert_id})
      -[cr:REFERENCES_DRUG]->(d:Drug)-[t:TARGETS]->(g:Gene)
WHERE $graph_build_id IN coalesce(hc.graph_build_ids, [])
  AND $graph_build_id IN coalesce(cr.graph_build_ids, [])
  AND $graph_build_id IN coalesce(t.graph_build_ids, [])
RETURN c.id AS candidate_id, c.mapping_status AS mapping_status, c.rank AS candidate_rank,
       d.id AS drug_id, d.label AS drug_label, g.id AS gene_id, g.label AS gene_label,
       [properties(hc), properties(cr), properties(t)] AS edge_properties,
       [type(hc), type(cr), type(t)] AS relation_types
LIMIT 1000
"""

PUBLIC_PATHWAY_QUERY = """
MATCH (ctx:DiseaseContext {project_slug: $project_slug})-[hc:HAS_CANDIDATE]->(c:Candidate {pert_id: $pert_id})
      -[cr:REFERENCES_DRUG]->(d:Drug)-[t:TARGETS]->(g:Gene)
MATCH (p)-[pg:CONTAINS_GENE]->(g)
WHERE (p:Pathway OR p:GeneSet)
  AND $graph_build_id IN coalesce(hc.graph_build_ids, [])
  AND $graph_build_id IN coalesce(cr.graph_build_ids, [])
  AND $graph_build_id IN coalesce(t.graph_build_ids, [])
  AND $graph_build_id IN coalesce(pg.graph_build_ids, [])
RETURN c.id AS candidate_id, c.mapping_status AS mapping_status, d.id AS drug_id, d.label AS drug_label,
       g.id AS gene_id, g.label AS gene_label, p.id AS pathway_id, p.label AS pathway_label,
       [properties(hc), properties(cr), properties(t), properties(pg)] AS edge_properties,
       [type(hc), type(cr), type(t), type(pg)] AS relation_types
LIMIT 2000
"""

S1_ENRICHMENT_QUERY = """
MATCH (ctx:DiseaseContext {project_slug: $project_slug})-[hc:HAS_CANDIDATE]->(c:Candidate {pert_id: $pert_id})
      -[cr:REFERENCES_DRUG]->(d:Drug)-[t:TARGETS]->(g:Gene)
MATCH (p)-[he:HAS_ENRICHED_GENE]->(g)
MATCH (ctx)-[hp:HAS_ENRICHED_PATHWAY]->(p)
WHERE (p:Pathway OR p:GeneSet)
  AND $graph_build_id IN coalesce(hc.graph_build_ids, [])
  AND $graph_build_id IN coalesce(cr.graph_build_ids, [])
  AND $graph_build_id IN coalesce(t.graph_build_ids, [])
  AND $graph_build_id IN coalesce(he.graph_build_ids, [])
  AND $graph_build_id IN coalesce(hp.graph_build_ids, [])
RETURN c.id AS candidate_id, c.mapping_status AS mapping_status, d.id AS drug_id, d.label AS drug_label,
       g.id AS gene_id, g.label AS gene_label, p.id AS pathway_id, p.label AS pathway_label,
       [properties(hc), properties(cr), properties(t), properties(he), properties(hp)] AS edge_properties,
       [type(hc), type(cr), type(t), type(he), type(hp)] AS relation_types
LIMIT 3000
"""

S1_DEG_QUERY = """
MATCH (ctx:DiseaseContext {project_slug: $project_slug})-[hc:HAS_CANDIDATE]->(c:Candidate {pert_id: $pert_id})
      -[cr:REFERENCES_DRUG]->(d:Drug)-[t:TARGETS]->(g:Gene)
MATCH (ctx)-[deg:HAS_DEG]->(g)
WHERE $graph_build_id IN coalesce(hc.graph_build_ids, [])
  AND $graph_build_id IN coalesce(cr.graph_build_ids, [])
  AND $graph_build_id IN coalesce(t.graph_build_ids, [])
  AND $graph_build_id IN coalesce(deg.graph_build_ids, [])
RETURN c.id AS candidate_id, c.mapping_status AS mapping_status, d.id AS drug_id, d.label AS drug_label,
       g.id AS gene_id, g.label AS gene_label,
       [properties(hc), properties(cr), properties(t), properties(deg)] AS edge_properties,
       [type(hc), type(cr), type(t), type(deg)] AS relation_types
LIMIT 1000
"""

CANDIDATE_QUERY = """
MATCH (ctx:DiseaseContext {project_slug: $project_slug})-[hc:HAS_CANDIDATE]->(c:Candidate {pert_id: $pert_id})
WHERE $graph_build_id IN coalesce(hc.graph_build_ids, [])
OPTIONAL MATCH (c)-[cr:REFERENCES_DRUG]->(d:Drug)
WHERE $graph_build_id IN coalesce(cr.graph_build_ids, [])
RETURN c.id AS candidate_id, c.mapping_status AS mapping_status, c.rank AS candidate_rank,
       c.wtcs_median_B AS graph_wtcs_median, c.condition_count AS condition_count,
       c.condition_scores AS graph_condition_scores, c.label AS candidate_label,
       d.id AS drug_id, d.label AS drug_label,
       coalesce(cr.evidence_ids, []) AS mapping_evidence_ids,
       coalesce(cr.source_release, '') AS mapping_source_release
LIMIT 2
"""

S2_CONDITIONS_QUERY = """
MATCH (ctx:DiseaseContext {project_slug: $project_slug})-[hc:HAS_CANDIDATE]->(c:Candidate {pert_id: $pert_id})
      -[condition_edge:HAS_S2_CONDITION]->(condition:S2Condition)
WHERE $graph_build_id IN coalesce(hc.graph_build_ids, [])
  AND $graph_build_id IN coalesce(condition_edge.graph_build_ids, [])
  AND condition.analysis_run_id = $s2_analysis_run_id
RETURN condition.condition_key AS condition_key, properties(condition) AS condition_properties,
       properties(condition_edge) AS edge_properties
ORDER BY condition.condition_key
LIMIT 100
"""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


PHASE_F_KG_FIELDS = (
    "mapping_method",
    "kg_mapping_status",
    "kg_supported_route_count",
    "kg_unique_target_gene_count",
    "kg_unique_pathway_count",
    "kg_interpretation",
)


def _phase_f_graph_build_status(manifest: dict, expected_graph_build_id: str | None) -> tuple[bool, str | None]:
    """Return whether Phase F KG-derived fields may be used for this build."""
    expected = str(expected_graph_build_id).strip() if expected_graph_build_id else ""
    if not expected:
        return True, None
    manifest_build_id = str(manifest.get("graph_build_id") or "").strip()
    if not manifest_build_id:
        return False, "missing_graph_build_id"
    if manifest_build_id != expected:
        return False, "graph_build_mismatch"
    return True, None


def _without_kg_derived_fields(row: dict) -> dict:
    """Keep Phase F fields that are not explicitly KG GraphBuild-derived."""
    return {key: value for key, value in row.items() if not key.startswith("kg_")}


def _phase_f_joined_evidence(phase_f: dict | None, provenance: dict) -> dict:
    if provenance.get("kg_fields_available") is not True:
        return {
            "available": False,
            "reason": provenance.get("reason") or "phase_f_kg_fields_unavailable",
        }
    return {key: (phase_f or {}).get(key) for key in PHASE_F_KG_FIELDS}


def _read_manifested_phase_f_row(
    pert_id: str,
    project: Project,
    expected_graph_build_id: str | None = None,
) -> tuple[dict | None, dict]:
    if project.slug != "erpos-gse42568":
        return None, {"available": False, "reason": "No validated Phase F file is registered for this project."}
    root = Path(__file__).resolve().parents[2]
    manifest_path = root / "experiments" / "phase_f_integrated_evidence" / "phase_f_manifest.json"
    data_path = root / "experiments" / "phase_f_integrated_evidence" / "candidate_evidence_with_kg.csv"
    if not manifest_path.is_file() or not data_path.is_file():
        return None, {"available": False, "reason": "Phase F manifest or candidate evidence file is missing."}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rel = data_path.relative_to(root).as_posix()
    expected = manifest.get("output_sha256", {}).get(rel)
    if manifest.get("status") != "PASS" or manifest.get("project_id") != project.project_id or not expected:
        return None, {"available": False, "reason": "Phase F evidence file failed project or SHA-256 validation."}

    graph_build_ok, graph_build_reason = _phase_f_graph_build_status(manifest, expected_graph_build_id)
    if not graph_build_ok:
        # A different GraphBuild must not leak its KG-derived columns into this
        # package.  Independent experiment columns are retained only after the
        # same manifest, project, and data-hash checks as the normal path.
        if sha256_file(data_path) != expected:
            return None, {
                "available": False,
                "kg_fields_available": False,
                "graph_build_matches": False,
                "reason": "hash_mismatch",
                "manifest_graph_build_id": manifest.get("graph_build_id"),
                "expected_graph_build_id": expected_graph_build_id,
                "source": rel,
            }
        with data_path.open(encoding="utf-8-sig", newline="") as stream:
            row = next((dict(item) for item in csv.DictReader(stream) if item.get("pert_id") == pert_id), None)
        safe_row = _without_kg_derived_fields(row) if row is not None else None
        return safe_row, {
            "available": False,
            "kg_fields_available": False,
            "graph_build_matches": False,
            "reason": graph_build_reason,
            "manifest_graph_build_id": manifest.get("graph_build_id"),
            "expected_graph_build_id": expected_graph_build_id,
            "independent_fields_retained": bool(safe_row),
            "kg_derived_fields_isolated": True,
            "manifest_sha256": sha256_file(manifest_path),
            "data_sha256": expected,
            "source": rel,
        }

    if sha256_file(data_path) != expected:
        return None, {
            "available": False,
            "kg_fields_available": False,
            "graph_build_matches": graph_build_ok,
            "reason": "hash_mismatch",
            "manifest_graph_build_id": manifest.get("graph_build_id"),
            "expected_graph_build_id": expected_graph_build_id,
            "source": rel,
        }
    with data_path.open(encoding="utf-8-sig", newline="") as stream:
        row = next((dict(item) for item in csv.DictReader(stream) if item.get("pert_id") == pert_id), None)
    return row, {
        "available": row is not None,
        "kg_fields_available": row is not None,
        "graph_build_matches": graph_build_ok,
        "manifest_sha256": sha256_file(manifest_path),
        "data_sha256": expected,
        "manifest_graph_build_id": manifest.get("graph_build_id"),
        "expected_graph_build_id": expected_graph_build_id,
        "source": rel,
    }


def _load_exp006_row(pert_id: str, project: Project) -> tuple[dict | None, dict]:
    if project.slug != "erpos-gse42568":
        return None, {"available": False, "reason": "Exp006 Top50 is specific to the ER+ project."}
    root = Path(__file__).resolve().parents[2]
    path = root / "experiments" / "exp006_ranking_reliability" / "results" / "candidate_drugs_top50.csv"
    if not path.is_file():
        return None, {"available": False, "reason": "Exp006 Top50 source file is missing."}
    with path.open(encoding="utf-8-sig", newline="") as stream:
        row = next((dict(item) for item in csv.DictReader(stream) if item.get("pert_id") == pert_id), None)
    return row, {"available": row is not None, "sha256": sha256_file(path), "source": path.relative_to(root).as_posix()}


def _parse_assertion(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {"raw_value": value}
    except (json.JSONDecodeError, TypeError):
        return {"invalid_json": str(raw)}


def _edge_evidence(props: dict, relation_type: str, evidence_by_id: dict[str, EvidenceRecord]) -> dict:
    assertion = _parse_assertion(props.get("source_assertion_json"))
    evidence_ids = list(dict.fromkeys(props.get("evidence_ids") or []))
    sources = []
    for evidence_id in evidence_ids:
        record = evidence_by_id.get(evidence_id)
        if record is None:
            sources.append({"evidence_id": evidence_id, "verified_in_postgres": False})
        else:
            sources.append({
                "evidence_id": evidence_id,
                "verified_in_postgres": True,
                "source_release": record.source_release,
                "source_record_id": record.source_record_id,
                "subject_id": record.subject_id,
                "predicate": record.predicate,
                "object_id": record.object_id,
                "source_url": record.source_url,
                "assertion_sha256": record.assertion_sha256,
            })
    relation_assertions = assertion.get("relation_assertions") or []
    reported_actions = sorted({
        str(item["relation"]).upper()
        for item in relation_assertions
        if isinstance(item, dict) and item.get("relation")
    })
    return {
        "edge_id": props.get("id", ""),
        "relation_type": relation_type,
        "source_id": props.get("from", ""),
        "target_id": props.get("to", ""),
        "source_release": props.get("source_release", ""),
        "source_record_id": assertion.get("source_record_id", ""),
        "evidence_ids": evidence_ids,
        "sources": sources,
        "source_assertion": assertion,
        "reported_actions": reported_actions,
        "action_interpretation": "source_reported_relation_only; no direction inferred from generic TARGET/ENZYME/TRANSPORTER assertions",
        "analysis_assertions": assertion.get("analysis_assertions") or [],
        "relation_conflict": bool(assertion.get("relation_conflict", False)),
        "source_urls": assertion.get("source_urls") or assertion.get("source_url") or [],
        "evidence_complete": bool(evidence_ids) and all(source.get("verified_in_postgres") for source in sources),
    }


def _make_path(row: dict, route_type: str, graph_build_id: str, evidence_by_id: dict[str, EvidenceRecord]) -> dict:
    edges = [_edge_evidence(props, rel, evidence_by_id) for props, rel in zip(row["edge_properties"], row["relation_types"])]
    edge_ids = [edge["edge_id"] for edge in edges]
    path_id = hashlib.sha256(f"{graph_build_id}|{route_type}|{'|'.join(edge_ids)}".encode()).hexdigest()
    context_id = row.get("disease_context_id") or (row["edge_properties"][0].get("from") if row.get("edge_properties") else "")
    nodes = [
        {"id": row.get("candidate_id"), "type": "Candidate", "label": row.get("candidate_id", "")},
        {"id": row.get("drug_id"), "type": "Drug", "label": row.get("drug_label") or row.get("drug_id", "")},
        {"id": row.get("gene_id"), "type": "Gene", "label": row.get("gene_label") or row.get("gene_id", "")},
    ]
    if row.get("pathway_id"):
        nodes.append({"id": row["pathway_id"], "type": "PathwayOrGeneSet", "label": row.get("pathway_label") or row["pathway_id"]})
    nodes.append({"id": context_id, "type": "DiseaseContext", "label": "project disease context"})
    nodes = [node for node in nodes if node.get("id")]
    s1_assertions = [item for edge in edges for item in edge.get("analysis_assertions", []) if isinstance(item, dict)]
    pathway_fdrs = [item.get("adjusted_p") for item in s1_assertions if item.get("adjusted_p") is not None]
    deg_assertions = [
        assertion
        for edge in edges
        if edge["relation_type"] == "HAS_DEG"
        for assertion in (edge["source_assertion"].get("source_assertions") or [edge["source_assertion"]])
        if isinstance(assertion, dict)
    ]
    return {
        "path_id": path_id,
        "route_type": route_type,
        "candidate_id": row.get("candidate_id", ""),
        "drug_id": row.get("drug_id", ""),
        "target_gene_id": row.get("gene_id", ""),
        "pathway_id": row.get("pathway_id"),
        "pathway_label": row.get("pathway_label"),
        "nodes": nodes,
        "edges": edges,
        "relation_types": row["relation_types"],
        "evidence_ids": sorted({eid for edge in edges for eid in edge["evidence_ids"]}),
        "source_releases": sorted({edge["source_release"] for edge in edges if edge["source_release"]}),
        "reported_actions": sorted({action for edge in edges for action in edge["reported_actions"]}),
        "disease_expression_assertions": deg_assertions,
        "pathway_adjusted_p_values": pathway_fdrs,
        "relation_conflict": any(edge["relation_conflict"] for edge in edges),
        "evidence_complete": bool(edges) and all(edge["evidence_complete"] for edge in edges),
    }


def build_evidence_package(
    db: Session,
    project: Project,
    build: GraphBuild,
    bundle_dir: Path,
    pert_id: str,
    expected_graph_build_id: str | None = None,
) -> dict:
    """Build a project-, GraphBuild-, and AnalysisRun-scoped EvidencePackage."""
    if not pert_id.strip() or len(pert_id) > 100:
        raise ValueError("invalid perturbagen ID")
    if build.project_id != project.project_id or build.status != "materialized":
        raise ValueError("GraphBuild is not a materialized build in this project")
    if build.graph_source != "neo4j":
        raise ValueError("GraphBuild is not a Neo4j build")
    expected = (
        str(expected_graph_build_id).strip()
        if expected_graph_build_id
        else str(build.graph_build_id).strip()
    )
    if expected and build.graph_build_id != expected:
        raise ValueError(
            "GraphBuild does not match the expected GraphBuild: "
            f"build={build.graph_build_id or '<missing>'} expected={expected}"
        )
    bundle_run = db.get(AnalysisRun, build.analysis_run_id)
    if bundle_run is None or bundle_run.project_id != project.project_id or bundle_run.run_type != "kg_graph_bundle" or bundle_run.status != "completed":
        raise ValueError("GraphBuild has no completed project GraphBundle run")
    params = bundle_run.parameters
    s1_run = db.get(AnalysisRun, params.get("s1_analysis_run_id"))
    s2_run = db.get(AnalysisRun, params.get("s2_analysis_run_id"))
    enrichment_run = db.get(AnalysisRun, params.get("enrichment_analysis_run_id"))
    if any(run is None or run.project_id != project.project_id or run.status != "completed" for run in [s1_run, s2_run, enrichment_run]):
        raise ValueError("EvidencePackage source AnalysisRuns are missing, incomplete, or belong to another project")
    if not bundle_dir.is_relative_to((Path(__file__).resolve().parents[2] / "data" / "processed" / "projects" / project.project_id).resolve()):
        raise ValueError("GraphBundle directory is outside the selected project")
    manifest_path = bundle_dir / "bundle_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "validated" or manifest.get("analysis_run_id") != bundle_run.analysis_run_id:
        raise ValueError("GraphBundle manifest is not validated for this AnalysisRun")
    for filename, expected in manifest.get("output_hashes", {}).items():
        path = (bundle_dir / filename).resolve()
        if not path.is_relative_to(bundle_dir.resolve()) or not path.is_file() or sha256_file(path) != expected:
            raise ValueError(f"GraphBundle output hash verification failed: {filename}")

    conditions = list(db.scalars(select(S2ConditionResult).where(
        S2ConditionResult.project_id == project.project_id,
        S2ConditionResult.analysis_run_id == s2_run.analysis_run_id,
        S2ConditionResult.pert_id == pert_id,
    ).order_by(S2ConditionResult.condition_key)))

    phase_f, phase_f_provenance = _read_manifested_phase_f_row(
        pert_id, project, build.graph_build_id
    )
    exp006, exp006_provenance = _load_exp006_row(pert_id, project)
    driver = GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password))
    params = {"project_slug": project.slug, "pert_id": pert_id, "graph_build_id": build.graph_build_id}
    try:
        with driver.session(database=settings.neo4j_database) as session:
            candidate_rows = session.run(CANDIDATE_QUERY, **params).data()
            graph_conditions = session.run(
                S2_CONDITIONS_QUERY,
                **params,
                s2_analysis_run_id=s2_run.analysis_run_id,
            ).data()
            rows = {
                "direct_target": session.run(TARGET_QUERY, **params).data(),
                "public_pathway": session.run(PUBLIC_PATHWAY_QUERY, **params).data(),
                "s1_enrichment_overlap": session.run(S1_ENRICHMENT_QUERY, **params).data(),
                "s1_deg_overlap": session.run(S1_DEG_QUERY, **params).data(),
            }
    finally:
        driver.close()
    if not candidate_rows:
        raise LookupError("candidate is not connected to this project and GraphBuild in Neo4j")
    graph_candidate = candidate_rows[0]
    evidence_ids = {
        evidence_id
        for route_rows in rows.values()
        for row in route_rows
        for edge in row["edge_properties"]
        for evidence_id in edge.get("evidence_ids", [])
    }
    evidence_ids.update(
        evidence_id
        for row in graph_conditions
        for evidence_id in (row.get("edge_properties", {}).get("evidence_ids") or [])
    )
    evidence_by_id = {
        record.evidence_id: record
        for record in db.scalars(select(EvidenceRecord).where(
            EvidenceRecord.project_id == project.project_id,
            EvidenceRecord.graph_build_id == build.graph_build_id,
            EvidenceRecord.evidence_id.in_(evidence_ids or {"__no_evidence__"}),
        ))
    }
    paths = [
        _make_path(row, route_type, build.graph_build_id, evidence_by_id)
        for route_type, route_rows in rows.items()
        for row in route_rows
    ]
    paths.sort(key=lambda row: (row["route_type"], row.get("pathway_id") or "", row["target_gene_id"], row["path_id"]))
    unique_paths = {path["path_id"]: path for path in paths}
    paths = list(unique_paths.values())
    all_path_evidence = {eid for path in paths for eid in path["evidence_ids"]}

    gaps = []
    if not graph_candidate.get("drug_id"):
        gaps.append("perturbagen has no exact compound mapping in this GraphBuild")
    if not rows["direct_target"]:
        gaps.append("no curated Drug-to-Gene target assertion was found")
    if not rows["public_pathway"]:
        gaps.append("no target-to-public-pathway route was found")
    if not rows["s1_enrichment_overlap"]:
        gaps.append("no target-to-S1-enriched-pathway overlap was found")
    if not rows["s1_deg_overlap"]:
        gaps.append("no target-to-S1-DEG overlap was found")
    if any(not path["evidence_complete"] for path in paths):
        gaps.append("one or more path evidence references are absent from this GraphBuild's PostgreSQL EvidenceRecord store")
    if any(not source.get("source_url") for path in paths for edge in path["edges"] for source in edge["sources"] if source.get("verified_in_postgres")):
        gaps.append("some source records have no source URL in the upstream assertion; source_record_id and source_release are retained")
    if phase_f_provenance.get("kg_derived_fields_isolated"):
        gaps.append(
            "Phase F KG-derived fields were isolated because the Phase F manifest GraphBuild "
            f"({phase_f_provenance.get('manifest_graph_build_id') or '<missing>'}) does not match "
            f"this package GraphBuild ({build.graph_build_id}); independent experiment fields were retained."
        )

    phase_f_by_pert = phase_f or {}
    persisted_condition_by_key = {row.condition_key: row.condition_json for row in conditions}
    s2_condition_evidence = []
    for row in graph_conditions:
        edge = _edge_evidence(row["edge_properties"], "HAS_S2_CONDITION", evidence_by_id)
        s2_condition_evidence.append({
            "condition_key": row["condition_key"],
            "condition": persisted_condition_by_key.get(row["condition_key"], row["condition_properties"]),
            "graph_condition_properties": row["condition_properties"],
            "evidence_ids": edge["evidence_ids"],
            "sources": edge["sources"],
            "evidence_complete": edge["evidence_complete"],
        })
    screening = {
        "s2_analysis_run_id": s2_run.analysis_run_id,
        "s2_candidate": {
            "candidate_id": graph_candidate.get("candidate_id"),
            "rank": exp006.get("rank") if exp006 else graph_candidate.get("candidate_rank"),
            "score_name": "WTCS",
            "score_value": exp006.get("wtcs_median_B") if exp006 else graph_candidate.get("graph_wtcs_median"),
            "condition_count": len(conditions),
            "conditions_source": "S2ConditionResult for the GraphBundle's exact S2 AnalysisRun",
        },
        "s2_conditions": s2_condition_evidence,
        "exp006": exp006 or {"available": False},
        "independent_validation_and_prism": {
            key: phase_f_by_pert.get(key)
            for key in [
                "external_s1_median_rank", "external_s1_B200_median_rank", "external_s1_conditions",
                "Q2_condition_median_rank", "Q2_conditions", "erpos_mean_sensitivity",
                "erpos_median_sensitivity", "n_erpos_lines", "erneg_mean_sensitivity",
                "n_erneg_lines", "mcf7_sensitivity", "n_mcf7_conditions", "sensitivity_rank_erpos",
                "sensitivity_rank_mcf7", "PRISM_endpoint", "WTCS_PRISM_agreement_interpretation",
            ]
        } if phase_f else {"available": False},
        "phase_f_joined_evidence": _phase_f_joined_evidence(
            phase_f, phase_f_provenance
        ),
    }
    evidence_input = {
        "project_id": project.project_id,
        "graph_build_id": build.graph_build_id,
        "graph_bundle_run_id": bundle_run.analysis_run_id,
        "source_analysis_runs": {"s1": s1_run.analysis_run_id, "s1_enrichment": enrichment_run.analysis_run_id, "s2": s2_run.analysis_run_id},
        "pert_id": pert_id,
        "path_ids": [path["path_id"] for path in paths],
        "evidence_ids": sorted(all_path_evidence),
        "bundle_manifest_sha256": sha256_file(manifest_path),
        "phase_f": phase_f_provenance,
        "exp006": exp006_provenance,
    }
    evidence_input_sha256 = hashlib.sha256(json.dumps(evidence_input, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    package = {
        "package_version": "kg06-evidence-package-1.0",
        "package_id": f"KG06:{project.project_id}:{build.graph_build_id.rsplit(':', 1)[-1]}:{pert_id}:{evidence_input_sha256[:16]}",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "project_id": project.project_id,
        "project_slug": project.slug,
        "graph_build_id": build.graph_build_id,
        "analysis_run_ids": evidence_input["source_analysis_runs"],
        "pert_id": pert_id,
        "candidate": {
            "candidate_id": graph_candidate.get("candidate_id") or f"CANDIDATE:{project.project_id}:{pert_id}",
            "drug_name": (exp006 or {}).get("drug_name") or graph_candidate.get("candidate_label") or graph_candidate.get("drug_label") or pert_id,
            "mapping_status": graph_candidate.get("mapping_status") or "unknown",
            "compound_id": graph_candidate.get("drug_id"),
            "rank": (exp006 or {}).get("rank") or graph_candidate.get("candidate_rank"),
        },
        "screening_evidence": screening,
        "paths": paths,
        "evidence_gaps": gaps,
        "coverage": {
            "candidate_present": True,
            "compound_mapped": bool(graph_candidate.get("drug_id")),
            "direct_target_path_count": len(rows["direct_target"]),
            "public_pathway_path_count": len(rows["public_pathway"]),
            "s1_enrichment_overlap_path_count": len(rows["s1_enrichment_overlap"]),
            "s1_deg_overlap_path_count": len(rows["s1_deg_overlap"]),
            "unique_evidence_count": len(all_path_evidence),
            "verified_evidence_count": sum(evidence_id in evidence_by_id for evidence_id in all_path_evidence),
            "s2_condition_evidence_count": len({eid for row in s2_condition_evidence for eid in row["evidence_ids"]}),
            "verified_s2_condition_evidence_count": sum(
                source.get("verified_in_postgres")
                for row in s2_condition_evidence
                for source in row["sources"]
            ),
            "s2_condition_count": len(s2_condition_evidence),
            "evidence_integrity_pass": all(path["evidence_complete"] for path in paths),
            "conflicted_path_count": sum(path["relation_conflict"] for path in paths),
        },
        "provenance": {
            **evidence_input,
            "evidence_input_sha256": evidence_input_sha256,
            "graph_bundle_source_sha256": manifest.get("input_hashes", {}),
            "package_source_policy": "all paths are fixed-template, GraphBuild-scoped Cypher results; no free-form Cypher is accepted",
        },
    }
    package["package_sha256"] = hashlib.sha256(json.dumps(package, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return package


def kg06_coverage_rows(coverage_csv: Path) -> list[dict[str, str]]:
    with coverage_csv.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))
