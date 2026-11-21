# ER-DReKG AI 药物机制解释 — 阶段 3.2 实施报告

- 日期：2026-11-20
- 范围：仅修复第二轮独立审查的 P0 问题；不进入前端，不调用真实 DeepSeek API，不重启服务。
- 结论：后端修复完成，专项 24 项测试通过；全量 103 passed / 1 既有失败 / 4 skipped；等待复核后再决定前端。

## 1. 修改文件清单

| 文件 | 修改类型 | 内容 |
| --- | --- | --- |
| `backend/app/interpretation/evidence.py` | 修改 | 增加 `evidence_meta`、`path_meta`；补充合成冻结排名引用 `TOP10:<pert_id>`；缓存输入加入 S1 文件 SHA-256 |
| `backend/app/interpretation/validator.py` | 修改 | 增加 `SECTION_RULES` 引用类型—段落绑定；`_citation_scope()`；封堵自由文本；`_guard_language()` 扩展；`backend_coverage()` 新语义；A/B/C/D 结构分层 |
| `backend/app/interpretation/prompt.py` | 修改 | Prompt 禁止模型输出自由文本和自评；版本升为 `drug-interpretation-v1.1`；新增 `prompt_fingerprint()` |
| `backend/app/interpretation/service.py` | 修改 | 缓存键加入 S1/Prompt 正文哈希；后端确定性生成 limitations/suggestions/gaps；`evidence_assessment` 只保留后端 coverage 和原始 grade matrix；新增 `_section_status()` 并接入响应 |
| `backend/tests/test_drug_interpretation.py` | 修改 | 重写为 24 项专项测试 |
| `docs/ai_drug_interpretation_stage3_2_report.md` | 新增 | 本报告 |

未修改：`provider.py`、`router.py`、`main.py`、`config.py`、S1/S2 原始数据、Top10、KG06/KG07、Neo4j/PostgreSQL、GraphBundle、智谱接口、前端、数据库迁移、`.env.example`。

## 2. P0-1：引用类型与陈述类型绑定

### 2.1 数据侧元数据

`evidence.py` 为每个证据生成 `evidence_meta`：

- `kind`：`target` / `pathway` / `s1_deg` / `s2` / `mapping` / `rank`。
- `predicate`：原始边谓词。
- `path_ids`：证据所属路径。
- `route_types`：`direct_target` / `s1_deg_overlap` / `public_pathway` / `s1_enrichment_overlap` 等。
- `s1_direction`：S1 基因的 `up` / `down`。
- `verified`：来源核验状态。

`path_meta` 记录每一条路径的 `route_type`、`target_gene_id`、`pathway_id`、`relation`、`s1_direction`。因此校验器看到的不再只是“ID 在不在白名单”，而是“这个 ID 是什么类型的证据、属于哪条路径、路径是什么关系”。

### 2.2 段落允许的证据类型

`validator.py:23` 的 `SECTION_RULES`：

| 段落 | 允许 kind | 允许 route | 必须至少含有一类合格证据 |
| --- | --- | --- | --- |
| `screening_rationale` | `s2`、`rank` | 无 | `s2` 或 `rank` |
| `target_mechanism` | `target`、`s1_deg` | `direct_target`、`s1_deg_overlap` | 必须含 `target` 或 `direct_target` |
| `pathway_association` | `pathway`、`s1_deg` | `public_pathway`、`s1_enrichment_overlap`、`s1_deg_overlap` | 必须含 `pathway` 或公共/S1 富集通路 |
| `er_positive_relevance` | `s2`、`s1_deg`、`pathway` | `s1_deg_overlap`、`public_pathway`、`s1_enrichment_overlap` | 必须含 `s2` / `s1_deg` / `pathway` 之一 |
| `mechanism_hypotheses` | `target`、`s1_deg`、`pathway` | 四类路径 | 必须含 `target` 或 `pathway` |

### 2.3 具体拦截规则

`_citation_scope()`（`validator.py:162`）负责：

1. 收集引用 evidence 的 `kind` 与路径 `route_type`。
2. 如果同时引用 `path_id` 和 `evidence_id`，逐条检查该 evidence 是否真的在该 path 的 `evidence_ids` 中；不匹配直接判定引用不成立。
3. 如果只引用 evidence，则用 evidence 自身的 `path_ids` 作为有效路径集合。

`validate_explanation()`（`validator.py:272`）随后：

- `_sanitize_ids()` 先把不在当前白名单的 evidence_id/path_id 剔除并记录 issue。
- 关键点：只要原始引用中出现任何白名单外 ID，即使同时还有合法 ID，整条 claim 删除（`validator.py:318`），不会保留“看起来合法”的部分。
- 无 evidence 且无 path 的 claim 直接删除（`validator.py:323`）。
- kind 或 route 与段落规则不符 → 删除并记录 `citation type does not support this section`。
- 段落没有任何合格证据类型 → 删除并记录 `section has no qualifying evidence type`。
- 靶点关系集合限定为 `TARGET / INHIBITOR / ACTIVATOR / BINDS / AGONIST / ANTAGONIST / MODULATOR / REGULATOR / SUBSTRATE / PRODUCT / INTERACTS_WITH / HAS_TARGET / DRUG_TARGET / INHIBITS / ACTIVATES`。`HAS_CANDIDATE`、`REFERENCES_DRUG`、`HAS_S2_CONDITION` 不能证明靶点或作用机制。
- PHA-767491 的 6 条证据全部是 `HAS_S2_CONDITION`、`path_id=None`，因此无法通过 `target_mechanism`；S2 evidence_id 不能支撑“PHA 抑制 CDK1”。
- 靶点 evidence_id 不能单独通过 `pathway_association`。

### 2.4 证据不足状态

`service.py:164` 新增 `_section_status()`，响应字段 `section_status`：

- `available`
- `no_screening_evidence`
- `no_target_evidence`
- `no_pathway_evidence`
- `no_er_context_evidence`
- `no_hypothesis_basis`
- `available_but_no_valid_claims`

PHA-767491 的 `target_mechanism` 返回 `no_target_evidence`，`pathway_association` 返回 `no_pathway_evidence`，不再用空白卡片伪装“没有机制”。

## 3. P0-2：封堵无引用自由文本绕过

`validate_explanation()` 不再信任以下字段：

- `limitations`
- `validation_suggestions`
- `insufficient_evidence`
- `evidence_assessment`

这些字段只记录到 `ignored_model_free_text`，不进入最终响应。

`service.py` 改为后端确定性生成：

- `_backend_limitations()`：基于当前 bundle 的缺靶点、缺通路、缺 S1、缺 S2、grade matrix 等事实。
- `_backend_validation_suggestions()`：只输出未来验证建议，不写成已知事实。
- `_backend_insufficient_evidence()`：只列出缺口，不包含机制断言。
- `evidence_assessment` 只保留后端 `coverage_level`、`coverage_basis`、`coverage_note`、原始 `grade_matrix`，以及标注为 `model_verdict_ignored` 的诊断字段。

因此模型无法再通过 limitations 或 basis 输出“PHA 通过抑制 CDK1 发挥作用”这类无引用机制事实。

## 4. P0-3：verdict 改为证据覆盖度

`backend_verdict` 已改为 `backend_coverage`，`validator.py:422` 输出：

- `coverage_high`
- `coverage_limited`
- `coverage_insufficient`

响应中同时给出：

- `coverage_basis`：直接靶点路径数、S1 富集路径数、唯一证据数。
- `coverage_note`：明确“只表示当前项目和知识图谱的证据覆盖程度，不代表药物疗效、临床证据等级或机制已被验证”。

阈值沿用原有规则：

- 未映射或无任何靶点/通路路径 → `coverage_insufficient`。
- 靶点 ≥ 1 且 S1 富集 ≥ 1 且证据 ≥ 20 → `coverage_high`。
- 有靶点但通路/证据不足 → `coverage_limited`。
- 只有间接通路、无直接靶点 → `coverage_limited`。

原始 `evidence_grade_matrix.csv` 的分维度定性值原样放入 `evidence_assessment.grade_matrix`，没有相加、没有换算概率、没有映射成统一分数。

## 5. P0-4：过强表述、因果和方向校验

`_guard_language()`（`validator.py:191`）当前覆盖：

- `relation=causal`：降级为 `association`，文本加“待验证表述（已降级）”。
- 因果词：导致、引起、驱动、造成、从而抑制、从而使、进而抑制、进而激活、介导。
- 疗效词：治愈、临床有效、疗效、推荐使用、可用于治疗、能够治疗、杀伤、抑制肿瘤、抑制生长、抗肿瘤、缩小肿瘤、细胞毒性、延长生存、生存期、临床获益、获益、耐药、治疗敏感、改善症状、逆转耐药、有效治疗、治疗、有效。
- 方向词：抑制、激活、上调、下调、升高、降低。
- 主语：`该药`、`药物`、`化合物`、`给药`、`处理`。
- S1 方向：只有引用中的 S1 方向唯一且文本方向一致、且文本明确出现“S1/差异表达”时才保留；否则删除。
- 特别规则：S1 基因上调/下调不等于药物能够上调/下调该基因；药物主语 + 表达方向断言直接删除。
- `screening_rationale` 中“表达逆转”作为项目筛选概念放行；其他段落出现“药物逆转表达”会被方向规则拦截。
- 方向无法可靠验证时删除相关事实性 claim，或在假设段保留并强制标注“待验证假设”，不假装完成完整语义验证。

需要明确：这是启发式规则，不是完整自然语言理解。它只能覆盖已列出的模式，不能保证识别所有隐式因果、临床获益或语义方向。

## 6. 缓存键变化

`service.cache_key()`（`service.py:45`）现在包含：

- `project_id`
- `drug_id`
- `graph_build_id`
- `top10_sha256`
- `s1_sha256`
- `prompt_sha256`
- `prompt_version`
- `model`
- `evidence_input_sha256`
- 既有证据包相关字段

`prompt.py:71` 的 `prompt_fingerprint()` 对 `SYSTEM_PROMPT` 全文做 SHA-256。`PROMPT_VERSION` 为 `drug-interpretation-v1.1`。

效果：

- S1 文件内容改变 → `s1_sha256` 改变 → 缓存键改变。
- Prompt 正文改变但版本常量不变 → `prompt_sha256` 改变 → 缓存键改变。

保留原有原子写入 `_write_cache()`（临时文件 + `replace`）和 `_LOCKS` 单进程非阻塞锁；未引入 Redis、Celery 或新外部依赖。

## 7. 新增/更新测试

`backend/tests/test_drug_interpretation.py` 共 24 项：

| # | 测试 | 验证内容 |
| --- | --- | --- |
| 1 | `test_frozen_top10_hash_and_rank_authority` | 冻结 Top10 哈希与排名来源 |
| 2 | `test_assemble_bundle_keeps_frozen_rank_and_s1_stats` | 证据包保留冻结 rank 与 S1 统计 |
| 3 | `test_target_without_deg_is_not_marked_as_disease_gene` | 靶点未在 S1 差异表达中时不得标为疾病基因 |
| 4 | `test_prompt_contains_scientific_constraints` | Prompt 含禁止临床建议、禁止编造引用等约束 |
| 5 | `test_pha_s2_evidence_cannot_support_target_mechanism` | PHA 的 S2 条件证据不能支撑靶点机制 |
| 6 | `test_s2_evidence_id_cannot_pass_target_mechanism` | S2 evidence_id 不能通过 target 段 |
| 7 | `test_target_evidence_cannot_pass_pathway_association` | 靶点证据不能单独通过 pathway 段 |
| 8 | `test_foreign_or_unknown_evidence_id_is_rejected` | 其他药物/未知 evidence_id 被拒 |
| 9 | `test_nonexistent_path_id_is_rejected` | 不存在的 path_id 被拒 |
| 10 | `test_empty_citation_claim_is_deleted` | 空引用事实性 claim 被删除 |
| 11 | `test_limitations_mechanism_assertion_is_not_trusted` | limitations 中的无引用机制断言不被信任 |
| 12 | `test_causal_relation_is_downgraded` | `relation=causal` 被降级 |
| 13 | `test_efficacy_claim_is_dropped` | 无证据疗效断言被删除 |
| 14 | `test_expression_direction_contradiction_is_dropped` | 表达方向与 S1 矛盾时删除 |
| 15 | `test_drug_expression_effect_without_s1_context_is_dropped` | 药物主语表达效应无 S1 语境时删除 |
| 16 | `test_hypothesis_is_forced_to_layer_c` | 机制假设强制为 C 层并标注待验证 |
| 17 | `test_screening_reversal_term_is_allowed` | screening 中“表达逆转”作为项目概念被允许 |
| 18 | `test_coverage_levels_are_not_scientific_grades` | coverage 语义不是科学证据等级 |
| 19 | `test_provider_requires_key` | 无 Key 时抛配置错误 |
| 20 | `test_provider_parses_fenced_json` | 解析 fenced JSON |
| 21 | `test_service_caches_and_keeps_key_out_of_output` | 文件缓存生效且 Key 不进入输出 |
| 22 | `test_service_skips_model_when_no_evidence` | 零证据不调用模型；`section_status` 为 `no_target_evidence`/`no_pathway_evidence` |
| 23 | `test_s1_hash_changes_cache_key` | S1 哈希变化导致缓存键变化 |
| 24 | `test_prompt_text_changes_cache_key` | Prompt 正文变化导致缓存键变化 |

测试缺口（明确保留）：

- 没有专门的并发请求测试；当前只保证单进程锁，跨进程不保证。
- 没有模型返回非法 JSON 的专项测试；provider 已有异常路径，但未纳入本文件测试。
- 没有“合法靶点证据 + 错误基因名”的语义一致性测试；这是已知残留绕过路径。
- 没有显式测试缓存版本常量变化；当前通过 S1/Prompt 哈希覆盖内容变化。

## 8. 测试结果

专项：

```text
24 passed in 0.86s
```

全量后端：

```text
1 failed, 103 passed, 4 skipped
```

唯一失败：

`backend/tests/test_api.py::test_project_s2_run_registers_cli_provenance_and_imports_condition_results`

原因：S2 隔离 Python 环境/`project_screen.py` 不存在（`main.py:819`），属于既有环境问题，与本功能无关。

## 9. 只读核验

| 项目 | 结果 |
| --- | --- |
| 冻结 Top10 SHA-256 | `c1f414d20e98923471a461e1ce6c3bf5b59002d4e5309cadf89be22492c08baf` |
| S1 limma SHA-256 | `b3dafdae855a0f44cbe80dbc40d17693ed01f4b31d899acbabc60e5485ed21c4` |
| S2 `signature_scores.csv` | 与 manifest 一致 |
| S2 `drug_condition_summary.csv` | 与 manifest 一致 |
| S2 `s1_gene_mapping_audit.csv` | 与 manifest 一致 |
| S2 `qc_summary.json` | 与 manifest 一致 |
| PostgreSQL `candidates` | 50 |
| PostgreSQL `evidence_records` | 48,729 |
| PostgreSQL `s2_condition_results` | 3,943 |
| GraphBuild | 仅 `materialized d570eb8deb3e5eea` |
| 缓存目录 | 不存在 `experiments/drug_interpretation_cache/` |
| `import app.main` | 成功 |
| 真实 DeepSeek API | 未调用 |
| 服务重启 | 未执行 |

未重建 GraphBundle，未重导入 Neo4j，未修改 S1/S2/Top10，未新增数据库表或迁移。

## 10. 已知绕过路径与残留风险

1. **语义级基因/通路名称未校验**：校验器验证证据类型和路径归属，但不验证 claim 文本中的基因名是否等于引用路径的 `target_gene`。用一条合法的靶点证据配错基因名，仍可能通过。这是当前最重要的残留风险。
2. **关键词表不完整**：已扩展因果/疗效/方向词，但无法覆盖所有隐式表述，例如“临床获益显著”“改善预后”“协同增效”的变体。不能声称完成完整语义验证。
3. **A/B/C/D 是结构分层，不是原始审计等级**：A 为直接项目证据（靶点/S1/S2/冻结排名），B 为通路关联，C 为模型假设，D 为证据缺口。假设若写在 target 段且引用合法靶点证据，仍可能被判 A；目前只强制 `mechanism_hypotheses` 段为 C。
4. **ER 阳性相关性**：允许由 S2 ER+ 条件或通路证据支撑；若措辞避开疗效词，仍可能对 ER+ 治疗相关性过度表述。
5. **合成排名引用**：`TOP10:<pert_id>` 不是 PostgreSQL 证据，`verified_in_postgres=False`，只以冻结 Top10 CSV 及其哈希为源。
6. **并发**：`_LOCKS` 只保证单进程；多 worker/多进程可能重复调用模型。
7. **Prompt 哈希范围**：只哈希 `SYSTEM_PROMPT` 正文，不覆盖消息拼接/压缩逻辑；修改拼接代码需手动升版本。
8. **性能**：每次请求计算 S1 文件 SHA-256，存在 I/O 开销。
9. **诊断字段**：`model_verdict_ignored` 仅用于审计；前端若展示需明确它不是后端结论。

## 11. 是否允许进入前端阶段

后端 P0-1～P0-4 已完成，专项测试与只读核验通过。建议允许进入前端阶段，但前端必须：

- 同时展示 `coverage_level` 与 `section_status`，证据不足的段落明确显示“无合格证据”。
- 明确区分“冻结 Top10 排名来源”与“当前 GraphBuild 机制证据来源”，不得混用版本。
- 不得把 `coverage_high` 展示为“疗效好”或“机制已证实”。
- 对 `mechanism_hypotheses` 显示“待验证假设”，不得与事实段混排。
- 保留科研辅助免责声明。

最终是否进入前端，等待复核拍板；本阶段未修改任何前端文件、未重启服务、未调用真实 API。
