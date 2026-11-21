"""Structural, citation-type, layer, and language validation.

The model never decides its own evidence class, coverage level, limitations, or
suggestions. This module binds every claim to evidence types that can actually
support it, recomputes the layer from cited IDs, and drops claims that cannot be
verified against the current drug/build evidence bundle.
"""
from __future__ import annotations

import re
from typing import Any

CLAIM_SECTIONS = (
    "screening_rationale",
    "target_mechanism",
    "pathway_association",
    "er_positive_relevance",
    "mechanism_hypotheses",
)

# Which evidence kinds/routes may support which section. A cited item outside the
# allowed sets invalidates the whole claim; we never keep "one seemingly legal" ID.
SECTION_RULES: dict[str, dict[str, set[str]]] = {
    "screening_rationale": {
        "allowed_kinds": {"s2", "rank"},
        "allowed_routes": set(),
        "require_kinds": {"s2", "rank"},
        "require_routes": set(),
    },
    "target_mechanism": {
        "allowed_kinds": {"target", "s1_deg"},
        "allowed_routes": {"direct_target", "s1_deg_overlap"},
        "require_kinds": {"target"},
        "require_routes": {"direct_target"},
    },
    "pathway_association": {
        "allowed_kinds": {"pathway", "s1_deg"},
        "allowed_routes": {"public_pathway", "s1_enrichment_overlap", "s1_deg_overlap"},
        "require_kinds": {"pathway"},
        "require_routes": {"public_pathway", "s1_enrichment_overlap"},
    },
    "er_positive_relevance": {
        "allowed_kinds": {"s2", "s1_deg", "pathway"},
        "allowed_routes": {"s1_deg_overlap", "public_pathway", "s1_enrichment_overlap"},
        "require_kinds": {"s2", "s1_deg", "pathway"},
        "require_routes": set(),
    },
    "mechanism_hypotheses": {
        "allowed_kinds": {"target", "s1_deg", "pathway"},
        "allowed_routes": {"direct_target", "s1_deg_overlap", "public_pathway", "s1_enrichment_overlap"},
        "require_kinds": {"target", "pathway"},
        "require_routes": set(),
    },
}

RELATIONS = {"binding", "inhibition", "activation", "association", "unknown"}
OVERCLAIM_PATTERNS = (
    "已证实",
    "已经证明",
    "证明了",
    "必然",
    "确定",
    "完全抑制",
    "显著改善",
    "疗效确切",
)
EFFICACY_PATTERNS = (
    "治愈",
    "临床有效",
    "疗效",
    "推荐使用",
    "可用于治疗",
    "能够治疗",
    "杀伤",
    "抑制肿瘤",
    "抑制生长",
    "抗肿瘤",
    "缩小肿瘤",
    "细胞毒性",
    "延长生存",
    "生存期",
    "临床获益",
    "获益",
    "耐药",
    "治疗敏感",
    "改善症状",
    "逆转耐药",
    "有效治疗",
    "治疗",
    "有效",
)
CAUSAL_PATTERNS = ("导致", "引起", "驱动", "造成", "从而抑制", "从而使", "进而抑制", "进而激活", "介导")
EXPRESSION_UP = ("上调", "升高", "高表达", "表达增加")
EXPRESSION_DOWN = ("下调", "降低", "低表达", "表达减少")
DIRECTIONAL_EFFECT = ("逆转",)
SUPPRESS_PATTERNS = ("抑制", "阻断", "拮抗")
PROMOTE_PATTERNS = ("激活", "促进", "诱导")
S1_CONTEXT_WORDS = ("S1", "差异表达")
MAX_TEXT = 120

# Exact, curated HGNC/NCBI synonyms. This is deliberately a whitelist of
# standardized symbols/synonyms, not a similarity or substring matcher.
# Unknown aliases are rejected instead of being guessed.
GENE_ALIASES: dict[str, tuple[str, ...]] = {
    "EGFR": ("ERBB1", "HER1"),
    "ABL1": ("ABL", "C-ABL", "P150"),
    "HDAC1": ("HD1", "RPD3", "RPD3L1"),
    "FGFR2": ("FGFR-2", "BEK", "KGFR"),
    "KIT": ("CD117", "SCFR", "C-KIT"),
    "CDK1": ("CDC2", "CDC28A"),
}
ALIAS_TO_CANONICAL = {
    alias.upper(): canonical
    for canonical, aliases in GENE_ALIASES.items()
    for alias in (canonical, *aliases)
}


def _target_index(bundle: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    by_symbol: dict[str, dict[str, Any]] = {}
    by_id: dict[str, dict[str, Any]] = {}
    for target in bundle.get("targets") or []:
        symbol = str(target.get("symbol") or "").strip().upper()
        gene_id = str(target.get("gene_id") or "").strip()
        if symbol:
            by_symbol[symbol] = target
        if gene_id:
            by_id[gene_id] = target
    return by_symbol, by_id


def _resolve_gene_entity(value: Any, by_symbol: dict[str, dict[str, Any]], by_id: dict[str, dict[str, Any]]) -> tuple[dict[str, Any] | None, str | None]:
    """Resolve only exact gene IDs or whitelisted standardized symbols/aliases."""
    if isinstance(value, dict):
        raw_id = str(value.get("gene_id") or "").strip()
        raw_symbol = str(value.get("symbol") or "").strip()
        found: list[dict[str, Any]] = []
        if raw_id:
            target = by_id.get(raw_id)
            if target is None:
                return None, f"unknown gene_id: {raw_id}"
            found.append(target)
        if raw_symbol:
            canonical = ALIAS_TO_CANONICAL.get(raw_symbol.upper(), raw_symbol.upper())
            target = by_symbol.get(canonical)
            if target is None:
                return None, f"unknown gene symbol/alias: {raw_symbol}"
            found.append(target)
        if not found:
            return None, "gene entity has neither gene_id nor symbol"
        if len({str(item.get("gene_id")) for item in found}) > 1:
            return None, "gene_id and symbol refer to different genes"
        return found[0], None

    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None, "empty gene entity"
        target = by_id.get(raw)
        if target is not None:
            return target, None
        canonical = ALIAS_TO_CANONICAL.get(raw.upper(), raw.upper())
        target = by_symbol.get(canonical)
        if target is None:
            return None, f"unknown gene symbol/alias: {raw}"
        return target, None
    return None, "gene entity must be a string or an object"


def _token_pattern(token: str) -> re.Pattern[str]:
    # Hyphen/slash are token characters on purpose: ABL1 must not match inside
    # BCR-ABL1, and EGFR must not match inside EGFRvIII.
    return re.compile(r"(?<![A-Za-z0-9_.\-])" + re.escape(token) + r"(?![A-Za-z0-9_.\-])", re.IGNORECASE)


def _genes_mentioned_in_text(text: str, by_symbol: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for symbol, target in by_symbol.items():
        aliases = {symbol, *GENE_ALIASES.get(symbol, ())}
        for alias in aliases:
            if _token_pattern(alias).search(text):
                found[str(target.get("gene_id"))] = target
                break
    return list(found.values())


def _declared_gene_entities(
    claim: dict[str, Any],
    text: str,
    bundle: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    by_symbol, by_id = _target_index(bundle)
    raw = claim.get("genes")
    errors: list[str] = []
    entities: dict[str, dict[str, Any]] = {}
    if raw is not None:
        if not isinstance(raw, list):
            return [], ["genes must be a list"]
        for item in raw:
            target, error = _resolve_gene_entity(item, by_symbol, by_id)
            if error:
                errors.append(error)
            elif target is not None:
                entities[str(target.get("gene_id"))] = target
    else:
        # Backward-compatible path for old model output: exact target symbols or
        # whitelisted aliases must appear in the text. No guessing.
        for target in _genes_mentioned_in_text(text, by_symbol):
            entities[str(target.get("gene_id"))] = target
    return list(entities.values()), errors


def _target_gene_binding_errors(
    declared: list[dict[str, Any]],
    evidence_ids: list[str],
    effective_paths: set[str],
    bundle: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    declared_ids = {str(item.get("gene_id")) for item in declared if item.get("gene_id")}
    if not declared_ids:
        return ["target claim has no bound gene entity"]

    evidence_meta = bundle.get("evidence_meta") or {}
    path_meta = bundle.get("path_meta") or {}
    covered: set[str] = set()

    for evidence_id in evidence_ids:
        meta = evidence_meta.get(evidence_id) or {}
        route_types = set(meta.get("route_types") or [])
        target_genes = meta.get("target_genes") or []
        if "direct_target" in route_types or meta.get("kind") == "target":
            gene_ids = {str(item.get("gene_id")) for item in target_genes if item.get("gene_id")}
            if not gene_ids:
                errors.append(f"cited target evidence has no structured target_gene: {evidence_id}")
                continue
            if not gene_ids <= declared_ids:
                errors.append(f"cited target evidence does not match declared gene entity: {evidence_id}")
                continue
            covered.update(gene_ids)
        elif meta.get("kind") == "s1_deg":
            gene_ids = {str(gid) for gid in (meta.get("gene_ids") or []) if gid}
            if not gene_ids:
                errors.append(f"cited S1 evidence has no structured gene: {evidence_id}")
            elif not gene_ids <= declared_ids:
                errors.append(f"cited S1 evidence does not match declared gene entity: {evidence_id}")

    for path_id in effective_paths:
        meta = path_meta.get(path_id) or {}
        route = str(meta.get("route_type") or "")
        gene_id = str(meta.get("target_gene_id") or "")
        if route == "direct_target":
            if not gene_id:
                errors.append(f"cited target path has no structured target_gene: {path_id}")
            elif gene_id not in declared_ids:
                errors.append(f"cited target path does not match declared gene entity: {path_id}")
            else:
                covered.add(gene_id)
        elif route == "s1_deg_overlap":
            if not gene_id:
                errors.append(f"cited S1 path has no structured gene: {path_id}")
            elif gene_id not in declared_ids:
                errors.append(f"cited S1 path does not match declared gene entity: {path_id}")

    missing = sorted(declared_ids - covered)
    if missing:
        errors.append("declared gene entity has no corresponding target evidence: " + ", ".join(missing))
    return errors


def _layer_for(
    path_ids: list[str],
    evidence_ids: list[str],
    route_types: dict[str, str],
    evidence_layers: dict[str, str],
    *,
    hypothesis: bool,
) -> str:
    if hypothesis:
        return "C"
    layers = {evidence_layers.get(eid) for eid in evidence_ids if evidence_layers.get(eid)}
    layers.update(
        "A" if route_types.get(path_id) in {"direct_target", "s1_deg_overlap"} else "B"
        for path_id in path_ids
        if route_types.get(path_id)
    )
    if "B" in layers:
        return "B"
    if "A" in layers:
        return "A"
    return "D"


def _path_relation_map(bundle: dict[str, Any]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for path_id, meta in (bundle.get("path_meta") or {}).items():
        mapping[str(path_id)] = str(meta.get("relation") or "association")
    return mapping


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text:
        return None
    if len(text) > MAX_TEXT:
        text = text[: MAX_TEXT - 1] + "…"
    return text


def _sanitize_ids(
    values: Any,
    allowed: set[str],
    *,
    field: str,
    issues: list[dict[str, Any]],
    index: int,
    section: str,
) -> list[str]:
    if not isinstance(values, list):
        return []
    valid, invalid = [], []
    for value in values:
        text = str(value)
        if text in allowed:
            valid.append(text)
        else:
            invalid.append(text)
    if invalid:
        issues.append({"section": section, "index": index, "issue": f"unknown {field} removed", "invalid": invalid})
    return valid


def _citation_scope(
    evidence_ids: list[str],
    path_ids: list[str],
    bundle: dict[str, Any],
) -> tuple[set[str], set[str], set[str], str | None]:
    """Return (kinds, routes, effective_path_ids, incompatibility_reason)."""
    evidence_meta = bundle.get("evidence_meta") or {}
    path_meta = bundle.get("path_meta") or {}
    kinds: set[str] = set()
    routes: set[str] = set()
    for evidence_id in evidence_ids:
        meta = evidence_meta.get(evidence_id) or {}
        kinds.add(str(meta.get("kind") or "other"))
        routes.update(str(route) for route in (meta.get("route_types") or []))
    effective_paths = set(path_ids)
    for path_id in path_ids:
        meta = path_meta.get(path_id) or {}
        routes.add(str(meta.get("route_type") or "other"))
    # If only evidence IDs are cited, their own path membership is the effective path set.
    for evidence_id in evidence_ids:
        effective_paths.update(str(pid) for pid in ((evidence_meta.get(evidence_id) or {}).get("path_ids") or []))
    # Verify that cited evidence actually belongs to cited paths.
    if path_ids and evidence_ids:
        for evidence_id in evidence_ids:
            if not any(evidence_id in (path_meta.get(path_id, {}).get("evidence_ids") or []) for path_id in path_ids):
                return kinds, routes, effective_paths, "cited evidence_id is not part of the cited path_id"
    return kinds, routes, effective_paths, None


def _guard_language(
    text: str,
    section: str,
    relation: str,
    path_ids: list[str],
    evidence_ids: list[str],
    bundle: dict[str, Any],
) -> tuple[bool, str, str, str | None]:
    """Apply conservative language rules.

    Returns (keep, new_text, new_relation, drop_reason). This is a heuristic guard,
    not a claim of full natural-language semantic verification.
    """
    path_meta = bundle.get("path_meta") or {}
    evidence_meta = bundle.get("evidence_meta") or {}
    normalized = text.replace("抑制剂", "")
    causal = relation == "causal" or any(token in normalized for token in CAUSAL_PATTERNS)
    hypothesis = section == "mechanism_hypotheses"

    if any(token in normalized for token in EFFICACY_PATTERNS):
        return False, text, relation, "efficacy or clinical benefit claim has no project evidence"

    expression_terms = [token for token in EXPRESSION_UP + EXPRESSION_DOWN if token in normalized]
    # "表达逆转" is the project's own screening concept and is allowed in the
    # screening section; elsewhere it would imply a drug effect on expression.
    directional_effect = []
    if section != "screening_rationale":
        directional_effect = [token for token in DIRECTIONAL_EFFECT if token in normalized]
    if expression_terms or directional_effect:
        if directional_effect:
            return False, text, relation, "drug-induced expression reversal cannot be attributed to project data"
        if any(token in normalized for token in ("该药", "药物", "化合物", "给药", "处理")):
            return False, text, relation, "drug-subject expression direction cannot be verified"
        directions = {
            str((path_meta.get(path_id) or {}).get("s1_direction") or "")
            for path_id in path_ids
            if (path_meta.get(path_id) or {}).get("route_type") == "s1_deg_overlap"
        }
        directions.update(
            str((evidence_meta.get(evidence_id) or {}).get("s1_direction") or "")
            for evidence_id in evidence_ids
        )
        directions = {value for value in directions if value}
        if len(directions) != 1:
            return False, text, relation, "expression direction is not supported by exactly one S1 assertion"
        direction = directions.pop()
        if any(token in normalized for token in EXPRESSION_UP) and direction != "UP":
            return False, text, relation, "expression direction contradicts S1 assertion"
        if any(token in normalized for token in EXPRESSION_DOWN) and direction != "DOWN":
            return False, text, relation, "expression direction contradicts S1 assertion"
        if not any(word in normalized for word in S1_CONTEXT_WORDS):
            return False, text, relation, "expression direction cannot be attributed to the drug"

    suppress = any(token in normalized for token in SUPPRESS_PATTERNS)
    promote = any(token in normalized for token in PROMOTE_PATTERNS)
    if suppress and promote:
        return False, text, relation, "conflicting direction terms in one claim"
    if suppress and relation != "inhibition":
        if hypothesis:
            text = f"待验证假设：{text}"
        else:
            return False, text, relation, "inhibition claim is not supported by an inhibition relation"
    if promote and relation != "activation":
        if hypothesis:
            text = f"待验证假设：{text}"
        else:
            return False, text, relation, "activation claim is not supported by an activation relation"

    if causal:
        relation = "association"
        if hypothesis or section == "mechanism_hypotheses":
            text = f"待验证因果假设：{text}"
        else:
            text = f"待验证关联（因果表述已降级）：{text}"
    if any(pattern in text for pattern in OVERCLAIM_PATTERNS):
        text = f"待验证表述（已降级）：{text}"
    if len(text) > MAX_TEXT:
        text = text[: MAX_TEXT - 1] + "…"
    return True, text, relation, None


def validate_explanation(
    explanation: dict[str, Any], bundle: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    allowed_evidence = set(bundle.get("allowed_evidence_ids") or [])
    allowed_paths = set(bundle.get("allowed_path_ids") or [])
    route_types = {str(key): str(value) for key, value in (bundle.get("path_route_types") or {}).items()}
    evidence_layers = {str(key): str(value) for key, value in (bundle.get("evidence_layers") or {}).items()}
    path_relations = _path_relation_map(bundle)
    issues: list[dict[str, Any]] = []
    dropped = 0
    downgraded = 0
    layer_counts = {"A": 0, "B": 0, "C": 0, "D": 0}
    clean: dict[str, Any] = {}
    ignored_free_text = []
    for key in ("limitations", "validation_suggestions", "insufficient_evidence", "evidence_assessment"):
        if key in explanation:
            ignored_free_text.append(key)

    for section in CLAIM_SECTIONS:
        rules = SECTION_RULES[section]
        raw_claims = explanation.get(section)
        if not isinstance(raw_claims, list):
            if raw_claims is not None:
                issues.append({"section": section, "issue": "section is not a list; removed"})
            clean[section] = []
            continue
        accepted = []
        for index, claim in enumerate(raw_claims[:6]):
            if not isinstance(claim, dict):
                issues.append({"section": section, "index": index, "issue": "claim is not an object"})
                dropped += 1
                continue
            text = _clean_text(claim.get("text"))
            if text is None:
                issues.append({"section": section, "index": index, "issue": "missing text"})
                dropped += 1
                continue

            evidence_ids = _sanitize_ids(
                claim.get("evidence_ids"), allowed_evidence, field="evidence_ids", issues=issues, index=index, section=section
            )
            path_ids = _sanitize_ids(
                claim.get("path_ids"), allowed_paths, field="path_ids", issues=issues, index=index, section=section
            )
            raw_evidence = claim.get("evidence_ids") if isinstance(claim.get("evidence_ids"), list) else []
            raw_paths = claim.get("path_ids") if isinstance(claim.get("path_ids"), list) else []
            if len(evidence_ids) != len(raw_evidence) or len(path_ids) != len(raw_paths):
                # Never keep a claim after silently discarding an unverifiable ID.
                issues.append({"section": section, "index": index, "issue": "claim dropped: one or more citation IDs are not in the current evidence bundle"})
                dropped += 1
                continue
            if not evidence_ids and not path_ids:
                issues.append({"section": section, "index": index, "issue": "claim dropped: no verifiable citation"})
                dropped += 1
                continue

            kinds, routes, effective_paths, incompatibility = _citation_scope(evidence_ids, path_ids, bundle)
            if incompatibility:
                issues.append({"section": section, "index": index, "issue": incompatibility})
                dropped += 1
                continue
            invalid_kinds = kinds - rules["allowed_kinds"]
            invalid_routes = routes - rules["allowed_routes"] if rules["allowed_routes"] else routes
            if invalid_kinds or invalid_routes:
                issues.append(
                    {
                        "section": section,
                        "index": index,
                        "issue": "citation type does not support this section",
                        "invalid_kinds": sorted(invalid_kinds),
                        "invalid_routes": sorted(invalid_routes),
                    }
                )
                dropped += 1
                continue
            if not (kinds & rules["require_kinds"] or routes & rules["require_routes"]):
                issues.append({"section": section, "index": index, "issue": "section has no qualifying evidence type"})
                dropped += 1
                continue

            bound_genes: list[dict[str, Any]] = []
            if section == "target_mechanism":
                declared, gene_errors = _declared_gene_entities(claim, text, bundle)
                if gene_errors:
                    issues.append(
                        {
                            "section": section,
                            "index": index,
                            "issue": "unknown or invalid gene entity; claim dropped",
                            "gene_errors": gene_errors,
                        }
                    )
                    dropped += 1
                    continue
                text_genes = _genes_mentioned_in_text(text, _target_index(bundle)[0])
                declared_ids = {str(item.get("gene_id")) for item in declared if item.get("gene_id")}
                text_ids = {str(item.get("gene_id")) for item in text_genes if item.get("gene_id")}
                if text_ids and not text_ids <= declared_ids:
                    issues.append(
                        {
                            "section": section,
                            "index": index,
                            "issue": "text names a gene entity not declared for this claim; claim dropped",
                            "text_gene_ids": sorted(text_ids),
                            "declared_gene_ids": sorted(declared_ids),
                        }
                    )
                    dropped += 1
                    continue
                binding_errors = _target_gene_binding_errors(declared, evidence_ids, effective_paths, bundle)
                if binding_errors:
                    issues.append(
                        {
                            "section": section,
                            "index": index,
                            "issue": "target gene entity is not bound to the cited evidence; claim dropped",
                            "binding_errors": binding_errors,
                        }
                    )
                    dropped += 1
                    continue
                bound_genes = [
                    {"gene_id": item.get("gene_id"), "symbol": item.get("symbol")}
                    for item in declared
                ]

            relation = str(claim.get("relation", "unknown")).lower()
            if relation not in RELATIONS and relation != "causal":
                issues.append({"section": section, "index": index, "issue": "unknown relation downgraded", "relation": relation})
                relation = "unknown"
            if relation == "causal":
                issues.append({"section": section, "index": index, "issue": "causal relation downgraded"})
                downgraded += 1
            if relation in {"binding", "inhibition", "activation"}:
                target_paths = {
                    path_id
                    for path_id in effective_paths
                    if route_types.get(path_id) == "direct_target"
                }
                supported = bool(target_paths) and all(path_relations.get(path_id) == relation for path_id in target_paths)
                if not supported:
                    issues.append(
                        {"section": section, "index": index, "issue": "direction not supported by cited source; downgraded", "relation": relation}
                    )
                    relation = "association"
                    text = f"待审核方向性表述（来源未确认方向，按关联处理）：{text}"
                    downgraded += 1

            keep, text, relation, drop_reason = _guard_language(text, section, relation, effective_paths, evidence_ids, bundle)
            if not keep:
                issues.append({"section": section, "index": index, "issue": drop_reason})
                dropped += 1
                continue

            hypothesis = section == "mechanism_hypotheses"
            if hypothesis and not text.startswith(("假设", "待验证")):
                text = f"待验证假设：{text}"

            layer = _layer_for(list(effective_paths), evidence_ids, route_types, evidence_layers, hypothesis=hypothesis)
            declared = claim.get("layer")
            if declared != layer:
                issues.append(
                    {"section": section, "index": index, "issue": "layer recomputed from cited evidence", "declared": declared, "assigned": layer}
                )
            layer_counts[layer] += 1
            accepted.append(
                {
                    "text": text,
                    "relation": relation,
                    "layer": layer,
                    "evidence_ids": evidence_ids,
                    "path_ids": path_ids,
                    "genes": bound_genes,
                }
            )
        clean[section] = accepted

    model_verdict = None
    assessment = explanation.get("evidence_assessment")
    if isinstance(assessment, dict):
        model_verdict = assessment.get("verdict")
    audit = {
        "dropped_claims": dropped,
        "downgraded_claims": downgraded,
        "issues": issues,
        "layer_counts": layer_counts,
        "accepted_claims": sum(len(clean.get(section, [])) for section in CLAIM_SECTIONS),
        "ignored_free_text": ignored_free_text,
        "model_verdict": model_verdict,
    }
    return clean, issues, audit


def backend_coverage(bundle: dict[str, Any]) -> dict[str, Any]:
    """Deterministic evidence coverage label; not a scientific or clinical grade."""
    counts = bundle.get("counts") or {}
    mapped = bool((bundle.get("compound") or {}).get("mapped"))
    targets = counts.get("direct_target_paths", 0)
    s1_paths = counts.get("s1_enrichment_paths", 0)
    evidence = counts.get("unique_evidence", 0)
    if not mapped or (targets == 0 and counts.get("public_pathway_paths", 0) == 0 and s1_paths == 0):
        level, reason = "coverage_insufficient", "候选未映射到药物节点，或没有任何靶点/通路路径"
    elif targets >= 1 and s1_paths >= 1 and evidence >= 20:
        level, reason = "coverage_high", "存在直接靶点、S1 富集通路交汇且证据量较多"
    elif targets >= 1:
        level, reason = "coverage_limited", "存在靶点证据，但通路交汇或证据量不足"
    else:
        level, reason = "coverage_limited", "只有间接通路信息，缺少直接靶点证据"
    return {
        "coverage_level": level,
        "coverage_basis": f"{reason}（直接靶点路径 {targets}，S1 富集路径 {s1_paths}，唯一证据 {evidence}）",
        "coverage_note": "该标签只表示当前项目和知识图谱的证据覆盖程度，不代表药物疗效、临床证据等级或机制已被验证。",
        "direct_path_count": targets,
        "s1_enrichment_path_count": s1_paths,
        "public_pathway_path_count": counts.get("public_pathway_paths", 0),
        "unique_evidence_count": evidence,
        "missing": list(bundle.get("evidence_gaps") or []),
    }
