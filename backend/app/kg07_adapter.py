"""Deterministic KG07 evidence adapter.

Stage 1 intentionally does not call the DeepSeek provider, the Top10 validator,
a database, or Neo4j.  It converts a real KG06 evidence package into:

* five deterministic evidence dimensions for the KG07 page;
* a complete validator-shaped bundle for later server-side validation;
* a bounded, deterministically ordered LLM input (never the full/raw KG06 dump);
* the legacy six-section shape used by the existing KG07 API/UI.

The adapter must not invent target symbols, pathway counts, evidence IDs, or
clinical claims.  Missing fields are reported in ``missing_fields`` instead.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
import hashlib
import json
import re
from typing import Any

ADAPTER_VERSION = "kg07-adapter-1.1"
REPRESENTATIVE_PATHWAY_LIMIT = 12
ACTION_CLASS_ORDER = ("TARGET", "ENZYME", "TRANSPORTER", "UNKNOWN")
S2_CONDITION_PREDICATE = "HAS_S2_CONDITION"
DIMENSION_ORDER = (
    "signature_coverage",
    "dose_consistency",
    "target_coverage",
    "s1_pathway_association",
    "independent_validation",
)
LEGACY_SECTION_KEYS = (
    "screening_basis",
    "known_targets",
    "associated_pathways",
    "independent_validation",
    "mechanism_hypotheses",
    "insufficient_evidence",
)


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _dedupe(values: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = _text(value)
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _first(source: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in source and source.get(key) not in (None, ""):
            return source.get(key)
    return None


def _first_from(
    sources: Sequence[Mapping[str, Any]], *keys: str
) -> tuple[Any, str | None]:
    for source in sources:
        value = _first(source, *keys)
        if value is not None:
            return value, source.get("_source") if isinstance(source.get("_source"), str) else None
    return None, None


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _action_token(value: Any) -> str:
    token = re.sub(r"[\s_-]+", "", _text(value)).upper()
    aliases = {
        "TARGETS": "TARGET",
        "TARGET": "TARGET",
        "ENZYMES": "ENZYME",
        "ENZYME": "ENZYME",
        "TRANSPORTERS": "TRANSPORTER",
        "TRANSPORTER": "TRANSPORTER",
    }
    return aliases.get(token, token)


def _path_actions(path: Mapping[str, Any]) -> list[str]:
    values: list[Any] = []
    raw = path.get("reported_actions")
    values.extend(_as_list(raw))
    for edge in _as_list(path.get("edges")):
        if isinstance(edge, Mapping):
            values.extend(_as_list(edge.get("reported_actions")))
            for assertion in _as_list(edge.get("analysis_assertions")):
                if isinstance(assertion, Mapping):
                    values.extend(_as_list(assertion.get("reported_actions")))
    actions: list[str] = []
    for value in values:
        if isinstance(value, Mapping):
            value = _first(value, "action", "action_type", "reported_action")
        token = _action_token(value)
        if token and token not in actions:
            actions.append(token)
    return actions


def _path_action_class(path: Mapping[str, Any]) -> str:
    classes = {
        "TARGET": bool({"TARGET"} & set(_path_actions(path))),
        "ENZYME": bool({"ENZYME"} & set(_path_actions(path))),
        "TRANSPORTER": bool({"TRANSPORTER"} & set(_path_actions(path))),
    }
    for name in ACTION_CLASS_ORDER:
        if classes.get(name):
            return name
    return "UNKNOWN"


def _path_relation(path: Mapping[str, Any]) -> str:
    actions = set(_path_actions(path))
    if actions & {"INHIBITOR", "INHIBITION", "ANTAGONIST", "BLOCKER"}:
        return "inhibition"
    if actions & {"ACTIVATOR", "AGONIST", "STIMULATOR"}:
        return "activation"
    if actions & {"BINDING", "BINDER"}:
        return "binding"
    return "association"


def _path_gene_id(path: Mapping[str, Any]) -> str:
    gene_id = _text(_first(path, "target_gene_id", "gene_id", "target_id"))
    if gene_id:
        return gene_id
    for node in _as_list(path.get("nodes")):
        if isinstance(node, Mapping) and _text(node.get("type")) == "Gene":
            candidate = _text(node.get("id"))
            if candidate:
                return candidate
    return ""


def _path_gene_symbol(path: Mapping[str, Any], gene_id: str) -> str:
    explicit = _text(
        _first(path, "target_symbol", "gene_symbol", "target_gene_symbol", "symbol")
    )
    if explicit:
        return explicit
    for node in _as_list(path.get("nodes")):
        if not isinstance(node, Mapping):
            continue
        if _text(node.get("type")) != "Gene":
            continue
        node_id = _text(node.get("id"))
        if node_id and node_id != gene_id:
            continue
        label = _text(_first(node, "label", "name", "symbol"))
        if label:
            return label
    return gene_id


def _path_evidence_ids(path: Mapping[str, Any]) -> list[str]:
    values: list[Any] = list(_as_list(path.get("evidence_ids")))
    for edge in _as_list(path.get("edges")):
        if isinstance(edge, Mapping):
            values.extend(_as_list(edge.get("evidence_ids")))
    return _dedupe(values)


def _path_adjusted_p(path: Mapping[str, Any]) -> float | None:
    value = path.get("pathway_adjusted_p_values")
    candidates: list[float] = []
    if isinstance(value, Mapping):
        candidates.extend(
            _float_or_none(item) for item in value.values() if _float_or_none(item) is not None
        )
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            if isinstance(item, Mapping):
                item = _first(item, "adjusted_p_value", "adjusted_p", "p_adjusted", "padj")
            number = _float_or_none(item)
            if number is not None:
                candidates.append(number)
    else:
        number = _float_or_none(value)
        if number is not None:
            candidates.append(number)
    return min(candidates) if candidates else None


def _route_priority(route_type: str) -> int:
    return {
        "s1_enrichment_overlap": 0,
        "public_pathway": 1,
        "s1_deg_overlap": 2,
        "direct_target": 3,
    }.get(route_type, 9)


def _dose_number(value: Any) -> float | None:
    match = re.search(r"[-+]?\d+(?:\.\d+)?", _text(value))
    return _float_or_none(match.group(0)) if match else None


def _normalise_condition(item: Mapping[str, Any], index: int) -> dict[str, Any]:
    condition = _as_mapping(item.get("condition"))
    fields = condition or item
    dose = _text(_first(fields, "dose", "dose_value"))
    wtcs = _float_or_none(_first(fields, "WTCS_median", "wtcs_median", "score_value"))
    n_signatures = _int_or_none(_first(fields, "n_signatures", "signature_count"))
    return {
        "condition_id": _text(_first(fields, "id"))
        or _text(_first(item, "condition_id"))
        or _text(_first(_as_mapping(item.get("graph_condition_properties")), "id")),
        "condition_key": _text(
            _first(item, "condition_key", "key")
            or f"{_text(_first(fields, 'cell_id'))}|{_text(_first(fields, 'pert_type'))}|{dose}|{_text(_first(fields, 'time'))}"
        ),
        "cell_id": _text(_first(fields, "cell_id", "cell")),
        "pert_type": _text(_first(fields, "pert_type", "perturbation_type")),
        "dose": dose,
        "dose_number": _dose_number(dose),
        "time": _text(_first(fields, "time", "pert_time")),
        "n_signatures": n_signatures,
        "WTCS_median": wtcs,
        "WTCS_mean": _float_or_none(_first(fields, "WTCS_mean", "wtcs_mean")),
        "WTCS_sd": _float_or_none(_first(fields, "WTCS_sd", "wtcs_sd")),
        "WTCS_mad": _float_or_none(_first(fields, "WTCS_mad", "wtcs_mad")),
        "quality_cc_median": _float_or_none(_first(fields, "quality_cc_median", "cc_median")),
        "TAS_median": _float_or_none(_first(fields, "TAS_median", "tas_median")),
        "replicates_median": _float_or_none(_first(fields, "replicates_median")),
        "rank_within_condition": _float_or_none(_first(fields, "rank_within_condition")),
        "direction": (
            "negative" if wtcs is not None and wtcs < 0
            else "positive" if wtcs is not None and wtcs > 0
            else "zero" if wtcs == 0
            else "missing"
        ),
        "evidence_ids": _dedupe(_as_list(item.get("evidence_ids"))),
        "sources": list(_as_list(item.get("sources"))),
        "evidence_complete": item.get("evidence_complete"),
    }


def _direction_from_assertions(assertions: Iterable[Any]) -> str | None:
    """Return an explicit source direction, never inferred from logFC sign."""
    values: list[str] = []
    for assertion in assertions:
        if not isinstance(assertion, Mapping):
            continue
        direction = _text(assertion.get("direction")).upper()
        if direction in {"UP", "DOWN"} and direction not in values:
            values.append(direction)
    return values[0] if len(values) == 1 else None


def _path_s1_direction(path: Mapping[str, Any]) -> str | None:
    """Read S1 direction only from the real path assertions."""
    assertions: list[Any] = list(_as_list(path.get("disease_expression_assertions")))
    for edge in _as_list(path.get("edges")):
        if not isinstance(edge, Mapping):
            continue
        relation = _text(edge.get("relation_type")).upper()
        if relation in {"HAS_DEG", "DEG", "DIFFERENTIAL_EXPRESSION"}:
            assertions.extend(_as_list(edge.get("analysis_assertions")))
    return _direction_from_assertions(assertions)


def _s1_deg_values(path: Mapping[str, Any]) -> dict[str, Any]:
    available = False
    values: dict[str, Any] = {}
    assertions: list[Any] = list(_as_list(path.get("disease_expression_assertions")))
    for edge in _as_list(path.get("edges")):
        if not isinstance(edge, Mapping):
            continue
        relation = _text(edge.get("relation_type")).upper()
        if relation not in {"HAS_DEG", "DEG", "DIFFERENTIAL_EXPRESSION"}:
            continue
        assertions.extend(_as_list(edge.get("analysis_assertions")))
    for assertion in assertions:
        if not isinstance(assertion, Mapping):
            continue
        wanted = {
            key: assertion.get(key)
            for key in ("logFC", "log2FC", "P.Value", "p_value", "adj.P.Val", "fdr", "direction")
            if assertion.get(key) not in (None, "")
        }
        if wanted:
            values.update(wanted)
            available = True
    direction = _path_s1_direction(path)
    if direction:
        values["direction"] = direction
    return {"available": available, **values}


def _target_dimension(
    paths: Sequence[Mapping[str, Any]],
    package: Mapping[str, Any],
    missing_fields: list[str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    symbol_map = _as_mapping(package.get("gene_symbols"))
    direct_paths = [p for p in paths if _text(p.get("route_type")) == "direct_target"]
    groups: dict[str, dict[str, Any]] = {}
    annotations: list[dict[str, Any]] = []

    for path in direct_paths:
        gene_id = _path_gene_id(path)
        if not gene_id:
            missing_fields.append("direct_target_path_without_target_gene_id")
            continue
        symbol = _path_gene_symbol(path, gene_id)
        if not symbol or symbol == gene_id:
            mapped = _text(symbol_map.get(gene_id))
            if mapped:
                symbol = mapped
            else:
                missing_fields.append(f"target_symbol_missing:{gene_id}")
        elif symbol_map.get(gene_id) and symbol != _text(symbol_map.get(gene_id)):
            # Keep the evidence-package symbol; do not silently replace it.
            missing_fields.append(f"target_symbol_conflict:{gene_id}")

        path_id = _text(path.get("path_id"))
        actions = _path_actions(path)
        action_class = _path_action_class(path)
        relation = _path_relation(path)
        evidence_ids = _path_evidence_ids(path)
        row = {
            "path_id": path_id,
            "gene_id": gene_id,
            "symbol": symbol,
            "reported_actions": actions,
            "primary_action": action_class,
            "relation": relation,
            "evidence_ids": evidence_ids,
        }
        annotations.append(row)
        group = groups.setdefault(
            gene_id,
            {
                "gene_id": gene_id,
                "symbol": symbol,
                "path_ids": [],
                "evidence_ids": [],
                "reported_actions": [],
                "relations": [],
            },
        )
        group["path_ids"].append(path_id)
        group["evidence_ids"].extend(evidence_ids)
        group["reported_actions"].extend(actions)
        group["relations"].append(relation)

    s1_deg_paths: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for path in paths:
        if _text(path.get("route_type")) == "s1_deg_overlap":
            gene_id = _path_gene_id(path)
            if gene_id:
                s1_deg_paths[gene_id].append(path)

    targets: list[dict[str, Any]] = []
    for gene_id, group in groups.items():
        deg_paths = s1_deg_paths.get(gene_id, [])
        deg_evidence: list[str] = []
        deg_path_ids: list[str] = []
        deg_values: dict[str, Any] = {"available": False}
        for path in deg_paths:
            deg_path_ids.append(_text(path.get("path_id")))
            deg_evidence.extend(_path_evidence_ids(path))
            current = _s1_deg_values(path)
            if current.get("available"):
                deg_values = current
        if deg_paths and not deg_values.get("available"):
            deg_values = {"available": False, "path_ids": sorted(deg_path_ids)}
        targets.append(
            {
                "gene_id": gene_id,
                "symbol": group["symbol"],
                "reported_actions": _dedupe(group["reported_actions"]),
                "relation": (
                    "inhibition" if "inhibition" in group["relations"]
                    else "activation" if "activation" in group["relations"]
                    else "binding" if "binding" in group["relations"]
                    else "association"
                ),
                "path_ids": _dedupe(group["path_ids"]),
                "evidence_ids": _dedupe(group["evidence_ids"]),
                "s1_deg": {
                    **deg_values,
                    "evidence_ids": _dedupe(deg_evidence),
                    "path_ids": _dedupe(deg_path_ids),
                } if deg_paths else None,
            }
        )

    targets.sort(key=lambda row: (row["symbol"] or row["gene_id"], row["gene_id"]))
    annotations.sort(key=lambda row: (row["symbol"] or row["gene_id"], row["path_id"]))

    primary_counts = Counter(row["primary_action"] for row in annotations)
    any_counts: Counter[str] = Counter()
    for row in annotations:
        classes = {name for name in ACTION_CLASS_ORDER if name in row["reported_actions"]}
        if not classes:
            classes = {"UNKNOWN"}
        any_counts.update(classes)

    dimension = {
        "key": "target_coverage",
        "graph_build_id": _text(package.get("graph_build_id")),
        "status": "complete" if annotations else "missing",
        "metrics": {
            "annotation_count": len(annotations),
            "unique_target_gene_count": len(groups),
            "annotation_count_equals_unique_gene_count": len(annotations) == len(groups),
            "primary_action_counts": {
                name: int(primary_counts.get(name, 0)) for name in ACTION_CLASS_ORDER
            },
            "any_action_counts": {
                name: int(any_counts.get(name, 0)) for name in ACTION_CLASS_ORDER
            },
            "action_class_order": list(ACTION_CLASS_ORDER),
            "target_annotation_definition": "每一条 direct_target path 是一条药物-基因注释；注释数不等于独立靶点数",
        },
        "annotations": annotations,
        "targets": targets,
        "caveat": "ABL1/BCR 等符号只来自 KG06 path node 或真实图谱 symbol 映射；缺失时必须标记，不能补造",
    }
    return dimension, targets


def _pathway_dimension(
    paths: Sequence[Mapping[str, Any]],
    missing_fields: list[str],
    *,
    path_limit: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    groups: dict[str, dict[str, Any]] = {}
    route_counts = Counter(_text(p.get("route_type")) for p in paths)
    best_by_pathway: dict[str, tuple[tuple[Any, ...], Mapping[str, Any]]] = {}

    for path in paths:
        pathway_id = _text(_first(path, "pathway_id", "pathway"))
        if not pathway_id:
            if _text(path.get("route_type")) == "s1_enrichment_overlap":
                missing_fields.append("s1_enrichment_path_without_pathway_id")
            continue
        route = _text(path.get("route_type"))
        group = groups.setdefault(
            pathway_id,
            {
                "pathway_id": pathway_id,
                "pathway_label": _text(_first(path, "pathway_label", "pathway_name")) or pathway_id,
                "path_ids": [],
                "evidence_ids": [],
                "route_types": [],
                "target_gene_ids": [],
                "_adjusted_p_values": [],
            },
        )
        group["path_ids"].append(_text(path.get("path_id")))
        group["evidence_ids"].extend(_path_evidence_ids(path))
        group["route_types"].append(route)
        gene_id = _path_gene_id(path)
        if gene_id:
            group["target_gene_ids"].append(gene_id)
        adjusted = _path_adjusted_p(path)
        if adjusted is not None:
            group["_adjusted_p_values"].append(adjusted)

        if route in {"s1_enrichment_overlap", "public_pathway"}:
            key = (
                _route_priority(route),
                adjusted if adjusted is not None else float("inf"),
                _text(path.get("path_id")),
            )
            previous = best_by_pathway.get(pathway_id)
            if previous is None or key < previous[0]:
                best_by_pathway[pathway_id] = (key, path)

    pathways: list[dict[str, Any]] = []
    for pathway_id, group in groups.items():
        adjusted_values = group.pop("_adjusted_p_values")
        pathways.append(
            {
                "pathway_id": pathway_id,
                "pathway_label": group["pathway_label"],
                "path_ids": _dedupe(group["path_ids"]),
                "evidence_ids": _dedupe(group["evidence_ids"]),
                "route_types": _dedupe(group["route_types"]),
                "target_gene_ids": _dedupe(group["target_gene_ids"]),
                "adjusted_p": min(adjusted_values) if adjusted_values else None,
            }
        )
    pathways.sort(key=lambda row: (row["pathway_id"], row["pathway_label"]))
    pathway_ids = [row["pathway_id"] for row in pathways]

    ordered_representatives = sorted(
        best_by_pathway.items(),
        key=lambda item: (
            item[1][0][0],
            item[1][0][1],
            item[0],
            item[1][0][2],
        ),
    )
    representatives: list[dict[str, Any]] = []
    for pathway_id, (_key, path) in ordered_representatives[:path_limit]:
        representatives.append(
            {
                "path_id": _text(path.get("path_id")),
                "pathway_id": pathway_id,
                "pathway_label": _text(_first(path, "pathway_label", "pathway_name")) or pathway_id,
                "route_type": _text(path.get("route_type")),
                "target_gene_id": _path_gene_id(path),
                "target_symbol": _path_gene_symbol(path, _path_gene_id(path)),
                "evidence_ids": _path_evidence_ids(path),
                "adjusted_p": _path_adjusted_p(path),
            }
        )

    s1_records = route_counts.get("s1_enrichment_overlap", 0)
    public_records = route_counts.get("public_pathway", 0)
    dimension = {
        "key": "s1_pathway_association",
        "graph_build_id": _text(paths[0].get("graph_build_id")) if paths else "",
        "status": "complete" if s1_records else "missing",
        "metrics": {
            "record_count": s1_records,
            "public_pathway_record_count": public_records,
            "unique_pathway_count": len(pathways),
            "unique_pathway_count_scope": "全部 KG06 path 的 unique pathway_id；不是记录数",
            "representative_path_count": len(representatives),
            "representative_limit": path_limit,
            "representative_truncated": len(pathways) > len(representatives),
            "s1_deg_overlap_record_count": route_counts.get("s1_deg_overlap", 0),
            "record_definition": "每条 s1_enrichment_overlap path 是一条通路关联记录；同一 pathway_id 可对应多条记录",
        },
        "pathways_total": len(pathways),
        "pathway_ids": pathway_ids,
        "representatives": representatives,
    }
    return dimension, pathways


def _s2_conditions(package: Mapping[str, Any], missing_fields: list[str]) -> list[dict[str, Any]]:
    screening = _as_mapping(package.get("screening_evidence"))
    raw_conditions = _as_list(
        package.get("s2_conditions") or screening.get("s2_conditions")
    )
    conditions: list[dict[str, Any]] = []
    for index, item in enumerate(raw_conditions):
        if isinstance(item, Mapping):
            conditions.append(_normalise_condition(item, index))
    conditions.sort(
        key=lambda row: (
            row["dose_number"] is None,
            row["dose_number"] if row["dose_number"] is not None else float("inf"),
            row["dose"],
            row["condition_key"],
        )
    )
    if conditions and any(not row["evidence_ids"] for row in conditions):
        missing_fields.append("s2_condition_evidence_ids_not_present_in_fixture")
    return conditions


def _candidate_sources(package: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    screening = _as_mapping(package.get("screening_evidence"))
    out: list[Mapping[str, Any]] = []
    for name, value in (
        ("candidate", package.get("candidate")),
        ("s2_candidate", screening.get("s2_candidate")),
        ("exp006", screening.get("exp006")),
        ("exp006_properties", _as_mapping(screening.get("exp006")).get("properties")),
    ):
        if isinstance(value, Mapping) and value:
            item = dict(value)
            item["_source"] = name
            out.append(item)
    return out


def _signature_dimension(
    package: Mapping[str, Any],
    conditions: Sequence[Mapping[str, Any]],
    missing_fields: list[str],
) -> dict[str, Any]:
    sources = _candidate_sources(package)
    n_signatures, _ = _first_from(sources, "n_signatures", "signature_count")
    replicates, _ = _first_from(sources, "replicates_median", "replicate_count")
    n_doses, _ = _first_from(
        sources, "n_24h_dose_conditions", "n_dose_conditions", "n_available_doses"
    )
    negative_fraction, _ = _first_from(sources, "negative_dose_fraction")
    primary_dose, _ = _first_from(sources, "primary_dose", "primary_dose_selection")
    source_dataset, _ = _first_from(sources, "source_dataset", "signature_source")
    stability_grade, _ = _first_from(sources, "stability_grade")
    dose_stability, _ = _first_from(sources, "dose_stability")

    n_signatures_int = _int_or_none(n_signatures)
    if n_signatures_int is None and conditions:
        n_signatures_int = _int_or_none(conditions[0].get("n_signatures"))
    replicates_float = _float_or_none(replicates)
    if replicates_float is None:
        condition_replicates = [
            _float_or_none(row.get("replicates_median")) for row in conditions
        ]
        condition_replicates = [value for value in condition_replicates if value is not None]
        if condition_replicates:
            replicates_float = max(condition_replicates)
    if n_doses is None:
        n_doses = len(conditions)
    if negative_fraction is None and conditions:
        wtcs_values = [row["WTCS_median"] for row in conditions if row["WTCS_median"] is not None]
        if wtcs_values:
            negative_fraction = sum(1 for value in wtcs_values if value < 0) / len(wtcs_values)
    if n_signatures_int is None:
        missing_fields.append("signature_coverage.n_signatures")
    if replicates_float is None:
        missing_fields.append("signature_coverage.replicates_median")
    if not primary_dose:
        missing_fields.append("signature_coverage.primary_dose")
    if not source_dataset:
        missing_fields.append("signature_coverage.source_dataset")
    signature_level = _text(_first(_as_mapping(package.get("candidate")), "signature_level")) or None
    if not signature_level:
        missing_fields.append("signature_coverage.signature_level")

    return {
        "key": "signature_coverage",
        "graph_build_id": _text(package.get("graph_build_id")),
        "status": "complete" if n_signatures_int is not None else "partial",
        "metrics": {
            "n_signatures": n_signatures_int,
            "n_dose_conditions": _int_or_none(n_doses) or len(conditions),
            "replicates_median": replicates_float,
            "negative_dose_fraction": (
                _float_or_none(negative_fraction)
                if negative_fraction is not None else None
            ),
            "primary_dose": _text(primary_dose) or None,
            "source_dataset": _text(source_dataset) or None,
            "stability_grade": _text(stability_grade) or None,
            "dose_stability": _text(dose_stability) or None,
            "single_consensus_signature": n_signatures_int == 1,
            "signature_level": signature_level,
        },
        "note": (
            "replicates_median 是实例/重复折叠计数，不等同于独立生物学重复；"
            "主条件只有 n_signatures=1 时不能包装成多组独立生物学重复。"
        ),
        "missing_fields": [
            field for field in (
                "n_signatures" if n_signatures_int is None else "",
                "replicates_median" if replicates_float is None else "",
                "primary_dose" if not primary_dose else "",
                "source_dataset" if not source_dataset else "",
            ) if field
        ],
    }


def _dose_dimension(
    package: Mapping[str, Any],
    conditions: Sequence[Mapping[str, Any]],
    missing_fields: list[str],
) -> dict[str, Any]:
    sources = _candidate_sources(package)
    primary_dose, _ = _first_from(sources, "primary_dose", "primary_dose_selection")
    stability_grade, _ = _first_from(sources, "stability_grade")
    dose_stability, _ = _first_from(sources, "dose_stability")
    primary_dose_text = _text(primary_dose)

    negatives = [row for row in conditions if row["direction"] == "negative"]
    positives = [row for row in conditions if row["direction"] == "positive"]
    negative_doses = [row["dose"] for row in negatives if row["dose"]]
    primary_condition: Mapping[str, Any] | None = None
    if primary_dose_text:
        primary_number = _dose_number(primary_dose_text)
        for row in conditions:
            if (
                row["dose"] == primary_dose_text
                or (
                    primary_number is not None
                    and row["dose_number"] is not None
                    and abs(row["dose_number"] - primary_number) < 1e-9
                )
            ):
                primary_condition = row
                break
    if not primary_condition and primary_dose_text:
        missing_fields.append("dose_consistency.primary_dose_condition_not_found")

    if not conditions:
        missing_fields.append("dose_consistency.s2_conditions")

    return {
        "key": "dose_consistency",
        "graph_build_id": _text(package.get("graph_build_id")),
        "status": "complete" if conditions else "missing",
        "metrics": {
            "condition_count": len(conditions),
            "negative_condition_count": len(negatives),
            "positive_condition_count": len(positives),
            "negative_dose_fraction": (
                len(negatives) / len(conditions) if conditions else None
            ),
            "negative_doses": negative_doses,
            "direction_flip": bool(negatives and positives),
            "primary_dose": primary_dose_text or None,
            "primary_dose_wtcs": (
                primary_condition.get("WTCS_median") if primary_condition else None
            ),
            "stability_grade": _text(stability_grade) or None,
            "dose_stability": _text(dose_stability) or None,
            "source": "s2_conditions",
        },
        "conditions": [
            {
                "condition_key": row["condition_key"],
                "cell_id": row["cell_id"],
                "pert_type": row["pert_type"],
                "dose": row["dose"],
                "time": row["time"],
                "n_signatures": row["n_signatures"],
                "WTCS_median": row["WTCS_median"],
                "replicates_median": row["replicates_median"],
                "direction": row["direction"],
                "evidence_ids": row["evidence_ids"],
            }
            for row in conditions
        ],
        "caveat": "五个剂量方向不稳定时只能陈述实际方向；不能声称剂量一致性良好或存在剂量依赖效应。",
    }


def _independent_dimension(
    package: Mapping[str, Any], missing_fields: list[str]
) -> dict[str, Any]:
    screening = _as_mapping(package.get("screening_evidence"))
    iv = _as_mapping(screening.get("independent_validation_and_prism"))
    phase_f = _as_mapping(screening.get("phase_f_joined_evidence"))
    exp006 = _as_mapping(screening.get("exp006"))
    raw: dict[str, Any] = {}
    for source in (iv, phase_f, exp006):
        raw.update(source)
    raw.pop("available", None)

    exp010 = {
        key: value for key, value in raw.items()
        if key.startswith("external_s1") or key in {"source_dataset", "rank_B", "wtcs_median_B"}
    }
    compz = {key: value for key, value in raw.items() if key.startswith("Q2")}
    prism_keys = {
        "erpos_mean_sensitivity", "erpos_median_sensitivity", "n_erpos_lines",
        "erneg_mean_sensitivity", "n_erneg_lines", "mcf7_sensitivity",
        "n_mcf7_conditions", "sensitivity_rank_erpos", "sensitivity_rank_mcf7",
        "PRISM_endpoint", "WTCS_PRISM_agreement_interpretation",
    }
    prism = {key: value for key, value in raw.items() if key in prism_keys}
    exp006_fields = {
        key: value for key, value in exp006.items()
        if key in {
            "n_signatures", "n_available_doses", "negative_dose_fraction",
            "dose_stability", "stability_grade", "evidence_limitations",
            "primary_dose", "source_dataset",
        }
    }

    expected = (
        "external_s1_median_rank",
        "Q2_condition_median_rank",
        "erpos_mean_sensitivity",
        "n_erpos_lines",
        "n_erneg_lines",
        "PRISM_endpoint",
    )
    absent = [key for key in expected if raw.get(key) in (None, "")]
    if absent:
        missing_fields.extend(f"independent_validation.{key}" for key in absent)
    available = bool(exp010 or compz or prism)
    status = "complete" if not absent else ("partial" if available else "missing")

    return {
        "key": "independent_validation",
        "graph_build_id": _text(package.get("graph_build_id")),
        "status": status,
        "claims_allowed": False,
        "metrics": {
            "available": available,
            "exp010": exp010,
            "compz": compz,
            "prism": prism,
            "exp006": exp006_fields,
            "source_note": "这些字段来自 KG06 证据包中的 phase_f/exp006 记录；没有 EvidenceRecord 或 Path ID 时不能作为可引用 claim。",
        },
        "caveats": [
            "COMPZ、Exp010 与主 WTCS 使用相同 LINCS 药物扰动数据；不能称为独立药物实验。",
            "PRISM 是细胞活性/viability 证据，不代表临床疗效，也不能单独证明 ER+ 选择性。",
            "没有证据绑定不能生成独立验证 claim。",
        ],
        "missing_fields": absent,
    }


def _certified_s2_evidence(
    conditions: Sequence[Mapping[str, Any]],
    path_evidence_ids: set[str],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Return evidence metadata only for real ``HAS_S2_CONDITION`` records.

    KG06 stores S2 evidence IDs on the S2 condition itself, not in the
    mechanism paths.  The condition's ``sources`` array is therefore the
    authoritative evidence record for an S2 ID.  An ID is accepted only when
    its source record is verified and points back to this condition; an empty
    path list is allowed, but an ID that is also path evidence is not.
    """

    metadata: dict[str, dict[str, Any]] = {}
    certified_conditions: list[dict[str, Any]] = []
    invalid_evidence_ids: list[str] = []
    condition_by_evidence: dict[str, str] = {}

    for condition in conditions:
        evidence_ids = _dedupe(condition.get("evidence_ids", []))
        if not evidence_ids:
            continue
        condition_id = _text(condition.get("condition_id"))
        if condition.get("evidence_complete") is not True or not condition_id:
            invalid_evidence_ids.extend(evidence_ids)
            continue

        valid_ids: list[str] = []
        for evidence_id in evidence_ids:
            if evidence_id in path_evidence_ids:
                invalid_evidence_ids.append(evidence_id)
                continue

            owner = condition_by_evidence.get(evidence_id)
            if owner is not None and owner != condition_id:
                invalid_evidence_ids.append(evidence_id)
                continue
            condition_by_evidence[evidence_id] = condition_id

            matching_sources = [
                row
                for row in _as_list(condition.get("sources"))
                if isinstance(row, Mapping)
                and _text(row.get("evidence_id")) == evidence_id
                and _text(row.get("predicate")).upper() == S2_CONDITION_PREDICATE
                and row.get("verified_in_postgres") is True
                and _text(row.get("source_record_id"))
                and _text(row.get("source_release"))
                and _text(row.get("assertion_sha256"))
                and _text(row.get("object_id")) == condition_id
            ]
            if len(matching_sources) != 1:
                invalid_evidence_ids.append(evidence_id)
                continue
            source = matching_sources[0]

            valid_ids.append(evidence_id)
            metadata[evidence_id] = {
                "evidence_id": evidence_id,
                "kind": "s2",
                "path_ids": [],
                "route_types": [],
                "target_genes": [],
                "gene_ids": [],
                "gene_symbols": [],
                "pathway_ids": [],
                "source_record_id": _text(source.get("source_record_id")),
                "source_release": _text(source.get("source_release")),
                "source_predicate": S2_CONDITION_PREDICATE,
                "condition_id": condition_id,
                "verified_in_postgres": True,
                "assertion_sha256": _text(source.get("assertion_sha256")),
            }

        if valid_ids and len(valid_ids) == len(evidence_ids):
            certified = dict(condition)
            certified["evidence_ids"] = valid_ids
            certified_conditions.append(certified)

    if invalid_evidence_ids:
        # S2 evidence identity is all-or-nothing.  Do not expose or retain the
        # valid subset when any ID has an ambiguous or unauthenticated source.
        return {}, [], sorted(set(invalid_evidence_ids))

    return metadata, certified_conditions, sorted(set(invalid_evidence_ids))


def _build_validator_bundle(
    package: Mapping[str, Any],
    paths: Sequence[Mapping[str, Any]],
    targets: Sequence[Mapping[str, Any]],
    pathways: Sequence[Mapping[str, Any]],
    conditions: Sequence[Mapping[str, Any]],
    missing_fields: list[str],
    *,
    target_annotation_count: int,
) -> dict[str, Any]:
    candidate = _as_mapping(package.get("candidate"))
    coverage = _as_mapping(package.get("coverage"))
    route_counts = Counter(_text(p.get("route_type")) for p in paths)
    path_meta: dict[str, dict[str, Any]] = {}
    evidence_meta: dict[str, dict[str, Any]] = {}
    all_evidence_ids: list[str] = []

    for path in paths:
        path_id = _text(path.get("path_id"))
        route = _text(path.get("route_type"))
        gene_id = _path_gene_id(path)
        symbol = _path_gene_symbol(path, gene_id)
        pathway_id = _text(_first(path, "pathway_id", "pathway"))
        evidence_ids = _path_evidence_ids(path)
        all_evidence_ids.extend(evidence_ids)
        if path_id:
            path_meta[path_id] = {
                "path_id": path_id,
                "route_type": route,
                "target_gene_id": gene_id,
                "target_symbol": symbol,
                "pathway_id": pathway_id,
                "relation": _path_relation(path),
                "s1_direction": _path_s1_direction(path),
                "evidence_ids": evidence_ids,
            }
        for evidence_id in evidence_ids:
            meta = evidence_meta.setdefault(
                evidence_id,
                {
                    "evidence_id": evidence_id,
                    "kind": "pathway",
                    "route_types": [],
                    "path_ids": [],
                    "gene_ids": [],
                    "pathway_ids": [],
                },
            )
            if route and route not in meta["route_types"]:
                meta["route_types"].append(route)
            if path_id and path_id not in meta["path_ids"]:
                meta["path_ids"].append(path_id)
            if gene_id and gene_id not in meta["gene_ids"]:
                meta["gene_ids"].append(gene_id)
            if pathway_id and pathway_id not in meta["pathway_ids"]:
                meta["pathway_ids"].append(pathway_id)
            if route in {"direct_target", "s1_deg_overlap"}:
                meta["kind"] = "target" if route == "direct_target" else "s1_deg"

    path_evidence_ids = set(all_evidence_ids)
    s2_evidence_meta, certified_conditions, invalid_s2_evidence_ids = (
        _certified_s2_evidence(conditions, path_evidence_ids)
    )
    for evidence_id, meta in s2_evidence_meta.items():
        evidence_meta[evidence_id] = meta

    # Rebuild evidence metadata from path-level facts so target genes and S1
    # direction are explicit, matching Top10 interpretation/evidence.py.
    for evidence_id, meta in evidence_meta.items():
        path_ids = sorted(_dedupe(meta.get("path_ids", [])))
        route_types = sorted(
            {
                str(path_meta[path_id].get("route_type") or "")
                for path_id in path_ids
                if path_id in path_meta
            }
        )
        if "s2" in (meta.get("route_types") or []) and "s2" not in route_types:
            route_types.append("s2")
        target_genes: list[dict[str, Any]] = []
        gene_ids: list[str] = []
        gene_symbols: list[str] = []
        for path_id in path_ids:
            path_entry = path_meta.get(path_id) or {}
            gene_id = _text(path_entry.get("target_gene_id"))
            if not gene_id:
                continue
            if gene_id not in gene_ids:
                gene_ids.append(gene_id)
            symbol = _text(path_entry.get("target_symbol"))
            if symbol and symbol not in gene_symbols:
                gene_symbols.append(symbol)
            if path_entry.get("route_type") != "direct_target":
                continue
            existing = next(
                (item for item in target_genes if item["gene_id"] == gene_id),
                None,
            )
            if existing is None:
                target_genes.append(
                    {
                        "gene_id": gene_id,
                        "symbol": symbol or None,
                        "path_ids": [path_id],
                    }
                )
            elif path_id not in existing["path_ids"]:
                existing["path_ids"].append(path_id)
        directions: list[str] = []
        for path_id in path_ids:
            direction = _text((path_meta.get(path_id) or {}).get("s1_direction")).upper()
            if direction in {"UP", "DOWN"} and direction not in directions:
                directions.append(direction)
        meta["path_ids"] = path_ids
        meta["route_types"] = route_types
        if target_genes:
            meta["kind"] = "target"
        elif "s1_deg_overlap" in route_types:
            meta["kind"] = "s1_deg"
        elif "s2" in route_types or meta.get("kind") == "s2":
            meta["kind"] = "s2"
        else:
            meta["kind"] = "pathway"
        meta["s1_direction"] = directions[0] if len(directions) == 1 else None
        meta["target_genes"] = target_genes
        meta["gene_ids"] = gene_ids
        meta["gene_symbols"] = gene_symbols

    evidence_layers: dict[str, str] = {}
    for evidence_id, meta in evidence_meta.items():
        if meta["kind"] in {"target", "s1_deg", "s2"}:
            evidence_layers[evidence_id] = "A"
        elif meta.get("route_types"):
            evidence_layers[evidence_id] = "B"
        else:
            evidence_layers[evidence_id] = "D"

    s2_evidence_ids = sorted(s2_evidence_meta)
    allowed_evidence_ids = _dedupe([*all_evidence_ids, *s2_evidence_ids])
    unique_evidence_count = len(set(all_evidence_ids))
    if invalid_s2_evidence_ids:
        missing_fields.append("s2_condition_evidence_unverified")
    if coverage.get("s2_condition_evidence_count") and not s2_evidence_ids:
        missing_fields.append("validator_bundle.s2_evidence_ids")
    if not package.get("graph_build_id"):
        missing_fields.append("graph_build_id")

    ranked_candidate = dict(candidate)
    compound_id = _text(_first(candidate, "compound_id", "drug_id"))
    mapping_status = _text(candidate.get("mapping_status")) or "unknown"

    return {
        "bundle_version": "kg07-validator-bundle-1.0",
        "source_package_version": _text(package.get("package_version")),
        "project_id": _text(package.get("project_id")),
        "graph_build_id": _text(package.get("graph_build_id")),
        "pert_id": _text(package.get("pert_id")),
        "candidate": ranked_candidate,
        "compound": {
            "compound_id": compound_id or None,
            "drug_name": _text(_first(candidate, "drug_name", "pert_id")) or None,
            "mapping_status": mapping_status,
            "mapped": bool(coverage.get("compound_mapped", bool(compound_id))),
        },
        "counts": {
            "direct_target_paths": route_counts.get("direct_target", 0),
            "public_pathway_paths": route_counts.get("public_pathway", 0),
            "s1_enrichment_paths": route_counts.get("s1_enrichment_overlap", 0),
            "s1_deg_paths": route_counts.get("s1_deg_overlap", 0),
            "unique_evidence": unique_evidence_count,
            "s2_conditions": len(conditions),
            "direct_target_annotations": target_annotation_count,
            "unique_target_genes": len(targets),
            "unique_pathways": len(pathways),
        },
        "targets": list(targets),
        "pathways": list(pathways),
        "s2_conditions": {
            "available": bool(conditions),
            "conditions": list(conditions),
            "certified_conditions": certified_conditions,
            "validation": {
                "valid": not invalid_s2_evidence_ids,
                "certified_condition_count": len(certified_conditions),
                "invalid_evidence_ids": invalid_s2_evidence_ids,
            },
        },
        "path_route_types": {
            path_id: meta["route_type"] for path_id, meta in path_meta.items()
        },
        "path_meta": path_meta,
        "evidence_meta": evidence_meta,
        "evidence_layers": evidence_layers,
        "allowed_evidence_ids": allowed_evidence_ids,
        "allowed_path_ids": sorted(path_meta),
        "total_path_ids": sorted(path_meta),
        "evidence_gaps": list(_as_list(package.get("evidence_gaps"))),
        "rank_evidence_id": package.get("rank_evidence_id"),
        "missing_fields": sorted(set(missing_fields)),
        "source_coverage": dict(coverage),
    }


def _build_llm_input(
    package: Mapping[str, Any],
    targets: Sequence[Mapping[str, Any]],
    pathways: Sequence[Mapping[str, Any]],
    conditions: Sequence[Mapping[str, Any]],
    representative_limit: int,
    *,
    total_pathway_count: int,
    signature_metrics: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    candidate = _as_mapping(package.get("candidate"))
    coverage = _as_mapping(package.get("coverage"))
    route_counts = Counter(_text(p.get("route_type")) for p in _as_list(package.get("paths")))
    # representatives are supplied by the pathway dimension; rebuild the final
    # bounded list here from the exact same deterministic order.
    bounded_pathways = list(pathways[:representative_limit])
    signature = _as_mapping(signature_metrics)

    def signature_value(key: str, *candidate_keys: str) -> Any:
        value = signature.get(key)
        if value is not None:
            return value
        return _first(candidate, *candidate_keys) if candidate_keys else None

    return {
        "llm_input_version": "kg07-llm-input-1.0",
        "graph_build_id": _text(package.get("graph_build_id")),
        "project_id": _text(package.get("project_id")),
        "pert_id": _text(package.get("pert_id")),
        "candidate": dict(candidate),
        "compound": {
            "compound_id": _text(_first(candidate, "compound_id", "drug_id")) or None,
            "drug_name": _text(_first(candidate, "drug_name", "pert_id")) or None,
            "mapping_status": _text(candidate.get("mapping_status")) or "unknown",
            "mapped": bool(
                coverage.get(
                    "compound_mapped",
                    bool(_first(candidate, "compound_id", "drug_id")),
                )
            ),
        },
        "signature_coverage": {
            "n_signatures": _int_or_none(signature_value("n_signatures", "n_signatures")),
            "n_dose_conditions": _int_or_none(
                signature_value("n_dose_conditions", "n_24h_dose_conditions", "n_dose_conditions")
            ),
            "replicates_median": signature_value("replicates_median", "replicates_median"),
            "negative_dose_fraction": signature_value("negative_dose_fraction", "negative_dose_fraction"),
            "primary_dose": _text(signature_value("primary_dose", "primary_dose")) or None,
            "source_dataset": _text(signature_value("source_dataset", "source_dataset")) or None,
            "signature_level": signature_value("signature_level", "signature_level"),
            "single_consensus_signature": signature_value(
                "single_consensus_signature", "single_consensus_signature"
            ),
            "dose_stability": signature_value("dose_stability", "dose_stability"),
        },
        "dose_conditions": [
            {
                "dose": row["dose"],
                "time": row["time"],
                "WTCS_median": row["WTCS_median"],
                "direction": row["direction"],
                "n_signatures": row["n_signatures"],
                "evidence_ids": row["evidence_ids"],
            }
            for row in conditions
        ],
        "targets": [
            {
                "gene_id": row["gene_id"],
                "symbol": row["symbol"],
                "reported_actions": row["reported_actions"],
                "relation": row["relation"],
                "path_ids": row["path_ids"],
                "evidence_ids": row["evidence_ids"],
            }
            for row in targets
        ],
        "pathways": bounded_pathways,
        "pathway_counts": {
            "record_count": route_counts.get("s1_enrichment_overlap", 0),
            "unique_pathway_count": total_pathway_count,
            "representative_count": len(bounded_pathways),
            "representative_limit": representative_limit,
            "representative_truncated": len(pathways) > len(bounded_pathways),
        },
        "evidence_gaps": list(_as_list(package.get("evidence_gaps"))),
        "independent_validation_status": "not_citable_in_stage_1",
        "constraints": {
            "no_clinical_efficacy_claim": True,
            "no_causal_claim_without_evidence": True,
            "no_target_or_pathway_invention": True,
            "no_annotation_count_as_independent_target_count": True,
        },
    }


def _build_legacy_sections(package: Mapping[str, Any]) -> dict[str, Any]:
    gaps = [str(gap) for gap in _as_list(package.get("evidence_gaps")) if _text(gap)]
    return {
        "screening_basis": [],
        "known_targets": [],
        "associated_pathways": [],
        "independent_validation": [],
        "mechanism_hypotheses": [],
        "insufficient_evidence": [
            {"text": gap, "evidence_ids": [], "path_ids": []} for gap in gaps
        ],
        "limitations": [
            gap for gap in gaps
        ],
    }


def _make_scoped_evidence_meta(
    evidence_meta: Mapping[str, Mapping[str, Any]],
    scoped_path_ids: set[str],
    path_meta: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Keep only evidence whose path membership is inside the scoped bundle."""
    scoped: dict[str, dict[str, Any]] = {}
    for evidence_id, full in evidence_meta.items():
        original_routes = set(full.get("route_types") or [])
        path_ids = sorted(
            set(full.get("path_ids") or []) & scoped_path_ids
        )
        if not path_ids and "s2" not in original_routes and full.get("kind") != "s2":
            continue
        route_types = sorted(
            {
                str((path_meta.get(path_id) or {}).get("route_type") or "")
                for path_id in path_ids
                if path_id in path_meta
            }
        )
        if "s2" in original_routes and "s2" not in route_types:
            route_types.append("s2")

        target_genes: list[dict[str, Any]] = []
        gene_ids: list[str] = []
        gene_symbols: list[str] = []
        directions: list[str] = []
        for path_id in path_ids:
            path_entry = path_meta.get(path_id) or {}
            gene_id = _text(path_entry.get("target_gene_id"))
            if gene_id:
                if gene_id not in gene_ids:
                    gene_ids.append(gene_id)
                symbol = _text(path_entry.get("target_symbol"))
                if symbol and symbol not in gene_symbols:
                    gene_symbols.append(symbol)
                if path_entry.get("route_type") == "direct_target":
                    existing = next(
                        (item for item in target_genes if item["gene_id"] == gene_id),
                        None,
                    )
                    if existing is None:
                        target_genes.append(
                            {
                                "gene_id": gene_id,
                                "symbol": symbol or None,
                                "path_ids": [path_id],
                            }
                        )
                    elif path_id not in existing["path_ids"]:
                        existing["path_ids"].append(path_id)
            direction = _text(path_entry.get("s1_direction")).upper()
            if direction in {"UP", "DOWN"} and direction not in directions:
                directions.append(direction)

        if target_genes:
            kind = "target"
        elif "s1_deg_overlap" in route_types:
            kind = "s1_deg"
        elif "s2" in route_types or full.get("kind") == "s2":
            kind = "s2"
        else:
            kind = "pathway"
        scoped[evidence_id] = {
            "evidence_id": evidence_id,
            "kind": kind,
            "path_ids": path_ids,
            "route_types": route_types,
            "s1_direction": directions[0] if len(directions) == 1 else None,
            "target_genes": target_genes,
            "gene_ids": gene_ids,
            "gene_symbols": gene_symbols,
            "pathway_ids": sorted(
                {
                    _text((path_meta.get(path_id) or {}).get("pathway_id"))
                    for path_id in path_ids
                    if _text((path_meta.get(path_id) or {}).get("pathway_id"))
                }
            ),
        }
    return scoped


def _scoped_counts(
    path_meta: Mapping[str, Mapping[str, Any]],
    evidence_meta: Mapping[str, Mapping[str, Any]],
    targets: Sequence[Mapping[str, Any]],
    conditions: Sequence[Mapping[str, Any]],
    *,
    target_annotation_count: int,
) -> dict[str, int]:
    route_counts = Counter(
        str(meta.get("route_type") or "") for meta in path_meta.values()
    )
    pathway_ids = {
        _text(meta.get("pathway_id"))
        for meta in path_meta.values()
        if _text(meta.get("pathway_id"))
    }
    return {
        "direct_target_paths": route_counts.get("direct_target", 0),
        "public_pathway_paths": route_counts.get("public_pathway", 0),
        "s1_enrichment_paths": route_counts.get("s1_enrichment_overlap", 0),
        "s1_deg_paths": route_counts.get("s1_deg_overlap", 0),
        "unique_evidence": len(evidence_meta),
        "s2_conditions": len(conditions),
        "direct_target_annotations": target_annotation_count,
        "unique_target_genes": len(targets),
        "unique_pathways": len(pathway_ids),
    }


def build_kg07_evidence(
    package: Mapping[str, Any],
    *,
    representative_pathway_limit: int = REPRESENTATIVE_PATHWAY_LIMIT,
    expected_graph_build_id: str | None = None,
) -> dict[str, Any]:
    """Build deterministic KG07 evidence outputs from a real KG06 package.

    ``expected_graph_build_id`` is supplied by the Stage 2 service after
    ``resolve_graph_build()``.  If provided, an archived/historical package is
    rejected rather than silently treated as the production GraphBuild.
    """
    if not isinstance(package, Mapping):
        raise TypeError("package must be a mapping")
    if representative_pathway_limit < 1:
        raise ValueError("representative_pathway_limit must be >= 1")

    paths = [p for p in _as_list(package.get("paths")) if isinstance(p, Mapping)]
    if not paths:
        raise ValueError("KG06 package has no paths")
    missing_fields: list[str] = []

    graph_build_id = _text(package.get("graph_build_id"))
    if not graph_build_id:
        missing_fields.append("graph_build_id")
    expected_build_id = _text(expected_graph_build_id)
    if expected_build_id and graph_build_id != expected_build_id:
        raise ValueError(
            "KG06 package GraphBuild does not match the resolved production GraphBuild: "
            f"package={graph_build_id or '<missing>'} expected={expected_build_id}"
        )
    if not _text(package.get("pert_id")):
        missing_fields.append("pert_id")

    target_dimension, targets = _target_dimension(paths, package, missing_fields)
    pathway_dimension, pathways = _pathway_dimension(
        paths, missing_fields, path_limit=representative_pathway_limit
    )
    conditions = _s2_conditions(package, missing_fields)
    signature_dimension = _signature_dimension(package, conditions, missing_fields)
    dose_dimension = _dose_dimension(package, conditions, missing_fields)
    independent_dimension = _independent_dimension(package, missing_fields)

    # The pathway dimension owns the canonical representative ordering.  Keep
    # that list in the LLM input, not the full 99-pathway list.
    representative_pathways = list(pathway_dimension["representatives"])

    dimensions = {
        "signature_coverage": signature_dimension,
        "dose_consistency": dose_dimension,
        "target_coverage": target_dimension,
        "s1_pathway_association": pathway_dimension,
        "independent_validation": independent_dimension,
    }
    # All dimension payloads must carry the same GraphBuild ID.
    for dimension in dimensions.values():
        dimension["graph_build_id"] = graph_build_id

    validator_bundle = _build_validator_bundle(
        package,
        paths,
        targets,
        pathways,
        conditions,
        missing_fields,
        target_annotation_count=len(target_dimension["annotations"]),
    )
    validator_bundle["scope"] = "full"
    validator_bundle["coverage_eligible"] = True
    # The LLM must see only the bounded representative pathway list, never the
    # complete 99-pathway set or the raw 157-path package.
    llm_input = _build_llm_input(
        package,
        targets,
        representative_pathways,
        conditions,
        representative_pathway_limit,
        total_pathway_count=len(pathways),
        signature_metrics=signature_dimension["metrics"],
    )
    llm_input["graph_build_id"] = graph_build_id
    llm_input["target_annotation_count"] = len(target_dimension["annotations"])
    llm_input["target_unique_gene_count"] = len(targets)

    # A scoped validator bundle for the actual LLM call.  It is deliberately
    # not coverage-eligible: coverage must use the complete bundle above.
    llm_pathways = representative_pathways
    llm_path_ids = set()
    for target in targets:
        llm_path_ids.update(target.get("path_ids", []))
        s1_deg = target.get("s1_deg")
        if isinstance(s1_deg, Mapping):
            llm_path_ids.update(s1_deg.get("path_ids", []))
    for pathway in llm_pathways:
        if pathway.get("path_id"):
            llm_path_ids.add(_text(pathway.get("path_id")))
        llm_path_ids.update(pathway.get("path_ids", []))
    llm_path_meta: dict[str, Any] = {}
    for path_id in sorted(llm_path_ids):
        meta = validator_bundle["path_meta"].get(path_id)
        if meta:
            llm_path_meta[path_id] = meta

    llm_evidence_meta = _make_scoped_evidence_meta(
        validator_bundle["evidence_meta"], set(llm_path_meta), llm_path_meta
    )
    s2_validation = _as_mapping(
        _as_mapping(validator_bundle.get("s2_conditions")).get("validation")
    )
    invalid_s2_evidence_ids = list(s2_validation.get("invalid_evidence_ids") or [])
    if invalid_s2_evidence_ids:
        # A single unauthenticated non-empty S2 ID invalidates the whole
        # direct/scoped S2 input; do not let the valid subset silently pass.
        llm_evidence_meta = {
            evidence_id: meta
            for evidence_id, meta in llm_evidence_meta.items()
            if meta.get("kind") != "s2"
        }
    safe_s2_conditions = (
        []
        if invalid_s2_evidence_ids
        else list(
            _as_mapping(validator_bundle.get("s2_conditions")).get(
                "certified_conditions"
            )
            or []
        )
    )
    llm_evidence_ids = set(llm_evidence_meta)
    scoped_counts = _scoped_counts(
        llm_path_meta,
        llm_evidence_meta,
        targets,
        conditions,
        target_annotation_count=len(target_dimension["annotations"]),
    )
    dropped_evidence_ids = sorted(
        set(validator_bundle["evidence_meta"]) - set(llm_evidence_meta)
    )
    llm_validator_bundle = {
        **validator_bundle,
        "scope": "llm_scoped",
        "coverage_eligible": False,
        "targets": list(targets),
        "pathways": list(llm_pathways),
        "path_route_types": {
            path_id: meta["route_type"] for path_id, meta in llm_path_meta.items()
        },
        "path_meta": llm_path_meta,
        "evidence_meta": llm_evidence_meta,
        "s2_conditions": {
            **validator_bundle["s2_conditions"],
            "available": bool(safe_s2_conditions),
            "conditions": safe_s2_conditions,
        },
        "evidence_layers": {
            evidence_id: (
                "A" if meta.get("kind") in {"target", "s1_deg", "s2"}
                else "B" if meta.get("route_types") else "D"
            )
            for evidence_id, meta in llm_evidence_meta.items()
        },
        "allowed_evidence_ids": sorted(llm_evidence_meta),
        "allowed_path_ids": sorted(llm_path_meta),
        "total_path_ids": validator_bundle["total_path_ids"],
        "counts": scoped_counts,
        "dropped_evidence_ids": dropped_evidence_ids,
    }
    if not conditions or not any(row.get("evidence_ids") for row in conditions):
        missing_fields.append("s2_condition_evidence_ids")
    validator_bundle["missing_fields"] = sorted(set(missing_fields))
    llm_validator_bundle["missing_fields"] = sorted(set(missing_fields))

    legacy_sections = _build_legacy_sections(package)
    for key in LEGACY_SECTION_KEYS:
        legacy_sections.setdefault(key, [])

    summary = {
        "adapter_version": ADAPTER_VERSION,
        "generated_at_utc": None,  # deliberately no wall-clock in deterministic output
        "graph_build_id": graph_build_id,
        "project_id": _text(package.get("project_id")),
        "pert_id": _text(package.get("pert_id")),
        "package_id": _text(package.get("package_id")),
        "package_version": _text(package.get("package_version")),
        "dimension_order": list(DIMENSION_ORDER),
        "dimensions": dimensions,
        "five_dimensions": [dimensions[key] for key in DIMENSION_ORDER],
        "validator_bundle": validator_bundle,
        "llm_validator_bundle": llm_validator_bundle,
        "llm_input": llm_input,
        "legacy_sections": legacy_sections,
        "metadata": {
            "path_count": len(paths),
            "target_annotation_count": len(target_dimension["annotations"]),
            "unique_target_gene_count": len(targets),
            "unique_pathway_count": len(pathways),
            "representative_pathway_count": len(representative_pathways),
            "representative_pathway_limit": representative_pathway_limit,
            "s2_condition_count": len(conditions),
            "evidence_id_count": len(validator_bundle["allowed_evidence_ids"]),
            "validator_bundle_sha256": _canonical_sha256(validator_bundle),
            "missing_fields": sorted(set(missing_fields)),
            "source": "KG06 evidence package (real paths/screening fields only; no DB, Neo4j, provider, or validator call)",
        },
    }
    return summary
