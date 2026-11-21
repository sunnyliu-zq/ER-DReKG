# 可解释性成果协作中心 —— 最小改造方案（待确认）

> 状态：**方案设计阶段，尚未改动任何现有代码**。本方案基于对 `ER-DReKG` 仓库实际代码的核查编写，确认后再按阶段实施。
> 适用项目：ER-DReKG（ER 阳性乳腺癌药物重定位知识图谱平台）。

## 1. 本次目标

在现有网站上增加一个「可解释性成果协作中心」，让小组成员登录后可以：

1. 上传药物机制解释文档和文字说明。
2. 上传 CSV / JSON 等靶点、通路、分析结果数据文件。
3. 上传 PNG / SVG 分析图表。
4. 把上传内容关联到对应候选药物。
5. 查看、修改已提交的分析内容；修改保留历史版本。
6. 提交后网站直接展示更新，无需管理员逐项审核。
7. 记录提交人、提交时间和历史版本。

## 2. 现状核查（来自实际代码，非假设）

| 维度 | 实际情况 | 代码位置 |
| --- | --- | --- |
| 后端框架 | FastAPI，单文件主应用；SQLAlchemy 2.0 ORM；Alembic 迁移；PostgreSQL 17（测试用 SQLite） | `backend/app/main.py`、`backend/app/db.py`、`backend/migrations/` |
| 前端框架 | **React 19 + TypeScript + Vite + Ant Design 6**；路由 `react-router-dom`；请求库 `@tanstack/react-query`；API 集中封装 | `frontend/src/api.ts`、`frontend/src/App.tsx`、`frontend/src/pages/` |
| 现有数据表 | `projects`、`studies`、`analysis_runs`、`graph_builds`、`evidence_records`、`disease_contexts`、`datasets`、`candidates`、`s2_condition_results` | `backend/app/models.py` |
| 认证现状 | **无任何登录/鉴权**：后端接口裸奔，前端 fetch 不带凭证；无用户表 | 全仓检索无 `OAuth2 / Bearer / passlib / jose` |
| 现有上传逻辑 | 流式分块写盘 + SHA-256 + 大小限额 + 扩展名白名单，落盘 `data/raw/uploads/` | `backend/app/main.py`（`upload_dataset`）、`backend/app/ingestion.py`（`UploadHasher`、`write_request_stream`） |
| 候选药物标识 | 项目内以 `candidates.pert_id` 标识药物，`drug_name` 为显示名 | `backend/app/models.py`（`Candidate`） |
| 图谱存储 | Neo4j 仅做只读查询；导入/校验脚本独立 | `backend/app/graph_repository.py`、`kg/` |
| 前端代理 | Vite 把 `/api` 代理到 `127.0.0.1:18000` | `frontend/vite.config.ts` |
| 部署文件 | 仓库内**无** Dockerfile / nginx / systemd / PM2 脚本；`docker-compose.yml` 只起 Postgres + Neo4j；后端 uvicorn，前端 `npm run build` 出 `dist` | 根目录、`docs/REPRODUCIBILITY.md` |
| 迁移链 head | 当前最新版本 `e4f62ab7093c`（`down_revision = d7e15b5a42c1`） | `backend/migrations/versions/` |

> 与需求描述的两点出入：前端实际是 **React**（不是 Vue）；现状**没有**任何认证机制，需要从零增加。

## 3. 硬约束与隔离映射

| 约束 | 本方案做法 |
| --- | --- |
| 不修改/覆盖现有正式 GraphBundle | 协作内容不写入 `graph_builds`、`evidence_records`、`disease_contexts`，不触碰任何图谱快照记录 |
| 不清空、不重建、不重新导入 Neo4j | 新功能**完全不连接 Neo4j**；后端不新增任何 Cypher 写入 |
| 不破坏现有 PostgreSQL 数据 | 只**新增独立表**，不 ALTER 现有表、不改现有列；迁移可 `downgrade` 回滚 |
| 新增内容独立存储、与原始图谱分离 | 元数据进新表；附件进独立目录 `data/collab_uploads/`，与 `data/raw/uploads/` 分开 |
| 必须有成员身份认证，禁止匿名写入 | 新增成员表 + 登录签发签名凭证；所有协作写接口强制鉴权 |
| 限制上传类型和大小，防止任意文件执行 | 扩展名白名单 + 文件头/内容校验 + 分级大小上限 + 随机文件名 + 禁可执行后缀 |
| 尽量复用现有代码、最小改造 | 复用现有流式写盘/哈希/限额模式、ORM 与迁移惯例、antd 组件与请求封装；后端以独立路由模块接入，尽量不改 `main.py` |
| 不需要 GitHub 工作流 | 不新增 CI/CD 配置 |
| 不允许成员上传/执行前端代码 | 仅接受文档/数据/图片三类附件，不接收脚本、压缩包、网页文件 |

## 4. 默认决策（如无异议按此执行，可推翻）

> 上一轮确认提问未收到回复，以下采用最稳妥、最少依赖的默认项。

| 决策点 | 默认选择 | 理由 |
| --- | --- | --- |
| 认证实现 | **Python 标准库零依赖**：`hashlib.pbkdf2_hmac` 存口令，`hmac` 签名凭证 | 不新增 pip 依赖，满足小组内部登录与禁止匿名写入 |
| 账号开通 | **CLI 种子创建首个管理员，管理员站内创建成员**；不开放自助注册 | 内部小组可控，避免匿名注册 |
| 接口权限 | **读写均需登录**（协作中心内容仅成员可见） | 符合内部小组定位，避免成果外泄 |
| 文档格式 | 文档 `.md / .txt`；数据 `.csv / .tsv / .json`；图表 `.png / .svg`；暂不支持 `.docx` | 纯文本可直接校验与预览，避免引入文档解析依赖 |

## 5. 数据模型设计（全部为新增独立表）

> 命名沿用现有惯例：主键 `String(36)` UUID，时间戳 `DateTime(timezone=True)`，JSON 字段存附加信息。新表与现有表之间**只对 `projects.project_id` 建外键**；与候选药物通过 `(project_id, pert_id)` 软关联，提交时校验 `pert_id` 属于本项目候选，不建硬外键到 `candidates`，避免耦合与级联删除风险。

### 5.1 `collab_members`（协作成员）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `member_id` | String(36) PK | UUID |
| `username` | String(64)，唯一索引 | 登录名 |
| `password_hash` | String(256) | 格式 `pbkdf2$迭代次数$盐(hex)$哈希(hex)` |
| `display_name` | String(120) | 页面显示名 |
| `role` | String(20)，默认 `member` | `member` / `admin` |
| `is_active` | Boolean，默认 True | 停用后不可登录 |
| `created_at` | DateTime(tz) | 创建时间 |
| `last_login_at` | DateTime(tz)，可空 | 最近登录时间 |

### 5.2 `collab_posts`（成果主记录，当前版本）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `post_id` | String(36) PK | UUID |
| `project_id` | FK `projects.project_id` ON DELETE CASCADE，索引 | 所属项目 |
| `pert_id` | String(100)，索引，可空 | 关联候选药物；为空表示项目级说明 |
| `title` | String(250) | 成果标题 |
| `summary_text` | Text | 文字说明（当前版本） |
| `status` | String(20)，默认 `active` | `active` / `archived` |
| `current_version` | Integer，默认 1 | 当前版本号 |
| `author_id` | FK `collab_members.member_id` ON DELETE SET NULL，可空 | 首作者 |
| `is_deleted` | Boolean，默认 False，索引 | 软删除标记 |
| `created_at` | DateTime(tz) | 首次提交时间 |
| `updated_at` | DateTime(tz) | 最近修改时间 |

### 5.3 `collab_post_versions`（历史版本快照）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `version_id` | String(36) PK | UUID |
| `post_id` | FK `collab_posts.post_id` ON DELETE CASCADE，索引 | 所属成果 |
| `version` | Integer | 版本号，与 `post_id` 唯一约束 |
| `title` | String(250) | 该版本标题快照 |
| `summary_text` | Text | 该版本文字说明快照 |
| `change_note` | String(500)，默认 `""` | 本次修改说明 |
| `author_id` | FK `collab_members.member_id` ON DELETE SET NULL，可空 | 该版本提交人 |
| `created_at` | DateTime(tz) | 该版本提交时间 |

> 每次修改都向本表追加一行新版本，主表 `current_version` 指到最新；旧版本永不覆盖。

### 5.4 `collab_attachments`（附件元数据）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `attachment_id` | String(36) PK | UUID |
| `post_id` | FK `collab_posts.post_id` ON DELETE CASCADE，索引 | 所属成果 |
| `kind` | String(20) | `doc` / `data` / `figure` |
| `original_filename` | String(255) | 原始文件名（展示用） |
| `stored_path` | String(500) | 相对协作上传根目录的存储路径 |
| `ext` | String(16) | 小写扩展名 |
| `mime_type` | String(100) | 校验后的 MIME |
| `file_bytes` | Integer | 字节数 |
| `sha256` | String(64) | 文件哈希 |
| `uploaded_by` | FK `collab_members.member_id` ON DELETE SET NULL，可空 | 上传人 |
| `created_at` | DateTime(tz) | 上传时间 |

## 6. 后端设计

### 6.1 新增/修改文件（最小改动）

| 文件 | 动作 | 内容 |
| --- | --- | --- |
| `backend/app/core/config.py` | 小改 | 新增 `collab_upload_dir`、`collab_secret_key`、各类附件大小上限配置项 |
| `backend/app/models.py` | 追加 | 上述 4 个新模型类（不动现有模型） |
| `backend/app/schemas.py` | 追加 | 成员登录/输出、成果创建/修改/输出、附件输出等 Pydantic 模型 |
| `backend/app/collab_auth.py` | 新增 | 口令哈希校验、凭证签发与解析、`get_current_member` 鉴权依赖 |
| `backend/app/collab_service.py` | 新增 | 成果/版本/附件的业务逻辑：关联药物校验、版本追加、落盘与校验 |
| `backend/app/routers/collab.py` | 新增 | 协作中心路由，挂载到 `app`，前缀 `/api/v1/collab` |
| `backend/app/main.py` | 极小改 | 仅 `include_router` 一行；不改动现有路由 |
| `backend/migrations/versions/<rev>_add_collab_tables.py` | 新增 | Alembic 迁移，`down_revision = "e4f62ab7093c"`，含 `upgrade()` / `downgrade()` |
| `backend/scripts/seed_collab_admin.py` | 新增 | 命令行种子脚本：创建首个管理员（用户名/口令从参数或环境读取） |
| `backend/tests/test_collab_api.py` | 新增 | 鉴权、CRUD、版本、附件类型/大小拦截、隔离性测试 |

### 6.2 接口清单（前缀 `/api/v1/collab`）

| 方法 | 路径 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| POST | `/auth/login` | 否（登录本身） | 用户名 + 口令 → 签名凭证 + 成员信息 |
| GET | `/auth/me` | 是 | 返回当前登录成员 |
| GET | `/projects/{project_id}/posts` | 是 | 成果列表，可按 `pert_id`、关键词过滤 |
| POST | `/projects/{project_id}/posts` | 是 | 新建成果（文字说明 + 可选关联 `pert_id`） |
| GET | `/projects/{project_id}/posts/{post_id}` | 是 | 成果详情 + 版本历史 + 附件列表 |
| PATCH | `/projects/{project_id}/posts/{post_id}` | 是 | 修改标题/说明/关联药物，追加一个新版本 |
| POST | `/projects/{project_id}/posts/{post_id}/attachments` | 是 | 上传附件（按 `kind` 校验类型与大小） |
| GET | `/attachments/{attachment_id}/download` | 是 | 鉴权后返回文件（图片可预览，其余下载） |
| DELETE | `/projects/{project_id}/posts/{post_id}` | 是 | 软删除（作者本人或 admin） |
| GET | `/members` | 是（admin） | 成员列表 |
| POST | `/members` | 是（admin） | 创建成员账号 |

### 6.3 认证设计（标准库零依赖）

- 口令存储：`hashlib.pbkdf2_hmac("sha256", 口令, 随机盐, 390000)`，盐与迭代次数一并编码进 `password_hash`。
- 凭证：自签 token，结构 `base64url(payload).base64url(hmac_sha256(payload, secret))`；payload 含 `member_id`、`exp`（过期时间）。密钥取自配置 `collab_secret_key`。
- 鉴权依赖 `get_current_member`：从 `Authorization: Bearer <token>` 解析验签、查库、校验 `is_active`；失败返回 401。
- 不引入 `python-jose` / `bcrypt` / `passlib`。

### 6.4 上传安全策略

| 环节 | 规则 |
| --- | --- |
| 类型白名单 | `doc`: `.md` `.txt`；`data`: `.csv` `.tsv` `.json`；`figure`: `.png` `.svg`。其余一律 415 拒绝 |
| 内容校验 | PNG 校验文件头魔数；JSON 必须可解析；CSV/TSV/MD/TXT 按 UTF-8 文本解码；SVG 作为文本接收并在预览时通过 `<img>` 引用（不在页面内联内插），上传时拒绝含 `<script>` 的内容 |
| 大小上限 | 文档/数据单文件 ≤ 10 MB，图表 ≤ 20 MB（可在配置调整）；空文件拒绝 |
| 文件名安全 | 取 `PurePosixPath(...).name` 去除路径；落盘文件名用 `uuid4 + 扩展名`，原始名单独存库 |
| 存储隔离 | 根目录 `data/collab_uploads/<project_id>/<post_id>/`，与现有 `data/raw/uploads/` 分开 |
| 可执行防护 | 拒绝 `.exe/.bat/.sh/.py/.js/.html/.zip/.tar` 等后缀；附件目录不作为可执行 Web 根目录挂载 |
| 哈希留痕 | 复用 SHA-256 计算，存入 `sha256`，便于追溯 |

## 7. 前端设计（React 增量）

| 文件 | 动作 | 内容 |
| --- | --- | --- |
| `frontend/src/api.ts` | 追加 | 本地存取 token；请求自动带 `Authorization`；401 清除登录态；新增协作中心与认证 API 方法与类型 |
| `frontend/src/authContext.tsx` | 新增 | 登录态 Context：当前成员、登录、退出、加载态 |
| `frontend/src/components/LoginModal.tsx` | 新增 | 用户名/口令登录弹窗 |
| `frontend/src/pages/CollabPage.tsx` | 新增 | 协作中心：按药物筛选、成果列表、新建/编辑弹窗、附件上传与版本历史查看 |
| `frontend/src/components/CollabPanel.tsx` | 新增 | 挂到药物详情页的「小组成果」卡片，按当前 `pert_id` 读取成果 |
| `frontend/src/pages/DrugPage.tsx` | 小改 | 引入 `CollabPanel`，在当前药物下展示小组成果 |
| `frontend/src/App.tsx` | 小改 | 注册 `/collab` 路由；顶栏加登录/退出按钮；包裹 `AuthProvider` |
| `frontend/src/locales/zh-CN.ts` | 追加 | 导航项「协作中心」与页面文案 |

> 复用现有 antd 组件（Card、Table、Modal、Upload、Select、Tag 等）与 react-query 数据获取模式，不新增前端框架。

## 8. 分阶段实施计划

> 每个阶段独立可验证、可回滚；前一阶段验证通过后再进入下一阶段。

### 阶段 1：后端基础 —— 配置、成员表、认证

**修改文件**
- `backend/app/core/config.py`（追加配置项）
- `backend/app/models.py`（追加 `collab_members`）
- `backend/app/schemas.py`（追加成员相关模型）
- `backend/app/collab_auth.py`（新增）
- `backend/migrations/versions/<rev>_add_collab_member.py`（新增）
- `backend/scripts/seed_collab_admin.py`（新增）
- `backend/tests/test_collab_auth.py`（新增）

**运行命令**
```powershell
cd backend
uv sync
uv run alembic upgrade head
uv run python scripts/seed_collab_admin.py --username admin --display-name 管理员
uv run pytest tests/test_collab_auth.py
```

**验证方法**
- 迁移成功，`collab_members` 表创建；现有 9 张表行数不变。
- 种子脚本生成管理员；`/api/v1/collab/auth/login` 用正确口令返回 token，错误口令 401；`/auth/me` 带 token 返回成员信息，不带 token 返回 401。

**回滚**
```powershell
uv run alembic downgrade -1   # 移除 collab_members 表
```
删除本阶段新增文件即可；不影响旧表。

### 阶段 2：后端协作内容 —— 成果、版本、附件

**修改文件**
- `backend/app/models.py`（追加 `collab_posts`、`collab_post_versions`、`collab_attachments`）
- `backend/app/schemas.py`（追加成果/附件模型）
- `backend/app/collab_service.py`（新增）
- `backend/app/routers/collab.py`（新增）
- `backend/app/main.py`（仅 `include_router` 一行）
- `backend/migrations/versions/<rev>_add_collab_content.py`（新增，`down_revision` 指向上一阶段）
- `backend/tests/test_collab_api.py`（新增）

**运行命令**
```powershell
cd backend
uv run alembic upgrade head
uv run pytest tests/test_collab_api.py
```

**验证方法**
- 未登录调用写接口一律 401；登录后可新建成果、关联 `pert_id`、上传三类附件。
- 修改成果后生成新版本，版本历史可查，旧版本内容保留；提交人、时间正确记录。
- 上传可执行/不在白名单类型 → 415；超大小 → 413；空文件 → 422。
- 隔离核对：`evidence_records`、`graph_builds`、`candidates`、Neo4j 节点/边计数与改造前一致。

**回滚**
```powershell
uv run alembic downgrade -1   # 移除三张内容表
# 手动清空独立上传目录（不影响现有 data/raw/uploads）
Remove-Item -Recurse -Force ..\data\collab_uploads
```

### 阶段 3：前端登录态与协作中心页面

**修改文件**
- `frontend/src/api.ts`、`frontend/src/authContext.tsx`、`frontend/src/components/LoginModal.tsx`
- `frontend/src/pages/CollabPage.tsx`、`frontend/src/App.tsx`、`frontend/src/locales/zh-CN.ts`

**运行命令**
```powershell
cd frontend
npm ci
npm run build
```

**验证方法**
- 未登录访问协作中心跳转登录；登录后顶栏显示成员名与退出。
- 可按候选药物筛选、查看成果、新建/编辑、上传附件、查看版本历史；刷新后登录态保持。
- `npm run build` 类型检查与构建通过。

**回滚**：还原本阶段前端文件，重新 `npm run build` 部署旧版前端。

### 阶段 4：药物详情页挂载小组成果

**修改文件**
- `frontend/src/components/CollabPanel.tsx`（新增）
- `frontend/src/pages/DrugPage.tsx`（小改引入面板）

**运行命令**
```powershell
cd frontend
npm run build
```

**验证方法**
- 在药物详情页选择某候选药物，「小组成果」卡片只显示该 `pert_id` 的成果；切换药物随之刷新。
- 未登录时引导登录；编辑后返回页面可见最新版本。

**回滚**：移除 `CollabPanel` 引用与文件，重新构建。

### 阶段 5：全量回归与部署核对

**运行命令**
```powershell
cd backend; uv run pytest
cd ..\frontend; npm run build
```

**验证方法**
- 后端全部单测通过；前端构建通过；既有页面（S1/S2/图谱/药物/对比/最终结果）行为不变。
- 核对生产 Postgres 旧表计数、Neo4j 节点/边数与上线前一致。

## 9. 部署相关待确认项

仓库内没有云端部署脚本，以下信息需要你补充后我才能给出准确的上线步骤：

1. 云服务器上后端如何启动（uvicorn 命令、端口、是否 systemd / supervisor 托管）。
2. 前端 `dist` 由谁托管（Nginx 静态目录 / 后端 StaticFiles / 对象存储），以及 `/api` 反代规则。
3. 上传目录在服务器上的持久化与备份位置（`data/collab_uploads/` 需要纳入备份）。
4. `.env` 需新增 `COLLAB_SECRET_KEY`（生产环境填强随机值）。

部署时不新增 GitHub 工作流；前端重新构建后发布 `dist`，后端重启进程即可。

## 10. 风险与注意事项

- SVG 存在脚本注入风险：预览通过 `<img>` 引用后端鉴权下载地址，不在页面内联；上传时拒绝 `<script>`。
- 凭证过期时间、会话时长在 `collab_auth.py` 集中配置；生产 `collab_secret_key` 不得提交仓库。
- 协作成果属于小组内部假设性解释，页面沿用现有免责口径：计算候选与机制假设不等同临床疗效。
- 本方案不替代权限细化（如成员只能改自己的成果）；初版支持「作者本人或 admin 可编辑/删除」，其余成员可读。
