"""Prompt construction and scientific constraints for drug interpretation."""
from __future__ import annotations

import hashlib
import json
from typing import Any

PROMPT_VERSION = "drug-interpretation-v1.2"

SYSTEM_PROMPT = """你是 ER-DReKG 药物重定位项目的证据整理助手。你的任务只是解释用户提供的项目证据，不是重新筛选药物，也不是给出医学结论。

【只依据输入】
- 只能使用 user JSON 中出现的字段和 ID；不得使用任何外部知识、文献、数据库记忆或常识补全。
- 不得虚构药物靶点、差异表达基因、通路、实验、文献、DOI、数据库记录。
- 不得把一般医学知识说成“本项目已验证/已证实”。
- 输入没有的内容，不要写成事实；系统会根据证据缺口生成缺失说明。

【引用规则】
- 每个 claim 必须带非空 evidence_ids 或 path_ids，且只能原样复制输入中出现的 ID。
- 无法引用任何 ID 的陈述一律不要输出。
- 不得编造 ID、不得改写 ID、不得引用输入之外的 ID。
- 如果同时引用 path_ids 和 evidence_ids，evidence_ids 必须确实属于所引用的路径；否则整条 claim 会被删除。
- 不要复制路径内的全部 evidence_ids；优先引用 path_ids。

【引用类型必须与段落匹配】
- screening_rationale：只能引用 s2_conditions 的 evidence_ids 或 rank_evidence_id；不得引用靶点或通路证据。
- target_mechanism：必须引用 targets 的 path_ids（route_type=direct_target）或 target 类 evidence_ids；可以额外引用该靶点的 S1 DEG 证据。不得引用 S2、REFERENCES_DRUG、HAS_CANDIDATE 或通路证据。
- pathway_association：必须引用 pathways 的 path_ids；可以额外引用对应基因的 S1 DEG 证据。不得只引用靶点证据或 S2 条件证据。
- er_positive_relevance：必须引用 S2 条件、S1 DEG 或通路交汇证据；不得只凭靶点关系断言 ER 阳性乳腺癌相关性，也不得断言治疗作用。
- mechanism_hypotheses：只能把 targets 或 pathways 的证据作为依据，并明确写成待验证假设；不得引用 S2 作为唯一依据。
- 如果引用类型与段落不匹配，整条 claim 会被删除，不会保留其中一个看似合法的 ID。

【靶点实体绑定】
- target_mechanism 的每条 claim 必须用 genes 明确声明所陈述的基因实体，不能只引用任意一条靶点证据。
- genes 中的 gene_id/symbol 必须原样来自输入 targets；不要输出输入 targets 之外的基因。
- 一条 claim 声明多个基因时，必须同时引用每个基因对应的靶点证据；不得把一个基因的证据拼接给另一个基因。
- text 中出现的基因名必须与 genes 一致；不得在 text 中额外出现未声明的基因。
- 只能使用输入 targets 中的标准 symbol 或已有标准化别名；无法确认的别名不要使用，系统会拒绝。
- 旧格式 claim 如果没有 genes，系统只接受 text 中精确出现的靶点标准名；否则整条删除。

【关系与因果】
- relation 只能是 binding / inhibition / activation / association / unknown。
- 只有输入 reported_actions 明确写出方向时，才可用 inhibition 或 activation；否则用 association 或 unknown。
- 禁止因果表述：导致、引起、驱动、造成、从而抑制、从而使。
- 禁止疗效表述：治愈、治疗、疗效、有效、临床有效、推荐使用、可用于治疗、能够治疗、杀伤、抑制肿瘤、改善症状、逆转耐药。
- 禁止“已证实 / 已经证明 / 必然 / 确定”等过度确定性措辞。
- 禁止把 S1 基因上调或下调写成药物上调或下调该基因；S1 方向只描述疾病表达数据本身。
- 建议使用“可能参与 / 提示潜在关联 / 与某通路存在联系 / 值得进一步验证”。

【证据分层】每条 claim 必须给出 layer：
- A：项目直接证据（药物—靶点断言、S1 差异表达命中、S2 条件记录、冻结排名引用）。
- B：靶点—S1 富集通路或公共通路的间接交汇。
- C：仅作为假设的机制推断，必须写“假设/待验证”，且只能出现在 mechanism_hypotheses。
- 不得把 C 标成 A 或 B。

【输出格式】
- 只输出一个 JSON 对象，不要 Markdown、不要代码块、不要解释文字。
- 所有文本用简体中文；每条 text ≤ 60 个汉字；每段最多 3 条。
- 不要输出 limitations、validation_suggestions、insufficient_evidence、evidence_assessment；这些由系统根据证据确定性生成。
- JSON 结构：
{
  "screening_rationale": [claim],
  "target_mechanism": [claim],
  "pathway_association": [claim],
  "er_positive_relevance": [claim],
  "mechanism_hypotheses": [claim]
}
claim = {"text": "...", "evidence_ids": ["..."], "path_ids": ["..."], "relation": "association", "layer": "A|B|C", "genes": [{"gene_id": "...", "symbol": "..."}]}

【各段职责】
- screening_rationale：只解释排名和 WTCS 方向、条件数能支持什么，不改写分数数学含义，不描述临床疗效。
- target_mechanism：只列输入 targets 中的靶点及其 S1 命中；s1_deg 为 null 时不得说成本项目差异表达。
- pathway_association：只列输入 pathways 中的交汇，说明是关联而非因果。
- er_positive_relevance：只能基于本项目 ER+ 背景与输入证据，不得因其他癌种有效而声称对本病有效。
- mechanism_hypotheses：只放 C 类假设，最多 2 条；不得写疗效或因果结论。
"""


def prompt_fingerprint() -> str:
    """Stable SHA-256 of the full system prompt text (used in the cache key)."""
    return hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest()


def _compact(bundle: dict[str, Any]) -> dict[str, Any]:
    targets = sorted(
        bundle.get("targets") or [],
        key=lambda item: (item.get("s1_deg") is None, item.get("symbol") or ""),
    )[:8]
    pathways = sorted(
        bundle.get("pathways") or [],
        key=lambda item: (
            0 if "s1_enrichment_overlap" in (item.get("route_types") or []) else 1,
            item.get("adjusted_p") is None,
            item.get("adjusted_p") or 1.0,
        ),
    )[:12]
    conditions = (bundle.get("s2_conditions") or [])[:8]
    return {
        "project_id": bundle.get("project_id"),
        "graph_build_id": bundle.get("graph_build_id"),
        "candidate": bundle.get("candidate"),
        "compound": bundle.get("compound"),
        "rank_source": "experiments/final_results/results/final_top10.csv (frozen, SHA-256 verified)",
        "rank_evidence_id": bundle.get("rank_evidence_id"),
        "targets": [
            {
                "symbol": item.get("symbol"),
                "gene_id": item.get("gene_id"),
                "relation": item.get("relation"),
                "reported_actions": item.get("reported_actions"),
                "evidence_ids": item.get("evidence_ids"),
                "path_ids": item.get("path_ids"),
                "s1_deg": item.get("s1_deg"),
            }
            for item in targets
        ],
        "pathways": [
            {
                "pathway_id": item.get("pathway_id"),
                "label": item.get("label"),
                "target_symbol": item.get("target_symbol"),
                "route_types": item.get("route_types"),
                "adjusted_p": item.get("adjusted_p"),
                "s1_direction": item.get("s1_direction"),
                "path_ids": item.get("path_ids"),
                "evidence_ids": item.get("evidence_ids"),
            }
            for item in pathways
        ],
        "s2_conditions": [
            {
                "condition_key": item.get("condition_key"),
                "cell_id": item.get("cell_id"),
                "pert_type": item.get("pert_type"),
                "dose": item.get("dose"),
                "time": item.get("time"),
                "WTCS_median": item.get("WTCS_median"),
                "n_signatures": item.get("n_signatures"),
                "evidence_ids": item.get("evidence_ids"),
            }
            for item in conditions
        ],
        "coverage": bundle.get("coverage"),
        "evidence_gaps": bundle.get("evidence_gaps"),
        "grade_matrix": bundle.get("grade_matrix"),
        "independent_context": {
            "phase_f": bundle.get("phase_f"),
            "exp006": bundle.get("exp006"),
            "note": "这些数值仅作背景，不得作为可引用机制事实；不得据此判断疗效。",
        },
        "counts": bundle.get("counts"),
        "instruction": "只解释以上证据；没出现的靶点、基因、通路和 ID 一律不得出现。",
    }


def build_messages(bundle: dict[str, Any]) -> list[dict[str, str]]:
    payload = _compact(bundle)
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        },
    ]
