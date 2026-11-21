# AI 辅助药物机制可解释性（Stage 1 只读审查与实施设计）

- 审查日期：2026-11-20
- 审查方式：只读（未修改代码、数据库、图谱、S1/S2/Top10，未调用任何真实大模型 API）
- 结论：**不重建系统，在现有 KG06 证据包 + KG07 解释管线之上新增一个独立、可追溯的“药物机制解释层”**；等待确认后再实施。

---

## 0. 先看三个必须先决定的事实

| # | 发现 | 影响 | 建议 |
|---|---|---|---|
| 1 | “DeepSeek Flash”确实存在：官方模型名 `deepseek-flash`（DeepSeek-V4.1-Flash），Base URL `https://api.deepseek.com`，OpenAI 兼容 `/chat/completions`；旧名 `deepseek-v4-flash` 仍可接受 | 模型可选，但本项目**尚未配置任何 DeepSeek Key**，`config.py` 里也没有 DeepSeek 字段 | 新增 3 个环境变量，Key 只放后端；无 Key 时接口明确返回 503，不自动换模型 |
| 2 | 现有 KG07 走的是**智谱 GLM**（`glm-5.3-flash`），且当前 `ZHIPU_API_KEY` 未配置 | 现有“AI 机制解释”按钮实际不可用；新功能不能复用它作为“DeepSeek Flash” | 新模块与 KG07 并存但不混用：KG07 保留原样，新模块独立 provider |
| 3 | 项目里同时存在 **3 个 GraphBuild ID**：前端静态 Top10 = `…:a6aabb2147cb5f5e`，KG06 审计 = `…:a08001496a447f7f`，当前可查询库 = `…:d570eb8deb3e5eea` | 页面上的“靶点/通路数”与实时证据包可能不一致（如 JNJ-26481585 页面显示 0 条路径，实时包有 76 条） | AI 面板必须**显式标注所用 GraphBuild**，并在与页面历史版本不一致时提示；绝不改写页面历史数字 |

> 另外：数据库 `candidates` 表的 50 条记录属于 `PHASEB_OPTIMUSKG32_20261113` 种子子图，不是 Top10 的 S2 排名；其 rank 与冻结 Top10 不同。**排名唯一权威来源应是冻结文件 `experiments/final_results/results/final_top10.csv`**，不能用 `Candidate.rank`。

---

## A. 当前项目架构说明

### A1. 目录与运行形态

| 层 | 位置 | 说明 |
|---|---|---|
| 后端 | `backend/app/` | FastAPI 单体，`main.py` 约 127KB，40+ 路由集中注册；`API = "/api/v1"` |
| 业务模块 | `kg06.py`、`kg07.py`、`s1_genes.py`、`s1_pathways.py`、`graph_repository.py` | KG06 证据包、KG07 解释、S1 基因/通路读取、图谱查询 |
| ORM | `backend/app/models.py` | Project / Study / AnalysisRun / GraphBuild / EvidenceRecord / DiseaseContext / Dataset / Candidate / S2ConditionResult / Collab* |
| 迁移 | `backend/migrations/versions/` | Alembic，9 个版本，含 `add_versioned_evidence_records` 等 |
| 前端 | `frontend/src/` | React 19 + Vite 6 + TypeScript + Ant Design 6 + TanStack Query 5 + ECharts + Cytoscape |
| 前端页面 | `pages/` | Dashboard / S1 / S2 / Graph / Drug / FinalResults / RunComparison |
| 本地服务 | —— | PostgreSQL 5432 ✅、Neo4j 7687 ✅、后端 **18000** ✅、前端 5173 ✅ |

> 注意：8000 端口被另一个项目 AiCompanion 占用，不是本项目后端；本项目后端实际为 `127.0.0.1:18000`，Vite 代理 `/api -> 18000`。

### A2. 现有证据/解释链路（可直接复用）

```
前端选择候选
  → GET  /api/v1/projects/{pid}/kg06/evidence-package/{pert_id}
  → kg06.build_evidence_package()：固定模板 Cypher + GraphBuild 版本过滤 + PostgreSQL EvidenceRecord 核验
  → POST /api/v1/projects/{pid}/kg07/interpret/{pert_id}?mode=kg_llm|kg_rule|direct_llm
  → kg07：压缩证据包 → Prompt → 模型 → 引用校验/降级 → 文件缓存
  → DrugPage 展示
```

- `kg06.build_evidence_package` 已完成：项目/GraphBuild/AnalysisRun 三重隔离、GraphBundle 哈希校验、证据缺口 `evidence_gaps`、覆盖率 `coverage`、每条路径 `path_id/evidence_ids/edges/sources`。
- `kg07` 已完成：引用 ID 白名单校验、无引用声明丢弃、因果表述降级、方向性降级、缓存、模型/耗时/费用字段。
- `GraphPage` 已支持按 `path_id` 高亮 KG06 路径，`DrugPage` 已有证据弹窗与 JSON 导出。

**因此本功能不需要新建取证体系，只需在 KG06 之上补“S1 数值 + 排名权威 + 六段式输出 + 证据分层 + 新模型”。**

### A3. 已确认的真实数据

| 项 | 实测 |
|---|---|
| 项目 | `3f18f4c1-713d-58c5-8a53-38eca5a5aaf9` / slug `erpos-gse42568` / MONDO:0007254 |
| 当前图谱 | `GRAPH_BUILD:3f18f4c1-…:d570eb8deb3e5eea`，status `materialized`，source `neo4j` |
| 证据记录 | PostgreSQL `evidence_records` 48,729 条（与交接文档一致） |
| S1 | `s1_初步_gse42568_limma/limma_all_genes.csv`：`gene_symbol, logFC, AveExpr, t, P.Value, adj.P.Val, B` |
| S1 通路 | `experiments/s1_pathway_enrichment/{go_up,go_down,reactome_up,reactome_down,hallmark_gsea}.csv`，字段含 `p.adjust / geneID / Count / significant` |
| Top10 | `experiments/final_results/results/final_top10.csv`（10 行，rank 1–10）；前端静态副本 `frontend/public/final-top10.csv`、`final-results.json` |
| 分项证据等级 | `experiments/final_results/final05_scientific_audit/results/evidence_grade_matrix.csv`（按维度给“weak/moderate/limited…”） |
| 综合证据 | `experiments/phase_f_integrated_evidence/candidate_evidence_with_kg.csv`（含 rank、PRISM、external S1、mapping） |
| 现有解释产物 | `experiments/kg07_mechanism_interpretation/**`（20 例对照、Top50、规则模板、人工盲审表） |

---

## B. Top10 与机制证据的数据来源说明

### B1. 排名与分数（只读，不重算）

| 字段 | 权威来源 | 说明 |
|---|---|---|
| rank | `final_top10.csv.rank` | 冻结的 Top10 顺序 |
| drug_name | `final_top10.csv.drug_name` | 展示名 |
| WTCS / 分数 | `final_top10.csv.WTCS_median`，`score_name = "WTCS median B"` | 表达逆转关联分数；负值＝反向关联，不改数学含义 |
| 条件数/剂量 | `final_top10.csv.n_signatures / n_24h_dose_conditions / negative_dose_fraction` | 描述性 |
| 独立验证/PRISM | `phase_f_...csv` + `final_top10.csv` 冗余列 | 只作背景，不作因果 |
| 分项证据强弱 | `evidence_grade_matrix.csv` | 用于“可信度与完整性”卡片 |

### B2. 药物身份

| 字段 | 来源 |
|---|---|
| pert_id（LINCS） | Top10 CSV |
| ChEMBL ID / Drug 节点 | KG06 `candidate.compound_id`（如 `CHEMBL272980`） |
| 映射状态 | KG06 `candidate.mapping_status` + `coverage.compound_mapped` |
| 未映射药物 | PHA-767491、PFI-1：`compound_mapped=false`，`paths=[]`（交接文档已确认身份但未入正式图谱） |

### B3. 靶点证据

- 来源：Neo4j `Candidate-[:REFERENCES_DRUG]->Drug-[:TARGETS]->Gene`，固定 Cypher，按 `graph_build_id` 过滤。
- 字段：`target_gene_id`(Ensembl)、`gene_label`(Symbol)、`reported_actions`（仅当来源明确写出 INHIBITOR/ACTIVATOR 才可用）、`evidence_ids`、`source_release`、`source_record_id`、`evidence_complete`。
- 当前图谱通路归属以 Ensembl ID 为准；Symbol 仅用于展示与 S1 连接。

### B4. S1 疾病基因证据

- 图谱内：`HAS_DEG` 边携带 `direction/logFC/adj.P.Val/t/gene_symbol`（实测 HDAC1：logFC=1.0965, FDR=0.000133, UP）。
- 数值补全：以 `gene_symbol` 连接 `limma_all_genes.csv`，补 `P.Value` 与原始行，保证 logFC/FDR/P 三者齐全。
- 规则：只有实际出现在 `HAS_DEG` 或 limma 表中的基因，才能说“本项目差异表达”；仅靶点关系不含表达证据的，必须标注“未在本项目差异基因中验证”。

### B5. 通路证据

- 图谱内：`Pathway/GeneSet -[:CONTAINS_GENE]-> Gene`；`DiseaseContext -[:HAS_ENRICHED_PATHWAY]-> Pathway`；`Pathway -[:HAS_ENRICHED_GENE]-> Gene`。
- 数值：路径 `pathway_adjusted_p_values`（实测 0.00401）；名称来自节点 label（如 `GO:0016032 viral process`）。
- 缺失即标缺失，不让模型补造通路。

### B6. 实测的“三档证据”候选（用于验收）

| 档位 | 候选 | 靶点路径 | 公共通路 | S1 富集交汇 | S1 DEG 交汇 | 唯一证据 | 说明 |
|---|---|---:|---:|---:|---:|---:|---|
| 充分 | ponatinib | 21 | 0 | 131 | 5 | 258 | 证据最完整 |
| 充分 | JNJ-26481585 | 11 | 12 | 76 | 4 | 178 | 通路类型齐全 |
| 中等 | mocetinostat | 5 | 12 | 37 | 1 | 94 | 有靶点与通路 |
| 不完整 | MK-2461 | 1 | 0 | 0 | 0 | 3 | 单靶点、无通路 |
| 不完整 | rebastinib | 1 | 0 | 0 | 0 | 3 | 单靶点 ABL1 |
| 不足 | PHA-767491 | 0 | 0 | 0 | 0 | 0 | 未映射 |
| 不足 | PFI-1 | 0 | 0 | 0 | 0 | 0 | 未映射 |

### B7. 已知数据缺口（必须如实呈现）

1. KG06 `candidate.rank` 只对部分药物存在（Exp006 Top50 未覆盖全部 Top10），**不可当排名**。
2. KG06 证据包内 Exp006 / Phase F / PRISM 数值**没有稳定 EvidenceRecord/Path ID**，不能作为“可引用事实”，只能作为背景展示。
3. 页面静态 Top10 使用的图谱版本（a6aa…）与实时包（d570…）不同，证据数量会变化。
4. `GET /kg06/coverage` 当前 409（审计清单绑定 a080…，与 d570… 不一致）；本功能**不依赖**该接口。

---

## C. 推荐的前后端架构

### C1. 设计原则

- 只读现有研究数据；新增结果单独存放；不触碰 S1/S2/Top10/GraphBuild/Neo4j。
- 证据由后端整理，模型只看到“已筛选、带 ID 的证据 JSON”。
- 所有模型声明必须带 Evidence ID / Path ID；无引用即丢弃。
- 证据分层（A/B/C/D）由**后端规则**判定，模型只能提建议。

### C2. 后端模块（新增，不改现有）

```
backend/app/interpretation/
├── __init__.py
├── evidence.py     # 证据收集：Top10权威行 + KG06包 + S1数值 + 通路 + 分项等级
├── prompt.py       # System/User Prompt、六段式 JSON Schema、科学约束
├── provider.py     # DeepSeek OpenAI 兼容客户端（超时/重试/错误分类）
├── validator.py    # 结构校验、引用白名单、层级重算、语言降级
├── service.py      # 编排 + 单飞锁 + 缓存 + 结果组装
├── schemas.py      # 请求/响应 Pydantic 模型
└── router.py       # APIRouter
```

复用而不重写：

- `app.kg06.build_evidence_package()`：继续做 GraphBuild 隔离取证。
- `app.s1_genes._read_rows()`：取 logFC/P/FDR（只读缓存）。
- `app.s1_pathways`：读取 GO/Reactome 富集表补名称与 FDR。
- `app.main._hash_file` / manifest 校验模式：校验 Top10 CSV 与 Phase F。

### C3. 请求流程

```
FinalResultsPage 选中候选
  → POST /api/v1/projects/{pid}/drug-interpretation  {pert_id, graph_build_id?}
  → 校验 project / pert_id / build
  → 读冻结 Top10 行（rank/score）
  → 读 KG06 证据包（靶点、路径、S2 条件、gaps/coverage）
  → 连接 S1 数值（logFC/P/FDR/direction）与通路 FDR
  → 组装 EvidenceBundle（按 A/B/C/D 预处理）
  → 紧凑化（targets≤8, pathways≤12, conditions≤8, claims 限额）
  → DeepSeek Flash → JSON
  → 校验：结构 / 引用 / 层级 / 语言 / 缺失声明
  → 组装六段式报告 + 审计信息
  → 文件缓存（key=evidence_sha+model+prompt_version+mode）
  → 返回前端
```

### C4. 关键安全边界

- 前端只调用本项目后端，绝不携带 Key。
- 模型不接触 PostgreSQL/Neo4j，不执行 Cypher。
- 结果只写独立缓存/独立表；不写回任何原始分析表。
- 生成失败不影响其他路由（异常在 router 内捕获为 503/502）。

---

## D. API 请求与响应格式

### D1. 请求

```http
POST /api/v1/projects/{project_id}/drug-interpretation
Content-Type: application/json
```

```json
{
  "drug_id": "BRD-K44227013",
  "graph_build_id": null,
  "force_regenerate": false,
  "mode": "deepseek"
}
```

校验：`drug_id` 必须是该项目 Top10 冻结文件中的 pert_id；后端自行查 rank、靶点、分数，**不信任前端传来的 rank/score/targets**。

### D2. 响应（建议字段，最终以实际结构为准）

```json
{
  "project_id": "...",
  "graph_build_id": "GRAPH_BUILD:...:d570eb8deb3e5eea",
  "drug_id": "BRD-K44227013",
  "drug_name": "ponatinib",
  "rank": 1,
  "screening": {"score_name": "WTCS median B", "score_value": -0.5166541987, "n_signatures": 1, "n_dose_conditions": 5, "negative_dose_fraction": 0.2},
  "model_name": "deepseek-flash",
  "prompt_version": "drug-interpretation-v1.0",
  "generated_at": "2026-11-20T…Z",
  "cached": false,
  "evidence_input_sha256": "…",
  "evidence_summary": {
    "mapped": true, "compound_id": "CHEMBL...", "target_count": 21,
    "s1_deg_target_count": 5, "pathway_count": 143, "s2_condition_count": 5,
    "unique_evidence_count": 258, "layer_counts": {"A": 26, "B": 131, "C": 0, "D": 3},
    "graph_build_matches_page": false, "page_graph_build_id": "…a6aabb2147cb5f5e"
  },
  "screening_rationale": {"text": "...", "layer": "A", "evidence_refs": [...]},
  "target_mechanism": {
    "text": "...", "layer": "A|B",
    "targets": [{"symbol":"HDAC1","gene_id":"ENSG...","relation":"association","evidence_ids":["..."],
                 "s1":{"logFC":1.0965,"p_value":null,"fdr":0.000133,"direction":"UP"}}]
  },
  "pathway_association": {"text":"...","layer":"B","pathways":[{"id":"GO:0016032","label":"viral process","adjusted_p":0.00401,"target_gene":"HDAC1","path_ids":["..."],"evidence_ids":["..."]}]},
  "evidence_assessment": {"verdict":"sufficient|limited|insufficient","basis":"...","direct_count":26,"indirect_count":131,"missing":["..."]},
  "limitations": ["..."],
  "validation_suggestions": ["..."],
  "evidence_references": [{"evidence_id":"...","path_id":"...","source_release":"...","source_record_id":"...","verified_in_postgres":true,"layer":"A"}],
  "claim_audit": {"dropped":[...],"downgraded":[...],"invalid_citations":[...]},
  "disclaimer": "科研辅助解释，不构成临床用药建议"
}
```

### D3. 错误码

| 场景 | HTTP | 前端提示 |
|---|---|---|
| 项目不存在 | 404 | 项目不存在 |
| 药物不在 Top10 | 404 | 该候选不属于当前 Top10 |
| 无 Key / Key 无效 | 503 | AI 服务未配置，请联系管理员（不暴露细节） |
| 模型超时/连接失败 | 503 | AI 服务暂时不可用 |
| 模型返回非 JSON/字段缺失 | 502 | 解释格式校验失败，已保留原始结果 |
| 空证据（未映射） | 200 + `verdict=insufficient` | 明确显示“证据不足”，不生成机制断言 |
| 重复点击 | 200 + `cached=true` / 409 进行中 | 按钮置灰 + 提示 |

---

## E. Prompt 设计方案

### E1. System Prompt（要点）

1. 角色：药物重定位研究证据整理助手，只解释输入 JSON，不引入任何外部知识。
2. 禁止：虚构靶点/基因/通路/实验/文献/DOI；把一般医学知识说成项目结果；把关联写成因果；给临床建议。
3. 每个 claim 必须带 `evidence_ids` 或 `path_ids`；无引用则输出空数组并写入 `insufficient_evidence`。
4. 关系词：仅 `binding/inhibition/activation/association/unknown`；只有来源写明方向才可用前三种。
5. 证据分层：`A` 直接项目证据、`B` 间接关联、`C` 推断、`D` 缺失；C 必须显式标注“假设”。
6. 输出严格 JSON，不要 Markdown、不要解释。
7. 每段最多 3 条，每条 ≤ 60 个汉字；总输出 ≤ 1200 tokens。

### E2. User Prompt 结构（证据 JSON）

```
{
  "task": "解释该候选药物的现有项目证据，不重新筛选",
  "candidate": {drug_name, pert_id, rank, score, score_name, n_signatures, n_conditions},
  "targets": [{symbol, gene_id, relation, evidence_ids, s1:{logFC,p,fdr,direction}|null}],
  "pathways": [{id, label, adjusted_p, target_gene, path_ids, evidence_ids}],
  "s2_conditions": [{condition_key, cell_id, dose, time, WTCS_median, evidence_ids}],
  "coverage": {...}, "evidence_gaps": [...],
  "grade_matrix": {...},
  "rules": "只能使用以上数据；缺失写缺失；不得补造"
}
```

### E3. 六段式输出 Schema

```json
{
  "screening_rationale": [{"text":"","evidence_ids":[],"path_ids":[],"layer":"A"}],
  "target_mechanism":   [{"text":"","evidence_ids":[],"path_ids":[],"layer":"A|B|C"}],
  "pathway_association":[{"text":"","evidence_ids":[],"path_ids":[],"layer":"B"}],
  "er_positive_relevance":[{"text":"","evidence_ids":[],"path_ids":[],"layer":"B|C"}],
  "evidence_assessment": {"verdict":"","basis":"","missing":[]},
  "limitations": ["..."],
  "validation_suggestions": ["..."],
  "insufficient_evidence": [{"text":"","evidence_ids":[],"path_ids":[]}]
}
```

### E4. 服务端后处理（模型无权决定）

- 引用不在白名单 → 删除该 ID；claim 无引用 → 丢弃并记录。
- `layer` 由后端按引用类型重算：
  - 引用 `TARGETS / REFERENCES_DRUG / HAS_S2_CONDITION / HAS_DEG` → A
  - 引用 `HAS_ENRICHED_GENE + HAS_ENRICHED_PATHWAY` 组合 → B
  - `target_mechanism` 中标记 hypothesis 且仅引用上述路径作为“依据” → C
  - 无引用缺口 → D（由 `evidence_gaps` 生成，不由模型写）
- 出现“已证实/必然/确定有效/治愈/推荐用药” → 降级为“提示/可能/待验证”，并标记 `downgraded`。
- `verdict` 由后端按覆盖度计算（见 F3），不采信模型自评；禁止百分比置信度。

---

## F. 前端页面布局方案

### F1. 入口位置

- 在 `FinalResultsPage` 的「Top 10 排名」表格下方、当前选中药物摘要卡片内，新增按钮 **「生成 AI 机制解释」**（主按钮）+「重新生成」。
- 保留 `DrugPage` 现有 KG07 入口不动；可选后续把新面板复用到 DrugPage（Stage 3 再定）。

### F2. 卡片结构（沿用现有设计语言：白卡片、16px 圆角、低饱和蓝）

顶部信息条：

`药物名 · pert_id` | `Top10 排名` | `WTCS` | `DeepSeek Flash` | `生成时间` | `GraphBuild（当前 d570…）`

六张卡片（两列网格，窄屏单列）：

1. 为什么筛选出该药物？（排名、WTCS 含义、条件数、分数能/不能支持什么）
2. 潜在药物靶点（靶点表：Symbol / Gene ID / 关系 / S1 方向 / logFC / FDR）
3. 潜在作用机制（A/B/C 分层表述，C 类带“假设”标签）
4. 与 ER 阳性乳腺癌的相关性（只引用项目证据，不推断跨癌种疗效）
5. 证据可信度与完整性（充分/有限/不足 + 判断依据 + 缺失项）
6. 局限性与后续验证建议

底部：`evidence_references` 折叠表 + 导出 JSON。

### F3. 判定规则（后端，前端只展示）

| verdict | 规则（建议） |
|---|---|
| 证据充分 | ≥1 直接靶点 且 ≥1 S1 富集通路 且 唯一证据 ≥20 |
| 证据有限 | 有靶点但无 S1 通路，或有通路但靶点 <2 |
| 证据不足 | 未映射 / 无靶点 / 无任何路径 |

### F4. 证据分层视觉（低饱和，不夸张）

| 层 | 标签 | 颜色 | 用途 |
|---|---|---|---|
| A 直接证据 | `A 直接证据` | 品牌蓝 `#2878D0` | 图谱直接关系、S2 记录、S1 数值 |
| B 间接关联 | `B 间接关联` | 灰 `#6E6E73` | 靶点→S1 通路交汇 |
| C AI 推断 | `C AI 推断` | 琥珀 `#B87928` | 机制假设，必须带“待验证” |
| D 当前缺失 | `D 当前缺失` | 浅红 `#BD4C4C` | 证据缺口 |

- 每条 claim 右侧挂 `Evidence` / `Path` 按钮：证据 → 复用现有证据弹窗；路径 → 跳转 `/graph?pert_id=…&path_id=…&graph_build_id=…`。
- 若 `graph_build_matches_page=false`，顶部显示一条黄色提示：**“AI 解释基于当前图谱版本 d570…，页面历史证据基于 a6aa…，数量差异属正常版本差异。”**

### F5. 交互

- 切换药物：清空上一份报告（React Query key 含 `pert_id`），避免串数据。
- 加载：按钮 loading + 骨架/提示；错误：`Alert` 显示友好文案，不展示原始堆栈。
- 空证据：直接显示“证据不足”卡片，不显示空白。
- 禁重复：请求中禁用按钮；后端单飞锁兜底。
- 不影响原 Top10 表格、图谱页、S2 页。

---

## G. 文件修改清单

### G1. 新增（后端）

| 文件 | 作用 |
|---|---|
| `backend/app/interpretation/__init__.py` | 包入口 |
| `backend/app/interpretation/evidence.py` | 证据收集与 A/B/C/D 预分类 |
| `backend/app/interpretation/prompt.py` | Prompt 与 JSON Schema |
| `backend/app/interpretation/provider.py` | DeepSeek Flash 客户端 |
| `backend/app/interpretation/validator.py` | 结构/引用/层级/语言校验 |
| `backend/app/interpretation/service.py` | 编排、缓存、单飞 |
| `backend/app/interpretation/schemas.py` | 请求/响应模型 |
| `backend/app/interpretation/router.py` | 路由 |
| `backend/tests/test_drug_interpretation.py` | 后端测试（Mock 模型） |
| `backend/tests/fixtures/`（如需） | 三种证据档位样例 |

### G2. 新增（前端）

| 文件 | 作用 |
|---|---|
| `frontend/src/components/DrugInterpretationPanel.tsx` | 六卡片面板 + 证据分层 + 引用按钮 |
| `frontend/src/components/EvidenceLayerTag.tsx`（可选） | 分层标签 |
| `frontend/src/api.ts`（追加类型/方法） | `drugInterpretation()` |

### G3. 新增（文档/配置）

| 文件 | 说明 |
|---|---|
| `docs/ai_drug_interpretation_stage1_design.md` | 本文档 |
| `.env.example`（追加 3 行） | 仅变量名与占位符 |
| `docs/api/api_contract.yaml`（追加路径） | 接口契约 |

### G4. 修改（最小化）

| 文件 | 改动 |
|---|---|
| `backend/app/core/config.py` | 追加 `deepseek_api_key/base_url/model`（默认空，不影响现有） |
| `backend/app/main.py` | 仅 `include_router(interpretation_router)` 数行 |
| `frontend/src/pages/FinalResultsPage.tsx` | 选中药物卡片内嵌入口与面板 |
| `frontend/src/styles.css` | 少量分层标签样式（或直接用 Ant `Tag`） |
| （可选）`frontend/src/pages/DrugPage.tsx` | 复用新面板，保留旧 KG07 |
| （可选）`backend/app/models.py` + 新迁移 | 仅在确认需要持久化时 |

### G5. 明确不改

`kg06.py`、`kg07.py`（现有行为）、`s1_*`、GraphBuild/Neo4j 导入脚本、`experiments/**` 结果文件、`data/processed/**`、Top10 CSV/JSON、S2 表与排名逻辑。

---

## H. 测试方案

### H1. 后端（pytest，默认不消耗真实 Token）

| # | 用例 | 断言 |
|---|---|---|
| 1 | 证据收集：ponatinib | 21 靶点、131 通路、5 DEG 命中、证据 ID 全部可核验 |
| 2 | 证据收集：MK-2461 | 1 靶点、0 通路、`gaps` 含“no target-to-S1…” |
| 3 | 证据收集：PFI-1 | `mapped=false`、`paths=[]`、`verdict=insufficient` |
| 4 | 排名权威 | 返回 rank 等于冻结 CSV，而非 DB `Candidate.rank` |
| 5 | Prompt 约束 | System 含“不得虚构/不得因果/无引用丢弃/临床建议禁止” |
| 6 | 正常 JSON | Mock 返回 → 解析成功、六段齐全、引用通过 |
| 7 | 非 JSON | Mock 返回 HTML/截断 → 502，缓存不写坏数据 |
| 8 | 伪造引用 | 模型给 `fake` ID → 删除、claim 丢弃、`claim_audit` 记录 |
| 9 | 因果降级 | “已证实可治疗” → 降级并标记 |
| 10 | 层级重算 | 只引用 B 类路径的 claim 不得标 A |
| 11 | Key 边界 | 响应/日志不含 Key；未配置时 503 |
| 12 | 只读性 | 生成前后 S1/S2/Top10/GraphBuild 行数与哈希不变 |
| 13 | 并发点击 | 同 key 第二次命中缓存或返回 409，不重复调用 |
| 14 | 空证据 | 不调用模型，直接返回 insufficient（节省 Token） |

### H2. 前端

| # | 检查 |
|---|---|
| 1 | Top10 表格、排序、下载、证据弹窗均正常 |
| 2 | 选中药物 → 按钮可用 → 请求参数正确 |
| 3 | 加载态、错误态、空证据态 |
| 4 | 六卡片完整渲染，A/B/C/D 标签正确 |
| 5 | 切换药物后清空上一份报告 |
| 6 | `Evidence` 弹窗 / `Path` 跳转可用 |
| 7 | `npm run build`（tsc + vite）通过 |
| 8 | 图谱页、S2 页、S1 页回归正常 |

> 当前前端无测试框架（`package.json` 仅 dev/build/preview）。Stage 3 采用“手工清单 + 构建校验”；如需自动化，另行确认是否引入 Vitest/Playwright（会增加依赖）。

### H3. 科学合理性（真实三档，Mock + 一次授权实测）

| 档位 | 药物 | 期望 |
|---|---|---|
| 充分 | ponatinib / JNJ-26481585 | 有靶点、有通路，结论克制，不出现疗效断言 |
| 不完整 | MK-2461 / rebastinib | 明确“单靶点、无通路证据”，机制假设标 C |
| 不足 | PFI-1 / PHA-767491 | 明确“当前项目未提供足够证据”，不生成机制 |

检查点：是否编造靶点/通路/实验；是否把 B 类说成 A 类；是否出现“已证实/必然抑制”；是否给用药建议。

### H4. 最终验收流程

Top10 页 → 选药 → 生成 → 后端读真实证据 → DeepSeek Flash → 结构化返回 → 六卡片展示 → 引用可点开 → 导出 JSON。

---

## I. 潜在风险与回滚方案

| # | 风险 | 影响 | 缓解 |
|---|---|---|---|
| 1 | 图谱版本漂移（a6aa/a080/d570） | 页面数字与 AI 证据不一致 | 面板显示 build ID + 差异提示；只读实时包 |
| 2 | DB `Candidate.rank` 是种子子图 | 排名错误 | 排名只用冻结 Top10 CSV，后端强校验 |
| 3 | 未配置 DeepSeek Key | 功能不可用 | 503 明确提示；不自动换模型 |
| 4 | 模型 ID 变更（V4→V4.1） | 调用失败 | `DEEPSEEK_MODEL` 可配置，默认 `deepseek-flash` |
| 5 | 幻觉 | 科学错误 | 引用白名单 + 无引用丢弃 + 层级重算 + 语言降级 |
| 6 | 证据过大（108k token 前车之鉴） | 超时/截断 | 紧凑化：靶点≤8、通路≤12、条件≤8、字段裁剪 |
| 7 | 重复点击/并发 | 费用与限流 | 前端禁用 + 后端单飞锁 + 缓存 |
| 8 | 缓存陈旧 | 证据更新后仍旧结果 | key 含 evidence_sha + model + prompt_version；支持 force_regenerate |
| 9 | 新增表迁移 | 数据风险 | 默认**不加表**，先用文件缓存；确需持久化再单独评审 |
| 10 | 误改现有结果 | 破坏验收 | 所有写入限定在新目录/新表；测试校验行数与哈希不变 |

### 回滚

1. 前端：移除 `FinalResultsPage` 中的面板与入口（2 处 import/JSX），删除 `DrugInterpretationPanel.tsx`。
2. 后端：删除 `app/interpretation/`，移除 `main.py` 一行 `include_router`，删除 tests。
3. 配置：删除 `.env` 中 3 个 DeepSeek 变量（或置空）。
4. 缓存：删除 `experiments/drug_interpretation_cache/`（如启用）。
5. 若已加表：`alembic downgrade -1`（迁移内含 drop table），不影响既有表。
6. 全程不触碰 S1/S2/Top10/GraphBuild/Neo4j，因此无需数据回滚。

---

## J. 分阶段实施计划（待确认后执行）

| 阶段 | 内容 | 交付 |
|---|---|---|
| 3.1 | 配置 + 证据收集模块 + 单测 1–5 | 证据 JSON 样例、行数不变证明 |
| 3.2 | DeepSeek provider + Prompt + 校验 + 单测 6–12 | Mock 全绿，真实调用 1 次（需授权） |
| 3.3 | Router + 缓存 + 错误码 | OpenAPI 文档、curl 示例 |
| 3.4 | 前端面板 + 交互 + 构建 | 截图、`npm run build` |
| 3.5 | 三档科学合理性 + 回归 | 测试报告 |
| 3.6 | 最终验收 + 使用说明 + 回滚演练 | 交付清单 |

---

## K. 需要你确认的 4 件事（确认后我才进入 Stage 3）

1. **模型与 Key**：确认使用 `deepseek-flash`（Base `https://api.deepseek.com`），并由你在后端 `.env` 配置 `DEEPSEEK_API_KEY`。是否需要我先写好 `.env.example` 占位与配置读取（不写入真实 Key）？
2. **证据版本口径**：AI 面板读取**实时 GraphBuild（d570…）**，并提示与页面历史版本（a6aa…）的差异；还是要求严格对齐页面历史版本（需另建/恢复对应图谱，成本高）？建议前者。
3. **排名口径**：确认排名一律取冻结 Top10 CSV（rank 1–10），不使用数据库 `Candidate.rank`。
4. **是否新增数据库表**：建议先用独立文件缓存、**不建表、不迁移**；若需要多人共享/审计留痕，再单独确认表结构与回滚。

> 在收到确认前，我不会修改任何代码、不执行迁移、不调用真实模型 API。
