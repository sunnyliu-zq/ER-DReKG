# 阶段 3.1 实施报告：后端证据收集 + DeepSeek Flash 服务

- 日期：2026-11-20
- 范围：仅后端；未改前端、未新增数据库表、未执行迁移、未调用真实 DeepSeek API
- 结果：证据收集与生成服务已落地并通过 11 项新测试；真实数据只读联调通过

---

## 1. 本阶段完成了什么

### 1.1 证据收集（只读）

新增 `app/interpretation/evidence.py`：

| 能力 | 实现 |
|---|---|
| 冻结 Top10 读取 | 读 `experiments/final_results/results/final_top10.csv`，用 `final_data_manifest.json` 的 `output_sha256` 校验，非 PASS 或哈希不符即失败 |
| 权威排名 | 只取 CSV 的 `rank`；数据库 `Candidate.rank` 完全未使用 |
| GraphBuild 解析 | 只接受 `status=materialized` 且 `graph_source=neo4j`；默认当前正式版本 `d570eb8deb3e5eea` |
| KG06 证据包 | 直接复用 `kg06.build_evidence_package()`，保留项目/GraphBuild/AnalysisRun 隔离与哈希核验 |
| 靶点 | 只从 `direct_target` 路径取；`reported_actions` 无方向则一律 `association` |
| S1 数值 | 从 `HAS_DEG` 断言取 logFC/FDR/t/direction，再用 `limma_all_genes.csv` 补 `P.Value` |
| 通路 | 只取 `public_pathway` 与 `s1_enrichment_overlap`，FDR 取路径断言最小值 |
| S2 条件 | 复用证据包内的条件记录与 Evidence ID |
| 分项等级 | 读 `evidence_grade_matrix.csv`，仅作背景 |
| 证据分层 | A=直接靶点/S1 DEG/S2 记录；B=靶点—通路交汇；D=deterministic gaps；C 由校验器标记 |

### 1.2 DeepSeek Flash 服务

新增 `app/interpretation/provider.py`：

- 只读后端配置 `DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL / DEEPSEEK_MODEL`。
- 默认 `https://api.deepseek.com`、`deepseek-flash`，OpenAI 兼容 `/chat/completions`。
- `temperature=0`、`response_format=json_object`、`max_tokens=8192`、超时 120s。
- 仅对 429/5xx 重试一次；连接失败、非 JSON、空内容分别分类报错。
- Key 不写日志、不进缓存、不返回前端。

### 1.3 Prompt、校验、编排

| 文件 | 内容 |
|---|---|
| `prompt.py` | 六段式 Schema；明确禁止虚构靶点/基因/通路/文献、禁止因果与临床建议；要求每条 claim 带 `evidence_ids` 或 `path_ids` |
| `validator.py` | 结构校验、引用白名单、无引用丢弃、因果/方向降级、过强措辞降级、`layer` 由后端按引用重算、移除百分比；`verdict` 由后端计算 |
| `service.py` | 单飞锁、文件缓存、确定性“证据不足”降级、响应组装、审计信息 |
| `router.py` | `POST /api/v1/projects/{project_id}/drug-interpretation`，错误码映射 |

缓存键包含：`project_id + drug_id + graph_build_id + Top10 SHA-256 + Prompt 版本 + 模型名 + 证据输入 SHA-256`；缓存在 `experiments/drug_interpretation_cache/`，本阶段未产生缓存文件。

---

## 2. 修改文件清单

### 新增

| 文件 | 作用 |
|---|---|
| `backend/app/interpretation/__init__.py` | 包入口 |
| `backend/app/interpretation/evidence.py` | 只读取证 + A/B/D 预分层 |
| `backend/app/interpretation/prompt.py` | Prompt 与科学约束 |
| `backend/app/interpretation/provider.py` | DeepSeek Flash 客户端 |
| `backend/app/interpretation/validator.py` | 结构/引用/层级/语言校验 |
| `backend/app/interpretation/service.py` | 编排、缓存、单飞、响应组装 |
| `backend/app/interpretation/schemas.py` | 请求模型 |
| `backend/app/interpretation/router.py` | API 路由 |
| `backend/tests/test_drug_interpretation.py` | 11 项单元测试 |

### 修改（最小）

| 文件 | 改动 |
|---|---|
| `backend/app/core/config.py` | 新增 5 个 DeepSeek 配置项，默认空 Key，不影响现有智谱配置 |
| `backend/app/main.py` | 新增 1 行 import + 1 行 `include_router` |
| `.env.example` | 仅追加 3 个占位变量，无真实 Key |

### 明确未改

`kg06.py`、`kg07.py`、S1/S2 模块、GraphBundle/Neo4j 导入脚本、`experiments/**` 结果文件、Top10 CSV/JSON、前端代码。

---

## 3. 测试结果

### 3.1 新增测试（11 项，全部通过）

覆盖：Top10 哈希与排名权威、靶点/S1/通路组装、无 DEG 时不得标疾病基因、Prompt 约束、伪造引用丢弃、过强措辞降级、通路 claim 层级重算、假设强制 C 类、verdict 三档、缺少 Key、JSON 解析、缓存命中且 Key 不出现在输出、零证据不调用模型。

```
11 passed in 0.83s
```

### 3.2 全量回归

```
1 failed, 90 passed, 4 skipped, 1 warning in 17.20s
```

唯一失败：`test_project_s2_run_registers_cli_provenance_and_imports_condition_results`
原因：本机缺少 S2 隔离 Python 环境或 `project_screen.py`，与本次改动无关（报错发生在 `main.py:819` 的 S2 运行入口）。

### 3.3 真实数据只读联调

| 药物 | 冻结 rank | 靶点 | 通路 | S2 条件 | 唯一证据 | 缺口 | 结论 |
|---|---:|---:|---:|---:|---:|---:|---|
| ponatinib | 1 | 21 | 131 | 5 | 263 | 2 | 证据充分 |
| MK-2461 | 3 | 1 | 0 | 5 | 8 | 4 | 证据不完整 |
| PHA-767491 | 6 | 0 | 0 | 6 | 6 | 5 | 证据不足 |

- 所有结果均绑定 `GRAPH_BUILD:3f18f4c1-…:d570eb8deb3e5eea`。
- ponatinib 首个靶点 ABCB1 的 `s1_deg=null`，不会被描述成本项目差异基因。
- MK-2461 的 MET 关系来自来源 `INHIBITOR`，保留方向；无通路。

### 3.4 无 Key 行为

直接调用路由函数，返回：

```
HTTP 503 AI 解释服务未配置（后端缺少 DeepSeek API Key）
```

未触发模型请求，未创建缓存。

### 3.5 只读性核验（前后一致）

| 项 | 结果 |
|---|---|
| candidates | 50（未变） |
| evidence_records | 48,729（未变） |
| GraphBuild | 仍为 `materialized / d570eb8deb3e5eea` |
| Top10 SHA-256 | `c1f414d20e98…c08baf`（未变） |
| 缓存目录 | 未创建 |

---

## 4. 接口说明（本阶段已可用，需重启后端）

```http
POST /api/v1/projects/{project_id}/drug-interpretation
Content-Type: application/json

{"drug_id": "BRD-K44227013", "graph_build_id": null, "force_regenerate": false}
```

主要返回字段：`rank / rank_source / screening / model_name / evidence_summary / screening_rationale / target_mechanism / pathway_association / er_positive_relevance / mechanism_hypotheses / evidence_assessment / limitations / validation_suggestions / insufficient_evidence / evidence_references / claim_audit / graph_build_matches_page / page_graph_build_id / page_version_note`。

错误码：404 项目/药物不存在；409 图谱或证据不一致；503 未配置或不可用；502 模型返回格式校验失败。

> 当前 18000 端口运行的旧进程尚未加载新路由。检查本阶段代码后可重启后端；我未主动重启或终止现有进程。

---

## 5. 风险与已知限制

| # | 风险/限制 | 当前处理 |
|---|---|---|
| 1 | 未配置真实 Key，无法做真实模型联调 | 返回 503；阶段 3.2 需你配置 `DEEPSEEK_API_KEY` 后再实测 |
| 2 | 旧进程未加载新路由 | 需重启后端（未擅自操作） |
| 3 | 页面静态证据版本 `a6aa…` 与机制证据 `d570…` 不同 | 响应含 `graph_build_matches_page=false` 与说明，前端阶段必须展示 |
| 4 | 现有 KG06 Cypher 在 Neo4j 产生 `rank` 属性不存在的 warning | 既有查询行为，不影响本功能；未改 KG06 |
| 5 | 全量测试 1 项失败 | S2 隔离环境缺失，与本功能无关 |
| 6 | 缓存命中不重复语义校验 | 缓存键绑定证据哈希；缓存内容本身是已校验结果 |
| 7 | 模型可能仍输出过度推断 | 引用白名单 + 分层重算 + 措辞降级 + 缺失标记；语义正确性仍需人工复核 |
| 8 | 生成结果未持久化到数据库 | 按确认采用文件缓存，不建表、不迁移 |

---

## 6. 下一阶段（待你检查后）

1. 你配置后端 `.env` 的 `DEEPSEEK_API_KEY`（我不会写入真实 Key）。
2. 重启后端 18000 端口，验证一次真实 DeepSeek Flash 调用（仅 1 个候选，控制 Token）。
3. 进入前端：Top10 页入口、六卡片、A/B/C/D 标签、版本差异提示、错误/空态、切换药物清空。
4. 前端完成后再做三档科学合理性验收与全流程联调。
