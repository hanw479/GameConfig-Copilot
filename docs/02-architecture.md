# GameConfig Copilot · 架构设计

| 项目 | 内容 |
| --- | --- |
| 文档版本 | v0.3 |
| 状态 | 设计冻结（编码前基线） |
| 上游文档 | [项目大纲](01-product-outline.md) |
| 下游文档 | [实施路线](03-implementation-plan.md) |

---

## 1. 架构原则

1. **LLM 不做最终裁决**：生成可以自由，校验必须确定。所有写入都要经过与模型无关的确定性校验管线。
2. **只读优先**：默认能力是"查 + 生成 + 预览"，写入是显式开启的特权路径。
3. **契约先行**：`TaskDraft`、`ValidationReport`、MCP 工具签名在写代码前冻结，模块之间只通过这些结构通信。
4. **每层可单独评测**：检索、生成、校验、写入分别有指标；失败能定位到层，而不是"Agent 又不行了"。
5. **最小工具集**：每个 Agent 只拿到完成职责所需的工具，减少误调用与越权。
6. **可回滚**：任何写入都有备份、有 commit_id、有审计记录。

---

## 2. 组件与职责

| 组件 | 职责 | 不负责 | 关键模块 |
| --- | --- | --- | --- |
| API 层 | 会话、SSE 推送、确认/驳回、查询轨迹 | 业务决策 | `api/routes.py`, `api/sse.py` |
| Supervisor | 意图判定与路由 | 检索、生成 | `agents/supervisor.py` |
| QueryAgent | 检索 + 带引用回答 | 写文件 | `agents/query_agent.py` |
| TaskAgent | 组装上下文、生成、修复、请求预览/提交 | 直接落盘 | `agents/task_agent.py` |
| LLM 调用层 | 统一生成 / 结构化输出接口与各厂商适配；超时、重试、token 与成本统计 | 提示词、业务决策（生成什么、用不用模型由上层决定） | `llm/client.py`（`LLMClient` Protocol + `LLMResponse`）、`llm/base.py`（重试/超时/token 成本）、`llm/providers/*`（DeepSeek、OpenAI 兼容） |
| RAG | 索引构建、混合检索、过滤、重排 | 生成 | `rag/store.py`（Protocol + Chunk/Hit）、`rag/fusion.py`（RRF）、`rag/stores/*`（adapter）、`rag/index.py`、`rag/retriever.py` |
| MCP Server | 工具实现、校验、原子写入、备份 | 路由、Prompt | `mcp_server/*` |
| Memory | 会话、偏好、任务上下文 | 检索 | `memory/*` |
| Evaluation | 三套指标 + 回归对比 | 线上逻辑 | `evaluation/*` |
| Observability | span 记录、脱敏、落盘 | 业务 | `observability/*` |

**依赖方向（禁止反向）**

```text
api → agents → { rag, memory, mcp_client }
agents → { schemas, llm }                         # llm 只暴露 LLMClient Protocol，厂商 SDK 不外泄
mcp_server → { schemas, validator, writer }      # 不得 import agents / api
evaluation → { rag, agents, mcp_server, llm }    # 只读调用（生成侧同样只依赖 LLMClient）
observability ← 所有层（通过 tracer 接口）
```

---

## 3. 运行时视图

### 3.1 请求生命周期

```text
Client                API              Supervisor        TaskAgent            MCP Server
  │  POST /messages     │                   │                 │                     │
  ├────────────────────►│                   │                 │                     │
  │                     ├─ create trace ───►│                 │                     │
  │◄── event: meta ─────┤                   │                 │                     │
  │                     ├── route ─────────►│                 │                     │
  │◄── event: status ───┤                   │                 │                     │
  │                     │                   ├── intent=task ─►│                     │
  │                     │                   │                 ├── search (RAG) ─────┤
  │◄── event: evidence ─┤                   │                 │◄── 片段 + 分数 ──────┤
  │                     │                   │                 ├── LLM draft ────────┤
  │◄── event: draft ────┤                   │                 ├── validate_draft ──►│
  │◄── event: validation┤                   │                 │◄── report ──────────┤
  │                     │                   │                 ├── preview_draft ───►│
  │◄── awaiting ────────┤                   │                 │◄── diff+fingerprint ┤
  │  POST /confirm      │                   │                 │                     │
  ├────────────────────►├─────────────────────────────────────── commit(draft, fp) ─►│
  │                     │                   │                 │◄── commit result ───┤
  │◄── event: committed ┤                   │                 │                     │
```

### 3.2 延迟预算（P95）

| 阶段 | 预算 | 说明 |
| --- | --- | --- |
| 路由 | 0.3s | 小模型或规则 + 小模型 |
| 检索（含过滤 + 融合） | 0.8s | 本地向量库；rerank 另计 0.3s |
| 草案生成 | 8s | 流式返回首 token；总时长取决于输出长度 |
| 校验 | 0.05s | 纯本地规则 |
| 预览 | 0.02s | 纯本地 |
| 合计（不含人工确认） | ≤ 15s | 超标先查检索与 Prompt 长度 |

---

## 4. LangGraph 编排

### 4.1 状态定义

```python
class AgentState(TypedDict, total=False):
    trace_id: str
    session_id: str
    designer_id: str
    messages: Annotated[list[AnyMessage], add_messages]
    intent: Literal["query", "task", "other"]
    request: str
    evidence: list[Evidence]          # 检索片段 + 来源
    constraints: list[Constraint]     # 本次相关约束快照
    preference: Preference            # 偏好记忆注入
    draft: dict | None                # 当前草案（JSON）
    draft_version: int
    report: dict | None               # ValidationReport
    repair_round: int
    preview: dict | None              # diff + fingerprint
    stage: str
    error: dict | None
```

### 4.2 节点契约

| 节点 | 输入 | 输出 | 失败行为 |
| --- | --- | --- | --- |
| `route` | `request` | `intent` | 判定失败 → `clarify` |
| `research` | `request` + 过滤条件 | `evidence` | 空召回 → 回答"未检索到依据"，不编造 |
| `compose` | `evidence` | 最终回答 + 引用 | LLM 超时 → 返回片段列表 + 提示 |
| `gather_context` | `request` | `evidence` + `constraints` + `preference` | 约束加载失败 → 中止生成（安全默认） |
| `draft` | 上下文 | `draft`（Schema 校验通过） | 解析失败 → 重试 1 次 → 失败则 `rejected` |
| `validate` | `draft` | `report` | 规则引擎异常 → 视为不通过（fail-closed） |
| `repair` | `draft` + `report` | 新 `draft` | 轮次 ≥ 2 → `rejected` |
| `preview` | `draft` | `diff` + `fingerprint` | 目标路径非法 → `rejected` |
| `human_gate` | `preview` | 确认/驳回 | 超时（10 分钟）→ `cancelled` |
| `commit` | `draft` + `fingerprint` | `commit_id` + 备份 | 冲突 → 提示重新预览，不覆盖 |

### 4.3 关键约束

- `validate` 的判定**不依赖 LLM 输出**：即使模型声称"已校验"，也必须实际执行；
- `repair` 每轮必须携带上一轮 `ValidationReport`，禁止无信息重试；
- `human_gate` 前不得预写任何文件（包括临时文件）；
- 图上有 `recursion_limit`（默认 12），防止修复死循环。

---

## 5. 检索管线

### 5.1 离线索引

```text
data/wiki/*.md ──┐
data/tasks/*.json ┼─► loader ─► chunker ─┬─► VectorStore.upsert(chunks)
data/constraints/*.yaml ┘                ├─► BM25 倒排（bm25s 持久化）
                                         └─► chunk 元数据表（SQLite）

VectorStore.upsert(chunks) 由 adapter 实现：
  ├─ ChromaAdapter（S1 默认；内含 BGE-M3 embedding + payload 映射）
  └─ QdrantAdapter（S7 可选）
```

- 索引构建只依赖 `VectorStore.upsert(chunks: list[Chunk])`，不直接调用任何向量库客户端；
- `Chunk` 是中性结构：`{chunk_id, text, metadata}`，`metadata` 即 payload；embedding 由 adapter 内部完成（`search` 接收原始 query 字符串），上层不感知；
- payload 映射（字段名、类型转换、库内索引创建）全部在 `rag/stores/<backend>.py` 内完成，上层不感知。

Chunk / payload 契约（中性，adapter 负责翻译）：

```text
向量（adapter 内）: dense(1024, cosine, BGE-M3)
稀疏（独立于向量库）: BM25 倒排（bm25s 持久化）
payload: {
  chunk_id, source_type, source_id, parent_id, text,
  task_type?, level_min?, level_max?, npc_id?, topic?, scope?, severity?
}
过滤字段: source_type / task_type / level_band / npc_id
```

> Chroma 与 Qdrant 的 collection/索引配置写在各自 adapter 的构造参数里；换库不改变 payload 语义，只新增 adapter。

### 5.2 在线查询

```text
raw_query
  → 术语归一（同义词表：矿洞/矿车/采矿 → MAP_MINE_03）
  → 业务层构造中性 filters: dict（如 {"source_type": ["task"], "level_band": [6, 9]}）
  → dense  = VectorStore.search(query, filters, top_k=30)   # adapter 把 filters 翻译成库语法
  → sparse = bm25.search(query, filters, top_k=30)          # 本地 bm25s，不经向量库
  → rag/fusion.py：RRF: score(d) = Σ 1/(k + rank_i(d)), k=60
  → top-15 → （可选）cross-encoder rerank → top-5
  → list[Hit] → Evidence[{source_id, chunk_id, text, score, meta}]
```

**检索层契约**

```python
class VectorStore(Protocol):
    def upsert(self, chunks: list[Chunk]) -> None: ...
    def search(self, query: str, filters: dict, top_k: int) -> list[Hit]: ...
    def health(self) -> bool: ...
```

- 业务层（`agents/`、`rag/retriever.py`）只依赖 `VectorStore` Protocol 与 `Hit`/`Evidence`，**禁止**出现 `collection.get(ids=...)`、`where={"$and": ...}` 等库特有语法；
- `filters` 是中性字典，由 adapter 翻译为 Chroma `where` / Qdrant `Filter`；
- RRF 是 `rag/fusion.py` 中的纯函数（约十几行），不依赖任何库的内置 hybrid API，可独立单测；
- 换向量库 = 在 `rag/stores/` 新增一个 adapter，`agents/`、`rag/retriever.py` 零改动。

### 5.3 上下文拼装（token 预算 6000）

| 区块 | 预算 | 说明 |
| --- | --- | --- |
| 系统提示 + 输出 Schema | 1000 | 固定开销 |
| 世界观片段 | 1400 | 2–3 条，按实体去重 |
| 相似任务模板 | 2300 | 2 条最相似 + 1 条"同 NPC 不同任务" |
| 约束 | 1000 | 只放与本次任务类型/等级相关的 |
| 偏好 | 300 | 结构化，不放自然语言长文 |

上表五块合计 6000，与标题的总预算一致。总预算 6000，超出按优先级截断（系统提示 + Schema > 约束 > 世界观 > 相似任务 > 偏好，与 `agents/context_builder.py` 的 `BLOCK_PRIORITY` 同序）：单块超出按"相似度 × 信息密度"截断，全局仍超出时按上述优先级从低到高裁剪，并记录被截断项（用于归因"为什么生成漏了某条设定"）。

---

## 6. MCP 工具契约

### 6.1 传输与生命周期

- 开发期：`stdio` 子进程，由 Agent 进程拉起；
- 演示期：`streamable-http`，独立服务，便于轨迹面板同时观察；
- 服务启动参数：`--config config/mcp.yaml --mode readonly|write`；
- 未开启写模式时，`commit_draft` / `rollback_commit` 不注册（工具列表里就不存在，而非调用后报错）。

### 6.2 工具签名

```python
def get_task_config(task_id: str) -> TaskConfigView
def list_recent_tasks(filters: TaskFilters, limit: int = 10) -> list[TaskSummary]
def list_constraints(scope: str | None = None) -> list[Constraint]

def validate_draft(draft: TaskDraft) -> ValidationReport
def preview_draft(draft: TaskDraft) -> PreviewResult
def commit_draft(draft: TaskDraft, fingerprint: str,
                 expected_revision: str, author: str,
                 note: str = "") -> CommitResult
def rollback_commit(commit_id: str) -> CommitResult
```

### 6.3 返回结构

| 工具 | 返回 |
| --- | --- |
| `TaskConfigView` | `{task_id, path, revision, content, updated_at}` |
| `PreviewResult` | `{target_path, revision, unified_diff, stats:{added,removed}, fingerprint}` |
| `CommitResult` | `{status: "committed"\|"conflict"\|"rejected", commit_id?, backup_path?, new_revision?, report}` |

**错误约定**：工具内部异常不抛栈，统一返回结构化错误，便于 Agent 解释：

```json
{"ok": false, "error": {"code": "E_STALE_REVISION", "message": "文件已被修改", "details": {"expected": "a1b2", "actual": "c3d4"}}}
```

### 6.4 指纹与版本

```python
revision = sha256(file_bytes).hexdigest()[:12]          # 文件内容版本，无需 git
fingerprint = sha256(canonical_json({
    "draft": draft, "target_path": path, "base_revision": revision
})).hexdigest()[:16]
```

`commit_draft` 必须同时满足：`fingerprint` 匹配当前草案、`expected_revision` 等于磁盘当前版本。两者任一不满足即拒绝，避免"确认的是 A，写入的是 B"。

---

## 7. 写入事务模型

```text
commit(draft, fingerprint, expected_revision)
  │
  ├─ 1. 参数与路径校验（白名单 data/tasks/**，拒绝 .. / 绝对路径 / 符号链接）
  ├─ 2. 获取文件锁 data/.locks/{task_id}.lock（超时 5s）
  ├─ 3. 读取当前文件，计算 revision
  ├─ 4. 校验 expected_revision 与 fingerprint
  ├─ 5. 完整校验管线复检（schema → business → conflict）
  ├─ 6. 备份当前文件 → data/backups/{task_id}/{utc}-{commit_id}.json
  ├─ 7. 写入同目录临时文件 .{task_id}.tmp-{uuid} → flush → fsync
  ├─ 8. os.replace(tmp, target)   # 同分区原子替换
  ├─ 9. 追加审计 traces/commits.jsonl
  └─ 10. 释放锁，返回 commit_id + backup_path + new_revision
```

**崩溃安全**

| 崩溃点 | 结果 | 恢复 |
| --- | --- | --- |
| 步骤 5–7 之间 | 目标文件未变，残留临时文件 | 启动时清理 `*.tmp-*` |
| 步骤 8 之前 | 同上 | 同上 |
| 步骤 8 之后、审计前 | 文件已更新，审计缺失 | 启动时对比 revision 与审计尾部，补记录 |
| 断电（极端） | `os.replace` 保证要么旧要么新 | 备份可回滚 |

**并发**：单文件串行写（锁），跨文件并行；`revision` 乐观校验兜底多进程场景。

**回滚**：`rollback_commit(commit_id)` 读取备份，走同样的原子替换流程，生成新的 commit 记录（不删除历史）。

---

## 8. 记忆模型

| 键 | 类型 | 示例 | TTL |
| --- | --- | --- | --- |
| `sess:{session_id}:state` | LangGraph checkpoint | 消息、草案版本、当前阶段 | 7 天 |
| `pref:{designer_id}` | Hash | `reward_band_side=120-200`、`naming=snake_case`、`strictness=high` | 90 天 |
| `ctx:{session_id}` | Hash | `task_id`、`evidence_hash`、`constraint_snapshot` | 会话结束 |

偏好更新规则：

```text
观察值 = 最终提交草案的字段（而非模型初稿）
若 |观察值 - 偏好均值| < 2σ  → 滑动平均更新（α=0.3）
否则                          → 记为异常样本，不更新，进入待确认队列
来源记录：{field, old, new, source_commit, updated_at}
```

设计意图：偏好用于**默认值**，不用于**覆盖用户显式指令**；用户说"这次给 500"，就按 500 走校验，通过即写。

---

## 9. 可观测事件模型

### 9.1 事件结构（JSONL，一行一个 span）

```json
{
  "ts": "2026-11-12T10:00:01.123+08:00",
  "trace_id": "tr_01J...",
  "span_id": "sp_01J...",
  "parent_id": "sp_root",
  "name": "retrieval.hybrid",
  "kind": "span",
  "duration_ms": 42,
  "status": "ok",
  "attrs": {
    "query": "铁匠老陈 6 级 支线",
    "filters": {"level_band": [6, 9], "task_type": "side"},
    "top_k": 5,
    "hits": [{"chunk_id": "c_102", "score": 0.87, "source": "task:T_MAIN_0102"}]
  }
}
```

### 9.2 关键 span

| span | 关键属性 |
| --- | --- |
| `request` | `session_id`, `intent`, `status` |
| `retrieval.*` | `query`, `filters`, `hits[]`, `rrf_k` |
| `llm.*` | `provider`, `model`, `prompt_tokens`, `completion_tokens`, `cost_cny`, `finish_reason`（latency 取 span 的 `duration_ms`） |
| `tool.*` | `tool`, `args_digest`, `result_digest`, `ok`, `error_code` |
| `validation` | `checked`, `error_codes[]`, `duration_ms` |
| `commit` | `task_id`, `commit_id`, `backup_path`, `revision_before/after` |

### 9.3 脱敏

- 不入库：API Key、`.env` 内容、完整系统提示（只存 hash 与版本）；
- 入库前过滤：`sk-[A-Za-z0-9]{16,}`、邮箱、手机号；
- 轨迹文件默认本地保存，不随仓库提交（`.gitignore` 已排除 `traces/`）。

---

## 10. API 契约

### 10.1 REST

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/sessions` | 创建会话，返回 `session_id` |
| POST | `/api/v1/messages` | 发送消息，SSE 流式返回 |
| POST | `/api/v1/drafts/{draft_id}/confirm` | 携带 `commit_token` 确认写入 |
| POST | `/api/v1/drafts/{draft_id}/reject` | 驳回并记录理由 |
| GET | `/api/v1/traces/{trace_id}` | 读取单次轨迹（JSON） |
| GET | `/healthz` | 健康检查（含 MCP 连接状态） |

### 10.2 SSE 事件

| event | data 关键字段 | 触发点 |
| --- | --- | --- |
| `meta` | `trace_id`, `session_id` | 请求开始 |
| `status` | `stage`, `label` | 每次阶段切换 |
| `evidence` | `items[]`（source/score/snippet） | 检索完成 |
| `draft` | `draft`, `version` | 生成/修复完成 |
| `validation` | `ok`, `errors[]`, `warnings[]` | 校验完成 |
| `awaiting_confirmation` | `commit_token`, `diff`, `target_path`, `expires_in` | 等待人工 |
| `committed` | `commit_id`, `backup`, `revision` | 写入成功 |
| `warning` | `code`, `message` | 非致命问题（如上下文截断） |
| `error` | `code`, `message`, `stage` | 失败终止 |
| `done` | `status`, `elapsed_ms`, `tokens`, `cost_cny` | 流结束 |

> `commit_token` 由 API 层签发，绑定 `draft_id + fingerprint + 过期时间`，服务端二次校验，防止越权提交。

---

## 11. 架构决策记录（ADR）

### ADR-001：双 Agent + Supervisor，而不是一个万能 Agent

- **背景**：单 Agent 同时持有检索、生成、写入工具，Prompt 长、工具多，误调用与越权风险高。
- **决策**：Supervisor 只路由；QueryAgent 只读；TaskAgent 生成 + 校验 + 提交。
- **代价**：多一次路由调用，链路变长。
- **收益**：工具集最小化、可分别评测、可分别限权（QueryAgent 无法触达写工具）。

### ADR-002：检索单元下沉到"配置片段"

- **背景**：整篇 Wiki 或整个任务 JSON 作为 chunk，会让上下文被无关字段占满，生成时"抄"到错误结构。
- **决策**：Wiki 按标题 + 窗口切；任务 JSON 按语义块（objectives/rewards/branches）切，保留父块回链。
- **收益**：召回更精准；引用可定位到字段级。

### ADR-003：MCP 独立进程，而不是进程内函数调用

- **背景**：函数调用最简单，但工具与 Agent 强耦合，无法被其他客户端复用，也难以独立限权与测试。
- **决策**：MCP Server 独立进程，stdio / streamable-http 两种传输。
- **代价**：多一层序列化与进程管理。
- **收益**：工具契约稳定、可单独测试、可只读部署、可被 IDE 复用。

### ADR-004：两阶段提交 + 原子替换

- **背景**：直接 `open(path,'w')` 写 JSON，进程中断会产生半截文件。
- **决策**：预览出指纹 → 确认 → 复检 → 临时文件 → `os.replace` → 备份。
- **收益**：不会出现半写文件；确认与写入内容一致；可回滚。

### ADR-005：校验独立于 LLM

- **背景**：让模型"自己检查"不可靠，且无法解释规则来源。
- **决策**：确定性规则引擎，规则来自 YAML DSL，与模型输出解耦；模型只负责修复建议的自然语言表达。
- **收益**：可单测、可审计、可向策划解释"依据哪条"。

### ADR-006：自研 JSONL tracer，而不是一开始接 OpenTelemetry

- **背景**：OTel 生态完善但接入成本高，MVP 阶段需要的是"能看懂、能演示"。
- **决策**：先自研轻量 tracer（contextvar + span 上下文），字段对齐 OTel 语义；后续可替换 exporter。
- **收益**：一天内可完成，Streamlit 直接读 JSONL。

### ADR-007：检索层薄抽象 VectorStore Protocol + 自研 RRF

- **背景**：Chroma 起步、S7 可能迁 Qdrant。若业务代码直接调用 `collection.get(ids=...)`、`where={"$and": ...}` 或库内置 hybrid API，换库就会变成跨 `agents/`、`rag/` 的重构，且融合逻辑无法单测。
- **决策**：检索层只暴露极薄 `VectorStore` Protocol（`upsert` / `search` / `health`）；过滤统一用中性 `filters: dict`，由 `rag/stores/<backend>.py` 翻译成各库语法；RRF 融合放 `rag/fusion.py` 自研纯函数（k=60），不使用库内置 hybrid API；换库只新增 adapter。
- **代价**：放弃部分库原生高级过滤/混合检索能力，需自行维护 `filters` 映射与各库集成测试；adapter 多一层轻量转发。
- **收益**：业务代码与向量库解耦，S7 迁移只换 adapter；RRF 可单测、可复现；评测口径不随库变化；避免业务层被某一家库的语法锁定。

### ADR-008：生成模型薄抽象 `LLMClient` Protocol（DeepSeek 默认、可换内部模型）

- **背景**：生成模型先用 DeepSeek（中文配置生成效果好、成本低），但公司后续大概率换内部模型或内网网关。若 `agents/`、`evaluation/` 直接 `import openai`、或用框架的 `with_structured_output` 绑死某家 SDK，"换模型"就会变成跨业务层的重构；结构化输出的解析/重试、超时与 token/成本统计也会在每个调用点重复。
- **决策**：业务层（`agents/`、`evaluation/` 生成侧等）只依赖 `llm/client.py` 的 `LLMClient` Protocol，不直接 import 任何厂商 SDK；openai/deepseek 等 SDK 只出现在 `llm/providers/`。接口命名沿用 [01 §9](01-product-outline.md) 的 `LLMClient`：

```python
class LLMClient(Protocol):
    def chat(self, messages: list[Message], *, temperature: float = 0.2,
             max_tokens: int | None = None) -> LLMResponse: ...
    def structured(self, messages: list[Message], schema: type[BaseModel] | dict,
                   *, temperature: float = 0.0) -> dict: ...
    def health(self) -> bool: ...
```

- **适配器与配置**：默认 `llm/providers/deepseek.py`（OpenAI 兼容）；内网 vLLM/网关走 `llm/providers/openai_compatible.py`（`base_url` 可配）。超时、重试、token 与成本统计集中放 `llm/base.py`（或 client 包装层），不散落到业务代码。配置走 `GCC_LLM_PROVIDER`（默认 `deepseek`）/ `GCC_LLM_MODEL` / `GCC_LLM_BASE_URL` / `GCC_LLM_API_KEY` / `GCC_LLM_TIMEOUT_S`；密钥只进 `.env`，不入库、不落轨迹。
- **结构化输出与观测**：`structured` 优先厂商 JSON mode / function calling，不支持时回退「Schema 注入 Prompt + 解析」，两条路径都返回结构化结果、业务层无感知；解析失败抛 `LLMStructuredOutputError`，由 TaskAgent 重试 1 次（保持 [03 S3-02](03-implementation-plan.md) 既有行为）。`LLMResponse` 携带 `content` / `model` / `usage`（`prompt_tokens`、`completion_tokens`）/ `latency_ms` / `finish_reason`；`llm.*` span 记 provider / model / tokens / cost / latency（§9.2）。
- **边界**：embedding **不进** `LLMClient`——检索侧 embedding 已由 `VectorStore` adapter 持有（ADR-007）；换 embedding 另开 `EmbeddingProvider` 抽象，标注为后续可选。
- **代价**：多一层 Protocol + adapter（约 150–200 行）；无法直接使用各厂商 SDK 的高级特性（如各家专属 tool schema 扩展）；流式与断线重连需在 adapter 内自行归一；退化为「Schema 注入 Prompt」路径时，结构化稳定性略低于原生 JSON mode。
- **收益**：换模型/换网关只改 `llm/providers/` + 环境变量，`agents/`、`evaluation/` 零改动；厂商 SDK 不外泄，便于内网/离线部署与合规审计；结构化输出与重试策略单点收敛，评测口径不随模型变化；token 与成本统计集中，成本回归可跨模型对比。

---

## 12. 失败模式与降级

| 故障 | 检测 | 行为 | 用户可见 |
| --- | --- | --- | --- |
| LLM 超时/限流 | 客户端超时（`GCC_LLM_TIMEOUT_S`，默认 30s） | 重试 1 次（指数退避）→ 失败则返回已检索证据 | `error` 事件 + 建议 |
| 结构化输出解析失败 | Pydantic 校验 | 重试 1 次（附解析错误）→ 仍失败则 `rejected` | 说明"模型输出不符合 Schema" |
| 检索空结果 | `len(hits)==0` | QueryAgent 明确说"未检索到"；TaskAgent 用默认模板但标记 `low_confidence` | `warning` 事件 |
| 约束加载失败 | 文件缺失/解析错误 | **中止生成**（fail-closed），不允许"无约束"写入 | `error` 事件 |
| 校验器异常 | 捕获异常 | 视为校验不通过 | `E_VALIDATOR_CRASH` |
| MCP 不可用 | 连接探测 | 降级为"仅生成 + 预览到内存"，禁止写入 | `error` + 提示 |
| 并发修改 | revision 不一致 | 拒绝写入，返回冲突提示 | 建议重新预览 |
| 磁盘满 | 写临时文件失败 | 不替换目标文件，清理临时文件 | `E_IO` |

---

## 13. 非功能需求

| 维度 | 要求 |
| --- | --- |
| 可测试性 | 规则引擎、切分器、RRF、指纹计算纯函数化，单测覆盖率 ≥ 80% |
| 可配置 | 模型、向量库、路径、写模式全部走配置（`pydantic-settings` + `.env`） |
| 可移植 | Linux/macOS/Windows 均可跑；Docker Compose 提供兜底环境 |
| 安全 | 写路径白名单、密钥不入库、日志脱敏、MCP 默认只读 |
| 性能 | 单请求 ≤ 15s（P95）；索引构建 ≤ 5 分钟（小语料） |
| 可维护 | 模块依赖单向；新增规则类型只需实现一个 `Rule` 子类 + 注册 |

---

## 14. 变更记录

| 版本 | 日期 | 变更 | 原因 |
| --- | --- | --- | --- |
| v0.1 | 2026-11-12 | 初始架构基线 | 立项 |
| v0.2 | 2026-11-13 | 新增检索层 VectorStore 抽象 + 误记勘误：§2 RAG 关键模块、§5.1/§5.2 检索契约、§11 ADR-007 | 锁定「先 Chroma、S7 迁 Qdrant 只换 adapter」；关闭多 Agent 协作待拍板项 |
| v0.3 | 2026-11-13 | §2 新增「LLM 调用层」组件行并补依赖方向；§9.2 `llm.*` span 补 `provider`；§11 追加 ADR-008（`LLMClient` 薄抽象、DeepSeek 默认、`openai_compatible` 可替换）；§12 超时改由 `GCC_LLM_TIMEOUT_S` 配置 | 用户新决策：先用 DeepSeek，但保留可替换接口，日后换公司内部模型业务层零改动 |
