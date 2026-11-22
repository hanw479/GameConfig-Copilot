# GameConfig Copilot

**面向游戏策划的任务配置 Agent —— 检索有依据、生成有结构、写入有闸门。**

`BM25 + RRF 混合检索` · `显式状态机编排` · `MCP 风格工具层` · `确定性三层校验` · `两阶段原子写入` · `JSONL 全链路轨迹`

> **状态（2026-11-18）**：M0–M2 已完成，M3/M4 部分收尾。可运行：检索 / 校验 / 预览 / 确认写入 / 记忆 / 检索评测 / SSE API；Docker 文件已交付（本机无 Docker，未实际 build）；轨迹/指标面板 `scripts/dashboard.py` 已交付（需另装 streamlit）。
> **生成链路已用真实 DeepSeek key 验证**：2026-11-18 跑完 30 条 v2 黄金需求（FPVR 91.30%）；无 key 时仍可用离线脚本化 LLM 验证。
> **指标只写实测值**：Recall@5 **0.775**、FPVR **91.30%**（21/23 首轮）、测试 **672 passed / 1 skipped**、对抗 **5/5**；Ragas 未测，不填占位数字。

---

## 1. 这是什么

策划用自然语言描述一个任务需求，Agent 依次完成：

1. **查依据** —— 混合检索世界观 Wiki、历史任务配置片段、数值约束表；
2. **出草案** —— 输出严格符合 `TaskDraft` Schema 的结构化任务 JSON；
3. **跑校验** —— Schema → 业务规则（等级/奖励区间/前置依赖）→ 冲突检测（ID 唯一、引用存在、循环依赖）；
4. **给人看** —— 返回 diff、目标路径和指纹，停在 `awaiting_confirmation`；
5. **安全落盘** —— 确认后强制复检，`os.replace` 原子写入；覆盖时先备份，全程审计。

任何一步不合规，Agent 不写文件，而是结构化地解释：**哪条规则、来自哪份约束、建议改成多少**。

**它不是“又一个 RAG 问答”**：区别在于 Agent 有一双受控的手，并且每次“动手”都可审计、可回滚。游戏研发效能岗最关心的正是这一点——让 AI 从“给建议”走到“可审计地执行”。

---

## 2. 快速开始（真实可跑）

### 2.1 安装

要求 Python **3.10+**（MCP 传输 `fastmcp` 需要 **3.11+**）。

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate

# 可选组已在 pyproject 里声明：[dev] 测试 / [api] FastAPI / [rag] BM25 / [ui] Streamlit / [mcp] fastmcp
pip install -e ".[dev,api,rag]"
# 面板与 MCP：按需追加（mcp 需要 Python 3.11+）
pip install -e ".[ui]"
```

也提供了 Makefile（Windows 无 `make` 时按本文命令直接跑）：

```bash
make install   # pip install -e ".[dev,api,rag]"
make test      # pytest -q -p no:cacheprovider
make lint      # ruff check src tests
make eval      # gcc eval --suite retrieval
```

### 2.2 构建索引 + 检索 + 评测 + 记忆

```bash
gcc ingest --source data
# 索引已是最新（语料指纹未变）：out/index
# chunk 总数：79（constraint 15 / task 40 / wiki 24）
# BM25：bm25s；dense：noop（降级：仅 BM25 + RRF）

gcc search "铁匠老陈是什么立场" --top-k 3
# 检索「铁匠老陈是什么立场」：3 条（过滤：无；RRF k=60）
# 1. 来源：wiki:npc_profiles#六、NPC 索引  分数：0.0164
# 2. 来源：task:T_SIDE_0213#objectives  分数：0.0161
# 3. 来源：task:T_SIDE_0213#task  分数：0.0159

gcc eval --suite retrieval
# query 数：20 · 相关标注：34
# recall@5  0.7750   mrr@10 0.7625   ndcg@10 0.7382
# 报告：reports/retrieval-baseline.json

gcc memory show --designer u_1024
# 策划：u_1024  后端：json  状态：有效
# 偏好默认值 / 来源记录 / 待确认队列

gcc schema dump | head -5     # 导出 TaskDraft JSON Schema
```

自检：

```bash
pytest -q -p no:cacheprovider   # 672 passed, 1 skipped（2026-11-18；S8 基线为 547，交付时为 651）
ruff check src tests            # All checks passed!
```

> **仍是占位**：`gcc ask`（QueryAgent 命令行）和 `gcc eval --suite all` 会以退出码 2 明确提示“尚未实现”。
> 问答走 API（见 2.3），评测目前只有 `--suite retrieval`。命令本身不写盘、不改数据。

### 2.3 启动 API（生成 + 两阶段写入需要 key）

```bash
# 可选：cp .env.example .env 后填入（.env 已被 gitignore）
# PowerShell: $env:GCC_LLM_API_KEY="sk-..."; $env:GCC_MCP_MODE="write"
export GCC_LLM_API_KEY="sk-..."
export GCC_MCP_MODE=write          # 默认 readonly；写模式必须显式开启
python -m uvicorn gameconfig_copilot.api.main:app --host 127.0.0.1 --port 8000
```

```bash
curl -s http://127.0.0.1:8000/healthz
# {"status":"ok","service":"gameconfig-copilot","sessions":0,"graph_ready":false,
#  "mcp":{"connected":false,"mode":"write"}}

SID=$(curl -s -X POST http://127.0.0.1:8000/api/v1/sessions \
  -H "Content-Type: application/json" -d '{"designer_id":"u_1024"}' \
  | python -c "import json,sys; print(json.load(sys.stdin)['session_id'])")

curl -N -X POST http://127.0.0.1:8000/api/v1/messages \
  -H "Content-Type: application/json" \
  -d "{\"session_id\":\"$SID\",\"message\":\"给铁匠老陈加一条 6 级支线，去矿洞捡 4 块矿石\"}"
```

事件顺序与字段见 [docs/demo-script.md](docs/demo-script.md) §3；没有 key 时 `/messages` 返回
`event: error` + `E_INTERNAL`（消息里点名 `GCC_LLM_API_KEY`），**不会伪造生成结果**。

### 2.4 轨迹与指标面板（可选）

```bash
pip install -e ".[ui]"
streamlit run scripts/dashboard.py          # http://localhost:8501
```

三个页面：**轨迹列表 / 轨迹详情（span + attrs）/ 指标**。数据源是 `traces/traces.jsonl`
（或 `GCC_TRACE_PATH`）、`reports/*.json` 与已入库的 [docs/reports/baseline.md](docs/reports/baseline.md)。
没有 streamlit 时面板会明确报 `DashboardUnavailable`，不影响 API/CLI；也可直接用
`GET /api/v1/traces/{trace_id}`。

---

## 3. Docker 部署

```bash
docker build -t gameconfig-copilot:local .
docker run --rm -p 127.0.0.1:8000:8000 -e GCC_LLM_API_KEY=sk-... gameconfig-copilot:local

# 或 compose：默认只起 api，写模式，端口只绑 127.0.0.1
docker compose up --build

# 可选 MCP 服务（只读 HTTP，8765）：单独 build 并安装 fastmcp
docker compose --profile mcp up --build
```

镜像要点：

| 项 | 实现 |
| --- | --- |
| base | `python:3.11-slim`（fastmcp 要求 3.11） |
| 用户 | 非 root，uid/gid **10001**（`app`） |
| 依赖 | pyproject 可选组 `[dev,api,rag,ui]`；MCP 镜像由 `INSTALL_MCP=true` 追加 `[mcp]` |
| 索引 | 构建期跑 `gcc ingest`，容器启动即可离线检索 |
| healthcheck | `GET /healthz`（标准库 urllib，不依赖 curl） |
| 默认模式 | `GCC_MCP_MODE=readonly`；compose 演示时覆盖为 `write` |
| 面板 | `[ui]` 组已装进镜像（`scripts/dashboard.py` + 8501 端口），覆盖 CMD 即可启动 Streamlit |
| qdrant | 仅注释占位：dense 分支仍是 `NoopDenseStore`，放开也不会被使用 |

查看容器内写入结果（data/ 留在容器里，避免覆盖镜像语料）：

```bash
docker compose cp api:/app/data/tasks/T_SIDE_0217.json ./out/
docker compose exec api tail -1 /app/traces/commits.jsonl
```

> **验证状态**：本机没有 Docker CLI，`docker build` / `docker compose config` **未实际执行**；
> Dockerfile 与 compose 按上述契约编写，YAML 已解析通过。首次构建请预留 1–2 分钟（下载 wheel + 建索引）。

---

## 4. 实测指标与出处

| 指标 | 实测 | 口径 / 出处 | 状态 |
| --- | --- | --- | --- |
| 检索 **Recall@5** | **0.775** | 20 条标注 query / 34 个相关块；`bm25s` + RRF，`dense=noop`；[docs/reports/baseline.md](docs/reports/baseline.md) §1、[reports/retrieval-baseline.json](reports/retrieval-baseline.json) | 低于 ≥0.85 目标（dense 未接） |
| Recall@10 / Hit@5 / MRR@10 / nDCG@10 | 0.875 / 0.95 / 0.7625 / 0.7382 | 同上，一次运行 | ✅ |
| 单元/集成/端到端测试 | **547 passed / 1 skipped**（S8 基线）→ **651**（交付时）→ **672 passed / 1 skipped**（2026-11-18） | `pytest -q -p no:cacheprovider`；[docs/reports/baseline.md](docs/reports/baseline.md) §6；skipped 为 fastmcp 用例（Py3.10） | ✅ |
| 对抗集拦截 | **5/5** | `tests/fixtures/adversarial/`：越界奖励 / 重复 ID / 缺失引用 / 依赖成环 / 提示注入；逐条断言错误码 | ✅ 100% |
| 草案首次通过率（FPVR） | **91.30%（21/23）** | 2026-11-18 真实 `deepseek-chat` 跑 30 条 `requests_v2`；[docs/reports/baseline.md](docs/reports/baseline.md) §2、[reports/fpvr-baseline.json](reports/fpvr-baseline.json) | ✅ 达 ≥80% 目标 |
| 平均修复轮次 | **0.13** | 23 条期望通过样本中 2 条需修复（1 轮 / 2 轮各 1 条），修复后 100% 通过 | ✅ 达 ≤1.2 目标 |
| 危险写入有效拦截率 | **100%（5/5）** | 30 条 v2 的 5 条对抗样本；`effective_security`（错误码命中 ∪ 攻击未采纳 ∪ 结构保证） | ✅ |
| 端到端 P95 | **4083 ms** | 30 条真实生成逐样本 nearest-rank；trace 端到端 P95 2049 ms | ✅ 达 ≤15s 目标 |
| 单次成本 | **¥0.016232 / 样本** | 34 次 LLM 调用共 ¥0.486968；按 LLM trace 摊为 ¥0.014323 | ✅ 已实测 |
| Ragas faithfulness | 未测 | Ragas 尚未接入 | ⏳ |

> 2026-11-18 新增奖励下限约束 `C_REWARD_GOLD_MIN_01` / `C_REWARD_EXP_MIN_01`（`level_band_range`，
> 下限 = 同档上限 30%），Prompt 注入 `[min,max]` 区间，黄金集下限断言同步恢复为硬断言；
> 详见 [docs/reports/baseline.md §2.2](docs/reports/baseline.md#22-下限约束已加2026-11-18)。

口径细节见 [docs/05-interview-kit.md §4](docs/05-interview-kit.md#4-指标口径防止被追问翻车)。
**未测的数字不写进简历/README**——这是本仓库的硬规则。

> `reports/` 已被 `.gitignore` 忽略；**已入库的基线**在 [docs/reports/baseline.md](docs/reports/baseline.md)（S7-08）。
> 换机器复现请先跑 2.2 的两条命令，再对照基线。

---

## 5. 已知限制（按影响排序）

1. **dense 分支未接**：`NoopDenseStore`，检索 = BM25 + RRF；Recall@5 0.775 是**降级基线**。BGE-M3 / Chroma / Qdrant adapter 尚未接线（`rag/stores/` 已留 Protocol 与占位）。
2. **没有真实 key 就没有 live 生成**：当前交付不含 LLM 响应录制回放；无 key 时 `/messages` 明确报错。离线验收靠 `tests/support/fakes.py` + [demo-script 附录 A](docs/demo-script.md#附录-a--无-key-离线复现mock实测可跑)。
3. **业务校验在确认时，不在草案阶段**：graph 的 `validate` 节点目前只做 Schema；完整规则在 `commit_draft` 内、文件锁下强制复检。因此 1000 金币会先看到预览，点确认才被 `E_REWARD_RANGE` 拒绝——**零写入**，但不要宣称“生成阶段就拒绝”。
4. **会话记忆未注入多轮**：`SessionStore` 记录消息与 trace，但 `/messages` 每次以单轮 state 启动；“奖励改成 150”这类追问不会自动带上上一轮草案。演示脚本因此要求重述完整需求。
5. **面板依赖未安装**：`scripts/dashboard.py`（Streamlit）已交付，但本机没有 streamlit，UI 启动未实测；无 streamlit 时轨迹用 `GET /api/v1/traces/{trace_id}` + `traces/traces.jsonl`，指标用 `gcc eval` 终端面板。
6. **fastmcp 需 Python 3.11**：本机 3.10，MCP 协议传输不可用（本地 Registry 仍可用）；Docker 镜像已用 3.11。
7. **文档勾选未全量同步**：`docs/03`、`docs/04` 的 S7/S8 复选框与 S8-05 简历数字仍是任务清单视图，未逐项回填。

---

## 6. 系统架构

```text
┌──────────────────────────────────────────────────────────────┐
│ 交互层  FastAPI + SSE（/api/v1/sessions、/messages、/confirm） │
│ 自然语言输入 → 流式事件（检索/草案/校验/待确认） → 确认或驳回   │
└───────────────────────────────┬──────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────┐
│ 编排层  自研显式状态机（LangGraph 风格 API，零 langgraph 依赖）│
│ route → QueryAgent（只读检索问答）                            │
│       → TaskAgent（取上下文 → 草案 → 校验 → 修复 → 预览 → 提交）│
└──────┬──────────────────┬──────────────────┬─────────────────┘
       │                  │                  │
┌──────▼───────┐  ┌───────▼────────┐  ┌──────▼──────────────┐
│ 检索层        │  │ 工具层 (MCP)    │  │ 记忆层               │
│ BM25 + RRF   │  │ 读/校验/预览/   │  │ 会话 / 偏好 /        │
│ dense=Noop   │  │ 提交/回滚       │  │ 任务上下文           │
└──────────────┘  └───────┬────────┘  └─────────────────────┘
                          │ 只读默认 · 白名单 · 两阶段提交 · 原子写
┌─────────────────────────▼───────────────────────────────────┐
│ 横切层  评测（检索指标；Ragas/Agent 指标待接）                │
│         可观测（JSONL span + GET /traces/{id}）               │
└─────────────────────────────────────────────────────────────┘
```

### 一次请求的状态流转

```text
drafting → validating ──(通过)──→ previewed → awaiting_confirmation → committed
                │
                └──(不通过，<2 轮)──→ repairing ──┐
                └──(不通过，≥2 轮)──→ rejected（向策划解释，不写文件）

确认阶段：confirmed → commit（指纹 + 版本 + 强制复检）→ committed | rejected
```

### 安全写入的五道关

1. **MCP 默认只读**：写模式是启动参数（`GCC_MCP_MODE`），不是运行时 API，Prompt 注入无法提升权限；
2. **路径白名单**：只允许 `data/tasks/**`，拒绝 `..`、非法 `task_id`、软链逃逸；
3. **两阶段提交**：预览返回一次性 `commit_token` + 内容指纹 + 基础版本；确认必须带同一个 token；
4. **强制复检**：`commit_draft` 在文件锁内重跑完整校验，不信任上游结论；
5. **原子写 + 备份 + 审计**：同目录临时文件 + `os.replace`；覆盖前备份；`traces/commits.jsonl` 记录 diff。

---

## 7. 核心能力

| 能力 | 实现要点 |
| --- | --- |
| **配置片段级 RAG** | JSON 感知切分（按任务/目标/奖励块），BM25 + RRF 融合，按 `source_type / 等级段 / 任务类型` 预过滤 |
| **双 Agent 路由** | Supervisor 分派；QueryAgent 只持有检索工具，TaskAgent 只持有生成/校验/写入工具 |
| **结构化生成** | Pydantic `TaskDraft` → JSON Schema；解析失败重试 1 次，字段/业务错误进入修复节点（≤2 轮） |
| **三级校验** | ① Schema ② YAML 业务规则 DSL ③ 冲突检测（ID 唯一/引用存在/循环依赖）；错误带 `constraint_ref` 与 `suggestion` |
| **安全写入** | 只读默认、路径白名单、指纹 + 版本乐观锁、文件锁、强制复检、原子替换、备份、审计 |
| **分层记忆** | 会话（草案版本）/ 偏好（JSON store，Redis 可插拔）/ 任务上下文（当前 task_id 与证据快照） |
| **评测** | 检索 Recall/MRR/nDCG；FPVR 91.30% / 修复轮次 0.13 / P95 4083ms（2026-11-18 真实 LLM）；Ragas 待接 |
| **可观测** | 每次调用落 JSONL span：检索了什么 → 调了哪个工具 → 校验报了什么 → 是否写入 |

---

## 8. 仓库结构

```text
gameconfig-copilot/
├── README.md                  # 本文档
├── Makefile                   # install / test / lint / eval / demo
├── .env.example               # GCC_* 环境变量模板（不含密钥）
├── Dockerfile                 # S8-01：python:3.11-slim + 非 root + healthcheck
├── docker-compose.yml         # api / mcp(可选) / qdrant(注释占位)
├── .dockerignore
├── docs/
│   ├── 01-product-outline.md
│   ├── 02-architecture.md
│   ├── 03-implementation-plan.md
│   ├── 04-task-checklist.md
│   ├── 05-interview-kit.md
│   ├── 06-multi-agent-collaboration.md
│   ├── demo-script.md         # S8-02/S8-03：5 分钟台词、分镜、录制清单
│   └── reports/               # S7-08 基线入库：baseline.md / baseline.json
├── src/gameconfig_copilot/
│   ├── api/                   # FastAPI 路由 + SSE
│   ├── agents/                # 状态机、Supervisor、双 Agent、Prompt 装配
│   ├── llm/                   # LLMClient Protocol + DeepSeek / OpenAI-compatible
│   ├── rag/                   # 语料、切分、索引、BM25/RRF、检索
│   ├── mcp_server/            # 工具、校验器、原子写入
│   ├── memory/                # 会话 / 偏好
│   ├── schemas/               # TaskDraft / ValidationReport / Constraint
│   ├── evaluation/            # 检索指标、Agent 指标（CLI 部分待接）
│   └── observability/         # JSONL tracer、事件模型
├── data/                      # 自建虚构 IP《星港纪元》语料（wiki/tasks/constraints/golden）
├── tests/                     # unit / integration / e2e + fakes
├── prompts/                   # query / supervisor / task 三个 Prompt
├── scripts/dashboard.py       # S7-06 Streamlit：轨迹列表 / 详情 / 指标
├── out/index/                 # 运行时索引（gitignored，由 gcc ingest 生成）
└── reports/                   # 评测报告（gitignored，本地生成）
```

---

## 9. 技术栈（以当前代码为准）

| 层 | 选型 | 说明 |
| --- | --- | --- |
| Web | FastAPI + 自研 SSE | `sse-starlette` 未使用；事件契约见 `api/sse.py` |
| 编排 | **自研显式状态机** | `agents/graph.py` 提供 LangGraph 风格的 `add_node/add_edge/stream`，零 `langgraph` 依赖；换取可单测、可控中断 |
| 检索 | `bm25s`（缺省回退纯 Python BM25）+ 自研 RRF | Dense = `NoopDenseStore`；Chroma/BGE-M3/Qdrant 未接 |
| 生成模型 | DeepSeek（默认）+ OpenAI-compatible | `LLMClient` Protocol 隔离；业务层不 import 厂商 SDK |
| 协议 | MCP 语义 + 本地 Registry；`fastmcp` 可选 | fastmcp 提供 stdio/streamable HTTP，需 Py3.11；缺失时明确退出码 3，不伪造协议 |
| 校验 | Pydantic v2 + 自研 YAML 规则引擎 | Schema 与业务规则分离，约束带 `rationale` |
| 记忆 | JSON 偏好存储 + 进程内会话 | Redis 预留未接；多轮注入待接 |
| 评测 | 自研检索/Agent 指标 | Ragas 未接 |
| 观测 | 自研 JSONL tracer + Streamlit 面板 | `traces/traces.jsonl`、`/api/v1/traces/{id}`、`scripts/dashboard.py`（streamlit 可选依赖） |
| 部署 | Docker Compose | 单机演示；不引入 K8s |

---

## 10. 路线图与当前状态

| 阶段 | 状态 | 证据 |
| --- | --- | --- |
| M0 立项 / 文档 / 骨架 | ✅ | `docs/01–06`、`gcc schema dump` |
| M1 知识库 + 混合检索 + 只读问答 | ✅ | 81 chunks、20 query 基线、QueryAgent + `/messages` |
| M2 校验 + MCP 工具 + 安全写入 | ✅ | 5/5 对抗拦截、8 条 confirm/reject 集成测试 |
| M3 分层记忆 + 评测 | ⚠️ 部分 | 偏好 CLI / 会话存储可用；FPVR 已跑 91.30%，Ragas 未跑 |
| M4 面板 + 反例 + 演示交付 | ⚠️ 部分 | Docker / 演示脚本 / README / Streamlit 面板已交付；5 分钟录屏未产出 |

**下一步（按优先级）**

1. 接 dense adapter（BGE-M3 / Qdrant），用同一份 `qrels.json` 重跑并对比 0.775；
2. ~~用真实 key 跑 30 条黄金需求~~（2026-11-18 已完成：FPVR 91.30%）→ 接 `gcc eval --suite agent` 与 Ragas；
3. 把会话历史注入 context（真正的“奖励改成 150”追问）；
4. 补齐 Ragas 报告，让 Streamlit 指标页从“检索 + Agent”变成三层齐全；
5. 完成 5 分钟录屏（分镜见 docs/demo-script.md §6），并回填 docs/03、04 的 S7/S8 勾选。

---

## 11. 文档导航

| 文档 | 内容 |
| --- | --- |
| [项目大纲](docs/01-product-outline.md) | 用户、场景、范围、功能清单、数据契约、风险 |
| [架构设计](docs/02-architecture.md) | 组件职责、请求生命周期、图、工具契约、ADR |
| [实施路线](docs/03-implementation-plan.md) | S0–S8 的目标、任务、验收标准、演示设计稿 |
| [任务清单](docs/04-task-checklist.md) | 可勾选任务表（进度视图，部分未回填） |
| [面试手册](docs/05-interview-kit.md) | 简历 bullet、叙述、高频追问、指标口径 |
| [多 Agent 协作计划](docs/06-multi-agent-collaboration.md) | 角色 / Wave / 契约所有权 / 验收门 |
| [**5 分钟演示脚本**](docs/demo-script.md) | 逐句台词、预期输出、两种模式、录屏分镜 |

---

## 12. 非目标（明确不做）

- 通用闲聊、百科问答；
- 模型训练 / 微调（只做 Prompt、检索、工具与评测工程）；
- 多游戏、多 Schema 的通用配置平台（MVP 只覆盖一类任务配置）；
- 自动发布到正式服 / 热更新链路（只写本地或测试分支目录）；
- 多人协作、权限系统、Web 编辑器（Streamlit 只做只读观测）。
