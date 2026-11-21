"""Deterministic KG07 rule-template service.

Stage 2A-1 deliberately implements only the deterministic ``kg_rule`` mode.
The legacy AI modes are kept as explicit not-implemented responses so that no
old Zhipu path or weak local validator can be reached accidentally.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from typing import Any, Mapping

from app.core.config import settings
from app.interpretation import provider, validator
from app.kg07_adapter import build_kg07_evidence

RULE_PROMPT_VERSION = "kg07-rule-stage2a1-v1"
AI_PROMPT_VERSION = "kg07-deepseek-flash-stage2a2-v1"
DEEPSEEK_FLASH_MODEL = "deepseek-flash"
MODE_NAMES = ("kg_rule", "kg_llm", "direct_llm")
SECTION_KEYS = (
    "screening_basis",
    "known_targets",
    "associated_pathways",
    "independent_validation",
    "mechanism_hypotheses",
    "insufficient_evidence",
    "limitations",
)

AI_SECTIONS = (
    "screening_rationale",
    "target_mechanism",
    "pathway_association",
    "mechanism_hypotheses",
)
AI_DIRECT_SECTIONS = ("screening_rationale",)
AI_CLAIM_KEYS = frozenset(
    {"text", "evidence_ids", "path_ids", "relation", "layer", "genes"}
)
AI_RELATIONS = frozenset({"binding", "inhibition", "activation", "association", "unknown"})
AI_LAYERS = frozenset({"A", "B", "C"})
AI_TEXT_MAX = 60
AI_MAX_CLAIMS_PER_SECTION = 3
AI_UNSAFE_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"131\s*(条|种|个)",
        r"99\s*(条|种|个)",
        r"21\s*(条|个)",
        r"3\s*个\s*独立",
        r"Level\s*5",
        r"独立药物实验",
        r"临床疗效",
        r"ER\+\s*(选择性|特异)",
        r"独立验证",
        r"PRISM",
        r"COMPZ",
        r"Exp010",
    )
)

AI_SYSTEM_PROMPT = """你是 ER-DReKG 项目 KG07 的证据解释助手。你的任务只是整理输入中的项目证据，不是筛选药物，也不是给出医学结论。

【只依据输入】
- 只能使用 user JSON 中出现的药物、基因、通路、条件、ID 和字段；不得使用外部知识、文献、数据库记忆或常识补全。
- 不得虚构靶点、基因、通路、证据 ID、路径 ID、实验、文献或数据库记录。
- 输入没有的内容不要写成事实；系统会根据证据缺口生成缺失说明。

【引用硬规则】
- 每个 claim 必须带非空 evidence_ids 或 path_ids；只能原样复制输入中出现的 ID。
- target_mechanism 只能使用 targets[].safe_target_path_ids 和 targets[].safe_target_evidence_ids；不得使用 S1 富集证据、S2 证据或通路证据。
- pathway_association 只能使用 pathways[].safe_pathway_path_id 和 pathways[].safe_pathway_evidence_ids；不得使用靶点证据或 S2 证据。
- mechanism_hypotheses 必须同时引用一个靶点安全证据和一个通路安全证据；缺少任一类时不要生成假设。
- screening_rationale 只有在 screening_available=true 时才能生成，并且只能引用 s2_conditions[].evidence_ids；screening_available=false 时该段必须为空。
- 如果同时引用 path_ids 和 evidence_ids，evidence_ids 必须确实属于所引用的 path_ids；不要复制路径内的全部 evidence_ids。
- 引用类型或 ID 不匹配时整条 claim 会被系统删除，不会保留其中一个看似合法的 ID。

【靶点实体绑定】
- target_mechanism 的每条 claim 必须用 genes 明确声明所陈述的基因实体，不能只引用任意一条靶点证据。
- genes 中的 gene_id/symbol 必须原样来自输入 targets；不要输出输入 targets 之外的基因。
- text 中出现的基因名必须与 genes 一致，不得额外出现未声明的基因。
- 一条 claim 只能陈述一个输入 target 的实体。

【关系、层与措辞】
- relation 只能是 binding / inhibition / activation / association / unknown。
- 只有输入 reported_actions 明确写出方向时才可用 inhibition 或 activation；否则用 association 或 unknown。
- layer：target 为 A，pathway 为 B，mechanism_hypotheses 为 C；不得把 C 标成 A 或 B。
- 禁止因果表述：导致、引起、驱动、造成、从而抑制、从而使、进而抑制、进而激活、介导。
- 禁止疗效表述：治愈、治疗、疗效、有效、临床有效、推荐使用、可用于治疗、能够治疗、杀伤、抑制肿瘤、改善症状、逆转耐药。
- 禁止“已证实 / 已经证明 / 必然 / 确定”等过度确定性措辞。
- 不得把 S1 基因上调或下调写成药物上调或下调该基因；S1 方向只描述疾病表达数据本身。
- 不得把关联写成激活或抑制机制；不得把记录数写成通路种数或机制数量。
- 页面负责展示完整统计；不要在 AI claim 中复述 131 条记录、99 条唯一通路、21 个注释/靶点或 3 个独立重复等完整计数。
- 独立验证、PRISM、COMPZ、Exp010 只由系统确定性展示；不要生成这些内容，也不要据此判断疗效。

【输出格式】
- 只输出一个 JSON 对象，不要 Markdown、不要代码块、不要解释文字。
- 只允许四个数组：screening_rationale、target_mechanism、pathway_association、mechanism_hypotheses。
- 不要输出 er_positive_relevance、limitations、validation_suggestions、insufficient_evidence、evidence_assessment 或其他字段。
- 每个数组最多 3 条 claim；每条 text 不超过 60 个汉字。
- claim = {"text": "...", "evidence_ids": ["..."], "path_ids": ["..."], "relation": "association", "layer": "A|B|C", "genes": [{"gene_id": "...", "symbol": "..."}]}
- target_mechanism 的 genes 必填；其他 section 不要输出 genes。
"""


AI_DIRECT_SYSTEM_PROMPT = AI_SYSTEM_PROMPT + """
【direct_llm 额外限制】
- 当前模式只允许输出 screening_rationale 数组。
- 不要输出 target_mechanism、pathway_association、mechanism_hypotheses 或其他任何 section。
- 每条 screening claim 只能引用输入 s2_conditions[].evidence_ids；不得引用靶点、通路或路径 ID。
"""


class KG07AIResponseError(ValueError):
    """Provider response, model lock, or AI structure failed validation."""


class KG07AIStructureError(ValueError):
    """The AI object does not match the agreed KG07 claim structure."""


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None and value != "":
            return value
    return None


def _empty_explanation() -> dict[str, list[Any]]:
    return {key: [] for key in SECTION_KEYS}


def _stringify(value: Any) -> str:
    if value is None:
        return "缺失"
    if isinstance(value, float):
        return f"{value:g}"
    return _text(value)


def _claim(
    text: str,
    *,
    evidence_ids: list[str] | None = None,
    path_ids: list[str] | None = None,
    relation: str = "association",
    genes: list[dict[str, str]] | None = None,
    display_only: bool = False,
    citation_reference_status: str = "deterministic_rule",
) -> dict[str, Any]:
    return {
        "text": text,
        "relation": relation,
        "evidence_ids": list(evidence_ids or []),
        "path_ids": list(path_ids or []),
        "genes": list(genes or []),
        "display_only": display_only,
        "citation_reference_status": citation_reference_status,
    }


def _base_response(
    package: Mapping[str, Any],
    *,
    mode: str,
    status: str,
    explanation: dict[str, list[Any]] | None = None,
    adapter: Mapping[str, Any] | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    candidate = _as_mapping(package.get("candidate"))
    provenance = _as_mapping(package.get("provenance"))
    adapter_metadata = _as_mapping(adapter.get("metadata")) if adapter else {}
    dimensions = adapter.get("dimensions") if adapter else None
    legacy = adapter.get("legacy_sections") if adapter else None
    graph_build_id = (
        _text(adapter.get("graph_build_id")) if adapter else ""
    ) or _text(package.get("graph_build_id"))
    return {
        "status": status,
        "mode": mode,
        "candidate": dict(candidate),
        "project_id": _text(package.get("project_id")),
        "pert_id": _text(package.get("pert_id")),
        "graph_build_id": graph_build_id,
        "package_id": _text(package.get("package_id")) or None,
        "adapter_version": adapter_metadata.get("adapter_version"),
        "validator_bundle_sha256": adapter_metadata.get("validator_bundle_sha256"),
        "evidence_input_sha256": provenance.get("evidence_input_sha256"),
        "coverage": dict(_as_mapping(package.get("coverage"))),
        "evidence_gaps": list(_as_list(package.get("evidence_gaps"))),
        "missing_fields": list(adapter_metadata.get("missing_fields", [])),
        "full_counts": (
            dict(_as_mapping(adapter.get("validator_bundle")).get("counts", {}))
            if adapter
            else None
        ),
        "scoped_counts": (
            dict(_as_mapping(adapter.get("llm_validator_bundle")).get("counts", {}))
            if adapter
            else None
        ),
        "dimensions": dimensions,
        "dimensions_status": "available" if dimensions is not None else "not_available",
        "legacy_skeleton": legacy,
        "explanation": explanation or _empty_explanation(),
        "prompt_version": RULE_PROMPT_VERSION if mode == "kg_rule" else "not_implemented",
        "model": None,
        "cached": False,
        "latency_seconds": 0.0,
        "usage": {},
        "provider_cost": None,
        "provider_cost_status": "not_requested_deterministic_rule",
        "citation_validation_issues": [],
        "citation_validation_pass": False,
        "citation_reference_validation_pass": False,
        "layer_validation_pass": False,
        "citation_reference_status": (
            "not_run_deterministic_rule" if mode == "kg_rule" else "not_implemented"
        ),
        "semantic_entailment_status": (
            "not_run_deterministic_rule" if mode == "kg_rule" else "not_implemented"
        ),
        "human_review_required": True,
        "message": message,
    }


def _assert_expected_build(
    package: Mapping[str, Any], expected_graph_build_id: str | None
) -> None:
    expected = _text(expected_graph_build_id)
    if not expected:
        return
    actual = _text(package.get("graph_build_id"))
    if actual != expected:
        raise ValueError(
            "KG06 package GraphBuild does not match the resolved production GraphBuild: "
            f"package={actual or '<missing>'} expected={expected}"
        )


def _s2_screening_claims(adapter: Mapping[str, Any]) -> list[dict[str, Any]]:
    dose = _as_mapping(_as_mapping(adapter.get("dimensions")).get("dose_consistency"))
    conditions = [
        _as_mapping(row) for row in _as_list(dose.get("conditions"))
    ]
    with_ids = [row for row in conditions if _as_list(row.get("evidence_ids"))]
    if not with_ids:
        return []
    evidence_ids = sorted(
        {
            _text(evidence_id)
            for row in with_ids
            for evidence_id in _as_list(row.get("evidence_ids"))
            if _text(evidence_id)
        }
    )
    metrics = _as_mapping(dose.get("metrics"))
    negative_doses = sorted(
        {_text(row.get("dose")) for row in with_ids if _text(row.get("direction")) == "DOWN"}
    )
    text = (
        f"当前 {len(conditions)} 条 S2 条件中，{len(with_ids)} 条带可核验 Evidence ID；"
        f"负向剂量为 {', '.join(negative_doses) if negative_doses else '无'}。"
        "WTCS 是表达逆转关联分数，不代表临床疗效或因果机制。"
    )
    return [
        _claim(
            text,
            evidence_ids=evidence_ids,
            relation="association",
        )
    ]


def _target_claims(adapter: Mapping[str, Any]) -> list[dict[str, Any]]:
    target = _as_mapping(_as_mapping(adapter.get("dimensions")).get("target_coverage"))
    metrics = _as_mapping(target.get("metrics"))
    annotations = [_as_mapping(row) for row in _as_list(target.get("annotations"))]
    annotation_count = metrics.get("annotation_count")
    unique_count = metrics.get("unique_target_gene_count")
    primary = _as_mapping(metrics.get("primary_action_counts"))
    action_text = "、".join(
        f"{action} {_stringify(primary.get(action))}"
        for action in ("TARGET", "ENZYME", "TRANSPORTER", "UNKNOWN")
        if primary.get(action) not in (None, 0, "0")
    )
    claims: list[dict[str, Any]] = [
        _claim(
            f"当前包含 {_stringify(annotation_count)} 条药物—基因注释，"
            f"对应 {_stringify(unique_count)} 个唯一基因；"
            f"动作主分类为 {action_text or '无可用分类'}。"
            "注释数量不等同于独立靶点数量。",
            display_only=True,
            citation_reference_status="deterministic_rule_count",
        )
    ]
    for row in annotations:
        symbol = _text(row.get("symbol")) or _text(row.get("gene_id")) or "未命名基因"
        gene_id = _text(row.get("gene_id"))
        primary_action = _text(row.get("primary_action")) or "UNKNOWN"
        reported_actions = [
            _text(value) for value in _as_list(row.get("reported_actions")) if _text(value)
        ]
        actions = "、".join(reported_actions) if reported_actions else primary_action
        claims.append(
            _claim(
                f"药物—基因注释：{symbol}；主分类 {primary_action}；"
                f"注释动作 {actions}。这是一条注释，不是独立靶点。",
                evidence_ids=[
                    _text(value) for value in _as_list(row.get("evidence_ids")) if _text(value)
                ],
                path_ids=[_text(row.get("path_id"))] if _text(row.get("path_id")) else [],
                relation="association",
                genes=(
                    [{"gene_id": gene_id, "symbol": symbol}]
                    if gene_id or symbol
                    else []
                ),
            )
        )
    return claims


def _pathway_claims(adapter: Mapping[str, Any]) -> list[dict[str, Any]]:
    pathway = _as_mapping(
        _as_mapping(adapter.get("dimensions")).get("s1_pathway_association")
    )
    metrics = _as_mapping(pathway.get("metrics"))
    representatives = [
        _as_mapping(row) for row in _as_list(pathway.get("representatives"))
    ]
    record_count = metrics.get("record_count")
    unique_count = metrics.get("unique_pathway_count")
    representative_count = metrics.get("representative_path_count")
    claims: list[dict[str, Any]] = [
        _claim(
            f"S1 富集关联记录 {_stringify(record_count)} 条，"
            f"对应 {_stringify(unique_count)} 条唯一通路；"
            f"本次仅展示 {_stringify(representative_count)} 条代表通路。"
            "记录数不代表通路种数或机制数量。",
            display_only=True,
            citation_reference_status="deterministic_rule_count",
        )
    ]
    for row in representatives:
        label = (
            _text(row.get("pathway_label"))
            or _text(row.get("pathway_id"))
            or "未命名通路"
        )
        path_id = _text(row.get("path_id"))
        claims.append(
            _claim(
                f"代表通路：{label}；这是靶点—通路关联证据，"
                "不代表激活或抑制机制，也不代表因果关系。",
                evidence_ids=[
                    _text(value) for value in _as_list(row.get("evidence_ids")) if _text(value)
                ],
                path_ids=[path_id] if path_id else [],
                relation="association",
            )
        )
    return claims


def _independent_claims(adapter: Mapping[str, Any]) -> list[dict[str, Any]]:
    independent = _as_mapping(
        _as_mapping(adapter.get("dimensions")).get("independent_validation")
    )
    metrics = _as_mapping(independent.get("metrics"))
    if not metrics.get("available"):
        return []
    exp010 = _as_mapping(metrics.get("exp010"))
    compz = _as_mapping(metrics.get("compz"))
    prism = _as_mapping(metrics.get("prism"))
    caveats = [_text(value) for value in _as_list(independent.get("caveats")) if _text(value)]
    text = (
        "独立验证仅作确定性展示："
        f"Exp010 外部 S1 中位排名 {_stringify(exp010.get('external_s1_median_rank'))}；"
        f"COMPZ Q2 中位排名 {_stringify(compz.get('Q2_condition_median_rank'))}；"
        f"PRISM ER+ 平均敏感性 {_stringify(prism.get('erpos_mean_sensitivity'))}，"
        f"ER− 平均敏感性 {_stringify(prism.get('erneg_mean_sensitivity'))}。"
    )
    if caveats:
        text += "限制：" + "；".join(caveats)
    return [
        _claim(
            text,
            display_only=True,
            citation_reference_status="deterministic_display_only_no_evidence_id",
        )
    ]


def _insufficient_evidence(
    package: Mapping[str, Any], adapter: Mapping[str, Any]
) -> list[dict[str, Any]]:
    gaps: list[str] = []
    missing = [
        _text(value)
        for value in _as_list(_as_mapping(adapter.get("metadata")).get("missing_fields"))
        if _text(value)
    ]
    if "s2_condition_evidence_ids" in missing or any(
        "s2_condition_evidence_ids" in value for value in missing
    ):
        gaps.append(
            "S2 条件没有可核验的 EvidenceRecord ID，不能形成可引用的筛选结论。"
        )
    if "signature_coverage.signature_level" in missing:
        gaps.append("未提供 signature_level，不推断 Level5。")
    gaps.extend(
        _text(value) for value in _as_list(package.get("evidence_gaps")) if _text(value)
    )
    for value in missing:
        if value not in {"s2_condition_evidence_ids", "signature_coverage.signature_level"}:
            gaps.append(f"证据缺口字段：{value}")
    seen: set[str] = set()
    claims: list[dict[str, Any]] = []
    for gap in gaps:
        if gap in seen:
            continue
        seen.add(gap)
        claims.append(
            _claim(
                gap,
                display_only=True,
                citation_reference_status="deterministic_gap",
            )
        )
    return claims


def _limitations(adapter: Mapping[str, Any]) -> list[str]:
    dimensions = _as_mapping(adapter.get("dimensions"))
    signature = _as_mapping(dimensions.get("signature_coverage")).get("metrics")
    signature = _as_mapping(signature)
    dose = _as_mapping(dimensions.get("dose_consistency")).get("metrics")
    dose = _as_mapping(dose)
    target = _as_mapping(dimensions.get("target_coverage")).get("metrics")
    target = _as_mapping(target)
    pathway = _as_mapping(dimensions.get("s1_pathway_association")).get("metrics")
    pathway = _as_mapping(pathway)
    independent = _as_mapping(dimensions.get("independent_validation"))
    limitations: list[str] = []
    if signature.get("n_signatures") == 1:
        limitations.append(
            "主条件只有 1 个 consensus signature；"
            f"replicates_median={_stringify(signature.get('replicates_median'))} "
            "是实例折叠计数，不是独立生物学重复。"
        )
    if signature.get("n_signatures") is not None:
        limitations.append(
            f"当前 signature 数：{_stringify(signature.get('n_signatures'))}；"
            "不得表述为多个独立生物学重复。"
        )
    if signature.get("signature_level") is None:
        limitations.append("未提供 signature_level，不推断 Level5。")
    if dose:
        limitations.append(
            f"剂量一致性：{_stringify(dose.get('condition_count'))} 个剂量条件，"
            f"负向 {_stringify(dose.get('negative_condition_count'))} 个，"
            f"负向剂量比例为 {_stringify(dose.get('negative_dose_fraction'))}；"
            "方向不稳定时不得表述为剂量依赖。"
        )
    if target:
        limitations.append(
            f"药物—基因注释 {_stringify(target.get('annotation_count'))} 条，"
            f"唯一基因 {_stringify(target.get('unique_target_gene_count'))} 个；"
            "注释数量不是独立靶点数量。"
        )
    if pathway:
        limitations.append(
            f"S1 富集关联记录 {_stringify(pathway.get('record_count'))} 条，"
            f"唯一通路 {_stringify(pathway.get('unique_pathway_count'))} 条；"
            "关联记录数不等于唯一通路数或机制数量。"
        )
    for caveat in _as_list(independent.get("caveats")):
        text = _text(caveat)
        if text:
            limitations.append(text)
    limitations.append("本结果来自确定性规则模板，未调用 AI，不构成因果或临床结论。")
    return limitations


def _adapter_response(
    package: Mapping[str, Any],
    adapter: Mapping[str, Any],
    *,
    status: str = "rule_template",
) -> dict[str, Any]:
    explanation = _empty_explanation()
    explanation["screening_basis"] = _s2_screening_claims(adapter)
    explanation["known_targets"] = _target_claims(adapter)
    explanation["associated_pathways"] = _pathway_claims(adapter)
    explanation["independent_validation"] = _independent_claims(adapter)
    explanation["mechanism_hypotheses"] = []
    explanation["insufficient_evidence"] = _insufficient_evidence(package, adapter)
    explanation["limitations"] = _limitations(adapter)
    return _base_response(
        package,
        mode="kg_rule",
        status=status,
        explanation=explanation,
        adapter=adapter,
    )


def _empty_paths_response(
    package: Mapping[str, Any],
    error: str,
) -> dict[str, Any]:
    explanation = _empty_explanation()
    explanation["insufficient_evidence"] = [
        _claim(
            "当前证据包没有 target/pathway paths；不创建路径、靶点或通路，"
            "也不生成机制解释。",
            display_only=True,
            citation_reference_status="deterministic_gap",
        ),
        _claim(
            error,
            display_only=True,
            citation_reference_status="deterministic_gap",
        ),
    ]
    explanation["limitations"] = [
        "当前候选没有可用的 target/pathway 路径；机制解释为空。",
        "本结果来自确定性规则模板，未调用 AI，不构成因果或临床结论。",
    ]
    response = _base_response(
        package,
        mode="kg_rule",
        status="rule_template",
        explanation=explanation,
        message="KG06 package has no paths; deterministic empty-mechanism fallback used.",
    )
    response["degraded"] = True
    response["dimensions_status"] = "not_available_no_paths"
    return response


def rule_template(
    package: Mapping[str, Any],
    *,
    expected_graph_build_id: str | None = None,
) -> dict[str, Any]:
    """Build the legacy six-section deterministic KG07 response.

    The adapter remains the single owner of the five dimensions and the full
    versus scoped evidence split.  If a package has no paths, no path or target
    is invented; the service returns an explicit empty-mechanism response.
    """

    _assert_expected_build(package, expected_graph_build_id)
    try:
        adapter = build_kg07_evidence(
            package,
            expected_graph_build_id=expected_graph_build_id,
        )
    except ValueError as exc:
        if _text(exc) == "KG06 package has no paths":
            return _empty_paths_response(package, _text(exc))
        raise
    return _adapter_response(package, adapter)


def _api_key_configured() -> bool:
    return bool(_text(settings.deepseek_api_key) or _text(os.getenv("DEEPSEEK_API_KEY")))


def _assert_flash_config() -> None:
    configured = _text(settings.deepseek_model)
    if configured.lower() != DEEPSEEK_FLASH_MODEL:
        raise provider.ProviderConfigError(
            "KG07 requires DeepSeek Flash; configured model is "
            f"{configured or '<missing>'}"
        )


def _assert_metadata_model(metadata: Mapping[str, Any]) -> None:
    actual = _text(metadata.get("model"))
    if actual.lower() != DEEPSEEK_FLASH_MODEL:
        raise KG07AIResponseError(
            "DeepSeek metadata.model does not match locked model "
            f"{DEEPSEEK_FLASH_MODEL}: {actual or '<missing>'}"
        )


def _not_configured_ai_response(package: Mapping[str, Any], mode: str) -> dict[str, Any]:
    explanation = _empty_explanation()
    explanation["limitations"] = [
        "当前环境未配置 DeepSeek API key；KG07 AI 模式不可用。",
        "本次未调用任何模型，不使用旧 Zhipu 接口或旧弱校验器。",
    ]
    response = _base_response(
        package,
        mode=mode,
        status="not_implemented",
        explanation=explanation,
        message=(
            f"KG07 mode {mode}: DeepSeek API key is not configured; "
            "no provider call was made."
        ),
    )
    response["dimensions_status"] = "not_built_for_unconfigured_ai_mode"
    response["prompt_version"] = "not_implemented"
    response["provider_cost_status"] = "not_requested_no_api_key"
    response["citation_reference_status"] = "not_configured"
    response["semantic_entailment_status"] = "not_run_not_configured"
    response["ai_called"] = False
    return response


def _empty_paths_ai_response(
    package: Mapping[str, Any], mode: str, error: str
) -> dict[str, Any]:
    response = _empty_paths_response(package, error)
    response["mode"] = mode
    response["status"] = "coverage_insufficient"
    response["message"] = (
        "KG06 package has no paths; deterministic empty-mechanism fallback used; "
        "AI was not called."
    )
    response["prompt_version"] = "not_called_no_paths"
    response["provider_cost_status"] = "not_requested_no_paths"
    response["citation_reference_status"] = "not_called_no_paths"
    response["semantic_entailment_status"] = "not_run_no_paths"
    response["ai_called"] = False
    response["dimensions_status"] = "not_available_no_paths"
    return response


def _s2_conditions(bundle: Mapping[str, Any]) -> list[dict[str, Any]]:
    conditions = _as_mapping(bundle.get("s2_conditions")).get("conditions")
    return [_as_mapping(row) for row in _as_list(conditions)]


def _s2_evidence_ids(bundle: Mapping[str, Any]) -> list[str]:
    return sorted(
        {
            _text(evidence_id)
            for row in _s2_conditions(bundle)
            for evidence_id in _as_list(row.get("evidence_ids"))
            if _text(evidence_id)
        }
    )


def _screening_available(bundle: Mapping[str, Any]) -> bool:
    return bool(_s2_evidence_ids(bundle))


def _evidence_belongs_to_path(
    evidence_id: str,
    path_id: str,
    evidence_meta: Mapping[str, Any],
    path_meta: Mapping[str, Any],
) -> bool:
    evidence = _as_mapping(evidence_meta.get(evidence_id))
    path = _as_mapping(path_meta.get(path_id))
    return (
        evidence_id in _as_list(path.get("evidence_ids"))
        and path_id in _as_list(evidence.get("path_ids"))
    )


def _target_citation_hints(bundle: Mapping[str, Any]) -> list[dict[str, Any]]:
    evidence_meta = _as_mapping(bundle.get("evidence_meta"))
    path_meta = _as_mapping(bundle.get("path_meta"))
    hints: list[dict[str, Any]] = []
    for raw_target in _as_list(bundle.get("targets")):
        target = _as_mapping(raw_target)
        gene_id = _text(target.get("gene_id"))
        symbol = _text(target.get("symbol")) or gene_id
        direct_paths = [
            _text(path_id)
            for path_id in _as_list(target.get("path_ids"))
            if _text(path_id)
            and _text(_as_mapping(path_meta.get(_text(path_id))).get("route_type"))
            == "direct_target"
            and _text(_as_mapping(path_meta.get(_text(path_id))).get("target_gene_id"))
            == gene_id
        ]
        safe_evidence: list[str] = []
        for path_id in direct_paths:
            for evidence_id in _as_list(_as_mapping(path_meta.get(path_id)).get("evidence_ids")):
                evidence_id = _text(evidence_id)
                meta = _as_mapping(evidence_meta.get(evidence_id))
                routes = {_text(route) for route in _as_list(meta.get("route_types"))}
                target_genes = {
                    _text(gene.get("gene_id"))
                    for gene in _as_list(meta.get("target_genes"))
                    if isinstance(gene, Mapping) and _text(gene.get("gene_id"))
                }
                if (
                    _text(meta.get("kind")) == "target"
                    and "direct_target" in routes
                    and "s1_enrichment_overlap" not in routes
                    and target_genes == {gene_id}
                    and _evidence_belongs_to_path(
                        evidence_id, path_id, evidence_meta, path_meta
                    )
                ):
                    safe_evidence.append(evidence_id)
        hints.append(
            {
                "gene_id": gene_id,
                "symbol": symbol,
                "reported_actions": [
                    _text(value)
                    for value in _as_list(target.get("reported_actions"))
                    if _text(value)
                ],
                "relation": _text(target.get("relation")) or "association",
                "safe_target_path_ids": sorted(set(direct_paths)),
                "safe_target_evidence_ids": sorted(set(safe_evidence)),
            }
        )
    return hints


def _pathway_citation_hints(bundle: Mapping[str, Any]) -> list[dict[str, Any]]:
    evidence_meta = _as_mapping(bundle.get("evidence_meta"))
    path_meta = _as_mapping(bundle.get("path_meta"))
    hints: list[dict[str, Any]] = []
    for raw_pathway in _as_list(bundle.get("pathways")):
        pathway = _as_mapping(raw_pathway)
        path_id = _text(pathway.get("path_id"))
        path = _as_mapping(path_meta.get(path_id))
        route = _text(path.get("route_type"))
        if route not in {"s1_enrichment_overlap", "public_pathway"}:
            continue
        safe_evidence: list[str] = []
        for evidence_id in _as_list(path.get("evidence_ids")):
            evidence_id = _text(evidence_id)
            meta = _as_mapping(evidence_meta.get(evidence_id))
            routes = {_text(route_name) for route_name in _as_list(meta.get("route_types"))}
            if (
                _text(meta.get("kind")) == "pathway"
                and route in routes
                and _evidence_belongs_to_path(
                    evidence_id, path_id, evidence_meta, path_meta
                )
            ):
                safe_evidence.append(evidence_id)
        hints.append(
            {
                "pathway_id": _text(pathway.get("pathway_id")),
                "pathway_label": _text(pathway.get("pathway_label"))
                or _text(pathway.get("pathway_id")),
                "route_type": route,
                "safe_pathway_path_id": path_id,
                "safe_pathway_evidence_ids": sorted(set(safe_evidence)),
            }
        )
    return hints


def _bounded_ai_payload(adapter: Mapping[str, Any], *, mode: str) -> dict[str, Any]:
    scoped = _as_mapping(adapter.get("llm_validator_bundle"))
    target_hints = _target_citation_hints(scoped)
    pathway_hints = _pathway_citation_hints(scoped)
    conditions = [
        row for row in _s2_conditions(scoped) if _as_list(row.get("evidence_ids"))
    ]
    screening_available = bool(conditions)
    payload: dict[str, Any] = {
        "payload_version": "kg07-ai-payload-stage2a2-v1",
        "graph_build_id": _text(adapter.get("graph_build_id")),
        "candidate": {
            key: value
            for key, value in _as_mapping(
                _as_mapping(adapter.get("llm_input")).get("candidate")
            ).items()
            if key in {"compound_id", "drug_id", "drug_name", "pert_id", "mapping_status"}
        },
        "signature_coverage": _as_mapping(
            _as_mapping(adapter.get("llm_input")).get("signature_coverage")
        ),
        "screening_available": screening_available,
        "s2_conditions": [
            {
                "dose": _text(row.get("dose")),
                "direction": _text(row.get("direction")),
                "WTCS_median": row.get("WTCS_median"),
                "evidence_ids": [
                    _text(value)
                    for value in _as_list(row.get("evidence_ids"))
                    if _text(value)
                ],
            }
            for row in conditions
        ] if screening_available else [],
        "targets": target_hints,
        "pathways": pathway_hints,
        "hypothesis_available": bool(
            any(row["safe_target_evidence_ids"] for row in target_hints)
            and any(row["safe_pathway_evidence_ids"] for row in pathway_hints)
        ),
        "constraints": {
            "no_efficacy_claim": True,
            "no_causal_claim": True,
            "no_target_or_pathway_invention": True,
            "no_annotation_count_as_target_count": True,
            "no_full_counts_in_ai_text": True,
            "page_owns_full_statistics": True,
            "screening_only_if_ids": True,
        },
    }
    if mode == "direct_llm":
        payload["targets"] = []
        payload["pathways"] = []
        payload["hypothesis_available"] = False
    return payload


def _build_ai_messages(
    payload: Mapping[str, Any], *, mode: str = "kg_llm"
) -> list[dict[str, str]]:
    system_prompt = AI_DIRECT_SYSTEM_PROMPT if mode == "direct_llm" else AI_SYSTEM_PROMPT
    return [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        },
    ]


def _call_provider(
    messages: list[dict[str, str]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    _assert_flash_config()
    try:
        raw, metadata = provider.chat_json(
            messages, max_tokens=settings.deepseek_max_tokens
        )
    except provider.ProviderAuthError:
        # AuthError subclasses RequestError; it must remain a RuntimeError so
        # main.py can map it to 503 rather than 502.
        raise
    except provider.ProviderConfigError:
        raise
    except provider.ProviderResponseError as exc:
        raise KG07AIResponseError(f"DeepSeek response is unusable: {exc}") from exc
    except provider.ProviderRequestError as exc:
        raise KG07AIResponseError(f"DeepSeek request failed: {exc}") from exc
    if not isinstance(raw, dict):
        raise KG07AIResponseError("DeepSeek returned a non-object JSON payload")
    if not isinstance(metadata, Mapping):
        raise KG07AIResponseError("DeepSeek returned invalid metadata")
    _assert_metadata_model(metadata)
    return raw, dict(metadata)


def _check_ai_structure(
    raw: Any,
    *,
    mode: str,
    screening_available: bool,
) -> tuple[dict[str, list[dict[str, Any]]], bool]:
    if not isinstance(raw, Mapping):
        raise KG07AIStructureError("AI output must be a JSON object")
    allowed_sections = AI_DIRECT_SECTIONS if mode == "direct_llm" else AI_SECTIONS
    unknown = set(raw) - set(allowed_sections)
    if unknown:
        raise KG07AIStructureError(
            "AI output contains unsupported sections: " + ", ".join(sorted(unknown))
        )
    suppressed = False
    if not screening_available and _as_list(raw.get("screening_rationale")):
        suppressed = True
    normalized: dict[str, list[dict[str, Any]]] = {}
    for section in allowed_sections:
        if section == "screening_rationale" and not screening_available:
            normalized[section] = []
            continue
        claims = raw.get(section, [])
        if not isinstance(claims, list):
            raise KG07AIStructureError(f"{section} must be a list")
        if len(claims) > AI_MAX_CLAIMS_PER_SECTION:
            raise KG07AIStructureError(
                f"{section} has more than {AI_MAX_CLAIMS_PER_SECTION} claims"
            )
        normalized_claims: list[dict[str, Any]] = []
        for index, claim in enumerate(claims):
            if not isinstance(claim, Mapping):
                raise KG07AIStructureError(f"{section}[{index}] must be an object")
            extra = set(claim) - AI_CLAIM_KEYS
            if extra:
                raise KG07AIStructureError(
                    f"{section}[{index}] has unsupported keys: "
                    + ", ".join(sorted(extra))
                )
            text = claim.get("text")
            if not isinstance(text, str) or not _text(text):
                raise KG07AIStructureError(f"{section}[{index}] has empty text")
            text = _text(text)
            if len(text) > AI_TEXT_MAX:
                raise KG07AIStructureError(
                    f"{section}[{index}] text exceeds {AI_TEXT_MAX} characters"
                )
            evidence_ids = claim.get("evidence_ids", [])
            path_ids = claim.get("path_ids", [])
            if not isinstance(evidence_ids, list) or not isinstance(path_ids, list):
                raise KG07AIStructureError(
                    f"{section}[{index}] citations must be lists"
                )
            if not all(isinstance(value, str) and _text(value) for value in evidence_ids):
                raise KG07AIStructureError(
                    f"{section}[{index}] has invalid evidence_ids"
                )
            if not all(isinstance(value, str) and _text(value) for value in path_ids):
                raise KG07AIStructureError(
                    f"{section}[{index}] has invalid path_ids"
                )
            if not evidence_ids and not path_ids:
                raise KG07AIStructureError(
                    f"{section}[{index}] must cite evidence_ids or path_ids"
                )
            relation = _text(claim.get("relation"))
            if relation not in AI_RELATIONS:
                raise KG07AIStructureError(
                    f"{section}[{index}] relation is not allowed: {relation or '<missing>'}"
                )
            layer = _text(claim.get("layer"))
            if layer not in AI_LAYERS:
                raise KG07AIStructureError(
                    f"{section}[{index}] layer is not allowed: {layer or '<missing>'}"
                )
            genes = claim.get("genes")
            if section == "target_mechanism":
                if not isinstance(genes, list) or not genes:
                    raise KG07AIStructureError(
                        f"{section}[{index}] must declare genes"
                    )
                normalized_genes: list[dict[str, str]] = []
                for gene in genes:
                    if not isinstance(gene, Mapping):
                        raise KG07AIStructureError(
                            f"{section}[{index}] gene must be an object"
                        )
                    if set(gene) - {"gene_id", "symbol"}:
                        raise KG07AIStructureError(
                            f"{section}[{index}] gene has unsupported keys"
                        )
                    gene_id = _text(gene.get("gene_id"))
                    symbol = _text(gene.get("symbol"))
                    if not gene_id and not symbol:
                        raise KG07AIStructureError(
                            f"{section}[{index}] gene has neither gene_id nor symbol"
                        )
                    normalized_genes.append({"gene_id": gene_id, "symbol": symbol})
                genes = normalized_genes
            elif genes:
                raise KG07AIStructureError(
                    f"{section}[{index}] must not declare genes"
                )
            normalized_claims.append(
                {
                    "text": text,
                    "evidence_ids": [_text(value) for value in evidence_ids],
                    "path_ids": [_text(value) for value in path_ids],
                    "relation": relation,
                    "layer": layer,
                    "genes": genes if section == "target_mechanism" else [],
                }
            )
        normalized[section] = normalized_claims
    return normalized, suppressed


def _unsafe_ai_claim_reason(text: str, section: str) -> str | None:
    if section != "screening_rationale" and re.search(
        r"(?i)\bS2\b|筛选证据|剂量条件|signature|表达签名", text
    ):
        return (
            "claim dropped: S2/signature/dose evidence cannot support a "
            f"{section} claim"
        )
    for pattern in AI_UNSAFE_PATTERNS:
        if pattern.search(text):
            return (
                "claim dropped: full-count, independent-validation, or "
                "mechanism-equivalence wording is not allowed in AI text"
            )
    return None


def _sanitize_unsafe_ai_claims(
    explanation: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    issues: list[dict[str, Any]] = []
    sanitized: dict[str, list[dict[str, Any]]] = {}
    for section, claims in explanation.items():
        kept: list[dict[str, Any]] = []
        for index, claim in enumerate(claims):
            reason = _unsafe_ai_claim_reason(_text(claim.get("text")), section)
            if reason:
                issues.append(
                    {"section": section, "index": index, "issue": reason}
                )
                continue
            kept.append(claim)
        sanitized[section] = kept
    return sanitized, issues


_VALIDATOR_REFERENCE_MARKERS = (
    "no verifiable citation",
    "one or more citation ids are not in the current evidence bundle",
    "cited evidence_id is not part of the cited path_id",
    "unknown evidence_ids removed",
    "unknown path_ids removed",
)


def _classify_validator_issue(issue: Mapping[str, Any]) -> str:
    text = _text(issue.get("issue")).lower()
    if "claim is not an object" in text or "missing text" in text or "section is not" in text:
        return "structure"
    if any(marker in text for marker in _VALIDATOR_REFERENCE_MARKERS):
        return "reference"
    if "citation type" in text or "qualifying evidence" in text:
        return "citation_type"
    if "gene entity" in text or "binding" in text:
        return "gene_binding"
    if "layer recomputed" in text:
        return "layer"
    return "language_or_relation"


def _issue_categories(issues: list[dict[str, Any]]) -> dict[str, int]:
    categories: Counter[str] = Counter()
    for issue in issues:
        categories[_classify_validator_issue(issue)] += 1
    return dict(categories)


def _annotate_clean_claims(
    claims: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    annotated: list[dict[str, Any]] = []
    for claim in claims:
        item = dict(claim)
        item["display_only"] = False
        item["citation_reference_status"] = "top10_validator_checked"
        annotated.append(item)
    return annotated


def _deterministic_ai_sections(
    package: Mapping[str, Any], adapter: Mapping[str, Any], mode: str
) -> dict[str, list[Any]]:
    explanation = _empty_explanation()
    if mode == "kg_llm":
        explanation["known_targets"] = _target_claims(adapter)[:1]
        explanation["associated_pathways"] = _pathway_claims(adapter)[:1]
    explanation["independent_validation"] = _independent_claims(adapter)
    explanation["mechanism_hypotheses"] = []
    explanation["insufficient_evidence"] = _insufficient_evidence(package, adapter)
    limitations = [
        value
        for value in _limitations(adapter)
        if "确定性规则模板" not in value
    ]
    limitations.append(
        "AI 解释仅在通过 Top10 Validator 后展示；未通过验证的文本不会显示。"
    )
    explanation["limitations"] = limitations
    return explanation


def _full_coverage(adapter: Mapping[str, Any]) -> dict[str, Any]:
    full = _as_mapping(adapter.get("validator_bundle"))
    if full.get("scope") != "full" or full.get("coverage_eligible") is not True:
        raise ValueError("KG07 coverage requires the full validator bundle")
    return validator.backend_coverage(dict(full))


def _direct_validator_bundle(adapter: Mapping[str, Any]) -> dict[str, Any] | None:
    scoped = _as_mapping(adapter.get("llm_validator_bundle"))
    s2_payload = _as_mapping(scoped.get("s2_conditions"))
    validation = _as_mapping(s2_payload.get("validation"))
    if validation.get("valid") is not True:
        return None
    conditions = [
        dict(row)
        for row in _as_list(s2_payload.get("conditions"))
        if _as_list(row.get("evidence_ids"))
    ]
    evidence_ids = sorted(
        {
            _text(evidence_id)
            for row in conditions
            for evidence_id in _as_list(row.get("evidence_ids"))
            if _text(evidence_id)
        }
    )
    if not evidence_ids:
        return None

    scoped_evidence_meta = _as_mapping(scoped.get("evidence_meta"))
    allowed_evidence_ids = {
        _text(evidence_id) for evidence_id in _as_list(scoped.get("allowed_evidence_ids"))
    }
    restricted_evidence_meta: dict[str, dict[str, Any]] = {}
    for evidence_id in evidence_ids:
        meta = _as_mapping(scoped_evidence_meta.get(evidence_id))
        if (
            not meta
            or meta.get("kind") != "s2"
            or evidence_id not in allowed_evidence_ids
        ):
            # An ID that cannot be backed by the authenticated scoped bundle
            # invalidates the entire direct call rather than being dropped.
            return None
        restricted_evidence_meta[evidence_id] = dict(meta)

    full = _as_mapping(adapter.get("validator_bundle"))
    return {
        "bundle_version": "kg07-direct-validator-bundle-stage2a31",
        "source_package_version": full.get("source_package_version"),
        "project_id": full.get("project_id"),
        "graph_build_id": full.get("graph_build_id"),
        "pert_id": full.get("pert_id"),
        "candidate": full.get("candidate") or {},
        "compound": full.get("compound") or {},
        "counts": {
            "s2_conditions": len(conditions),
            "unique_evidence": len(evidence_ids),
        },
        "targets": [],
        "pathways": [],
        "s2_conditions": {"available": True, "conditions": conditions},
        "path_route_types": {},
        "path_meta": {},
        "evidence_meta": restricted_evidence_meta,
        "evidence_layers": {evidence_id: "A" for evidence_id in evidence_ids},
        "allowed_evidence_ids": evidence_ids,
        "allowed_path_ids": [],
        "total_path_ids": [],
        "evidence_gaps": list(_as_list(full.get("evidence_gaps"))),
        "rank_evidence_id": None,
        "missing_fields": list(_as_list(full.get("missing_fields"))),
        "source_coverage": dict(_as_mapping(full.get("source_coverage"))),
        "scope": "llm_scoped",
        "coverage_eligible": False,
    }


def _ai_result(
    package: Mapping[str, Any],
    adapter: Mapping[str, Any],
    *,
    mode: str,
    clean: dict[str, Any],
    issues: list[dict[str, Any]],
    audit: Mapping[str, Any],
    metadata: Mapping[str, Any],
    coverage: Mapping[str, Any],
    screening_available: bool,
    screening_suppressed: bool,
    pre_validator_issues: list[dict[str, Any]],
) -> dict[str, Any]:
    explanation = _deterministic_ai_sections(package, adapter, mode)
    explanation["screening_basis"] = _annotate_clean_claims(
        _as_list(clean.get("screening_rationale"))
    )
    if mode == "kg_llm":
        explanation["known_targets"] = [
            *explanation["known_targets"],
            *_annotate_clean_claims(_as_list(clean.get("target_mechanism"))),
        ]
        explanation["associated_pathways"] = [
            *explanation["associated_pathways"],
            *_annotate_clean_claims(_as_list(clean.get("pathway_association"))),
        ]
    explanation["mechanism_hypotheses"] = _annotate_clean_claims(
        _as_list(clean.get("mechanism_hypotheses"))
    )

    accepted = int(audit.get("accepted_claims") or 0)
    validator_issues = list(audit.get("issues") or issues)
    categories = _issue_categories(validator_issues)
    reference_categories = {"reference", "citation_type", "gene_binding"}
    reference_pass = not (set(categories) & reference_categories)
    layer_pass = "layer" not in categories
    validation_pass = (
        accepted > 0
        and int(audit.get("dropped_claims") or 0) == 0
        and not (
            set(categories)
            & {"structure", "reference", "citation_type", "gene_binding", "layer"}
        )
    )
    status = "generated" if accepted > 0 else "generated_no_valid_claims"
    response = _base_response(
        package,
        mode=mode,
        status=status,
        explanation=explanation,
        adapter=adapter,
        message=(
            "KG07 AI explanation generated and checked by the Top10 validator."
            if accepted > 0
            else "KG07 AI returned no validator-accepted claims."
        ),
    )
    response.update(
        {
            "prompt_version": AI_PROMPT_VERSION,
            "model": _text(metadata.get("model")),
            "cached": False,
            "latency_seconds": metadata.get("latency_seconds", 0.0),
            "usage": dict(_as_mapping(metadata.get("usage"))),
            "provider_cost": None,
            "provider_cost_status": "not_tracked_no_cache",
            "citation_validation_issues": validator_issues,
            "validator_issues": validator_issues,
            "pre_validator_issues": pre_validator_issues,
            "citation_validation_pass": validation_pass,
            "citation_reference_validation_pass": reference_pass,
            "layer_validation_pass": layer_pass,
            "citation_reference_status": "top10_validator_checked",
            "semantic_entailment_status": "heuristic_guard_only",
            "human_review_required": True,
            "claim_audit": dict(audit),
            "validator_issue_categories": categories,
            "coverage_assessment": dict(coverage),
            "coverage_bundle_scope": "full",
            "validator_bundle_scope": _as_mapping(adapter.get("llm_validator_bundle")).get("scope"),
            "screening_available": screening_available,
            "ai_screening_suppressed": screening_suppressed,
            "ai_called": True,
        }
    )
    return response


def _direct_no_s2_response(
    package: Mapping[str, Any],
    adapter: Mapping[str, Any],
    coverage: Mapping[str, Any],
) -> dict[str, Any]:
    explanation = _deterministic_ai_sections(package, adapter, "direct_llm")
    explanation["screening_basis"] = []
    response = _base_response(
        package,
        mode="direct_llm",
        status="coverage_insufficient",
        explanation=explanation,
        adapter=adapter,
        message=(
            "direct_llm requires a real S2 EvidenceRecord ID; "
            "no provider call was made."
        ),
    )
    response.update(
        {
            "prompt_version": "not_called_no_s2_evidence_ids",
            "model": None,
            "cached": False,
            "provider_cost_status": "not_requested_no_s2_evidence_ids",
            "citation_reference_status": "not_called_no_s2_evidence_ids",
            "semantic_entailment_status": "not_run_no_s2_evidence_ids",
            "coverage_assessment": dict(coverage),
            "coverage_bundle_scope": "full",
            "screening_available": False,
            "ai_called": False,
        }
    )
    return response


def _run_kg_llm(
    package: Mapping[str, Any],
    adapter: Mapping[str, Any],
    coverage: Mapping[str, Any],
) -> dict[str, Any]:
    scoped = _as_mapping(adapter.get("llm_validator_bundle"))
    if scoped.get("scope") != "llm_scoped" or scoped.get("coverage_eligible") is not False:
        raise ValueError("KG07 AI requires the scoped validator bundle")
    payload = _bounded_ai_payload(adapter, mode="kg_llm")
    raw, metadata = _call_provider(
        _build_ai_messages(payload, mode="kg_llm")
    )
    screening_available = _screening_available(scoped)
    normalized, suppressed = _check_ai_structure(
        raw, mode="kg_llm", screening_available=screening_available
    )
    normalized, pre_issues = _sanitize_unsafe_ai_claims(normalized)
    if suppressed:
        pre_issues.append(
            {
                "section": "screening_rationale",
                "issue": "screening claim suppressed: no S2 EvidenceRecord ID",
            }
        )
    clean, issues, audit = validator.validate_explanation(normalized, dict(scoped))
    return _ai_result(
        package,
        adapter,
        mode="kg_llm",
        clean=clean,
        issues=issues,
        audit=audit,
        metadata=metadata,
        coverage=coverage,
        screening_available=screening_available,
        screening_suppressed=suppressed,
        pre_validator_issues=pre_issues,
    )


def _run_direct_llm(
    package: Mapping[str, Any],
    adapter: Mapping[str, Any],
    coverage: Mapping[str, Any],
) -> dict[str, Any]:
    restricted = _direct_validator_bundle(adapter)
    if restricted is None:
        return _direct_no_s2_response(package, adapter, coverage)
    payload = _bounded_ai_payload(adapter, mode="direct_llm")
    raw, metadata = _call_provider(
        _build_ai_messages(payload, mode="direct_llm")
    )
    normalized, suppressed = _check_ai_structure(
        raw, mode="direct_llm", screening_available=True
    )
    normalized, pre_issues = _sanitize_unsafe_ai_claims(normalized)
    clean, issues, audit = validator.validate_explanation(normalized, restricted)
    return _ai_result(
        package,
        adapter,
        mode="direct_llm",
        clean=clean,
        issues=issues,
        audit=audit,
        metadata=metadata,
        coverage=coverage,
        screening_available=True,
        screening_suppressed=suppressed,
        pre_validator_issues=pre_issues,
    )


def interpret(
    package: Mapping[str, Any],
    mode: str = "kg_llm",
    *,
    expected_graph_build_id: str | None = None,
) -> dict[str, Any]:
    """Compatibility entry point used by ``main.py``.

    ``kg_rule`` is the frozen deterministic template.  ``kg_llm`` and
    ``direct_llm`` use DeepSeek Flash and the Top10 validator.  If no API key
    is configured, the legacy not-implemented response is retained without any
    provider call.
    """

    if mode not in MODE_NAMES:
        raise ValueError(f"unsupported KG07 mode: {mode}")
    _assert_expected_build(package, expected_graph_build_id)
    if mode == "kg_rule":
        return rule_template(
            package,
            expected_graph_build_id=expected_graph_build_id,
        )
    if not _api_key_configured():
        return _not_configured_ai_response(package, mode)
    _assert_flash_config()
    try:
        adapter = build_kg07_evidence(
            package,
            expected_graph_build_id=expected_graph_build_id,
        )
    except ValueError as exc:
        if _text(exc) == "KG06 package has no paths":
            return _empty_paths_ai_response(package, mode, _text(exc))
        raise
    coverage = _full_coverage(adapter)
    if mode == "kg_llm":
        return _run_kg_llm(package, adapter, coverage)
    return _run_direct_llm(package, adapter, coverage)
