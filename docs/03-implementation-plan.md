# GameConfig Copilot · 编码实施思路

| 项目 | 内容 |
| --- | --- |
| 文档版本 | v0.3 |
| 状态 | 待执行（代码尚未开始） |
| 上游 | [项目大纲](01-product-outline.md) · [架构设计](02-architecture.md) |
| 配套 | [任务清单](04-task-checklist.md)（可勾选版本） |

---

## 1. 开发策略

### 1.1 五条原则

| 原则 | 说明 | 反例（要避免） |
| --- | --- | --- |
| **契约先行** | S0 先冻结 `TaskDraft` / `ValidationReport` / 工具签名，再写实现 | 边写边改字段，导致校验、Prompt、前端三处返工 |
| **只读优先** | 先跑通"查 + 生成 + 预览"，再开写权限 | 第一天就写文件，调试时反复污染 `data/tasks/` |
| **垂直切片** | 每个阶段结束都有一个能演示的端到端能力，而不是"底层写完了但跑不起来" | 先写完美抽象层，两周后还不能演示 |
| **评测前置** | 每个阶段结束记录基线，不等到最后才补评测 | 最后发现指标不可比，不知道改动是否有效 |
| **演示驱动** | 每个阶段定义"给面试官看什么"，倒推实现范围 | 做了很多内部优化，但演示时讲不出来 |

### 1.2 阶段依赖

```text
S0 地基 ──► S1 检索 ──► S2 只读闭环 ──► S3 草案 ──► S4 校验 ──► S5 写入闭环
                                                                    │
                                                    S6 记忆 ◄────────┘
                                                       │
                                                    S7 评测与观测 ──► S8 交付
```

S6 与 S5 可并行（记忆不依赖写入，但偏好抽取需要提交事件，故放在 S5 之后）。

### 1.3 里程碑总览

| 阶段 | 内容 | 预估 | 依赖 | 演示物 |
| --- | --- | --- | --- | --- |
| S0 | 工程地基 + 数据契约 | 0.5–1 天 | — | `schema dump` 输出 JSON Schema |
| S1 | 知识库 + 混合检索 | 3–5 天 | S0 | CLI 检索：query → 片段 + 来源 |
| S2 | QueryAgent + 只读闭环 | 2–3 天 | S1 | 一句话问答，带引用 |
| S3 | 草案生成 | 3–4 天 | S2 | 一句话 → 任务草案 JSON |
| S4 | 校验管线 + 自动修复 | 3–4 天 | S3 | 越界草案被拦截并解释 |
| S5 | MCP + 安全写入 | 3–5 天 | S4 | 确认后安全落盘 + 备份 |
| S6 | 分层记忆 | 2–3 天 | S5 | 第二次生成沿用偏好 |
| S7 | 评测 + 可观测面板 | 3–4 天 | S6 | 一键评测报告 + 轨迹面板 |
| S8 | 演示打磨与交付 | 2–3 天 | S7 | 5 分钟完整演示 |

合计约 **18–24 个工作日**（业余 15–20 h/周，约 4–5 周）。

---

## 2. 阶段详细设计

### S0 工程地基与数据契约（0.5–1 天）

**目标**：任何后续模块都能"import 得到、跑得起来、测得下去"。

**任务**

| # | 任务 | 产出文件 | 要点 |
| --- | --- | --- | --- |
| S0-01 | 项目元数据与依赖 | `pyproject.toml` | Python ≥3.10；依赖分组 `core/dev`；固定大版本 |
| S0-02 | 配置管理 | `config.py` | `pydantic-settings`；`GCC_` 前缀；`.env` 不入库 |
| S0-03 | 数据契约 | `schemas/task.py`、`report.py`、`constraint.py` | Pydantic v2；导出 JSON Schema 供 LLM 使用 |
| S0-04 | 轨迹记录器 | `observability/tracer.py`、`events.py` | contextvar 传递 trace_id；JSONL 追加写；脱敏 |
| S0-05 | CLI 骨架 | `cli.py` | `typer`；`schema dump` / `ingest` / `search` / `ask` 子命令占位 |
| S0-06 | 测试与规范 | `tests/`、`pyproject` 配置 | pytest + ruff；`make test` |
| S0-08 | LLMClient Protocol + DeepSeek adapter（可替换内部模型） | `llm/client.py`、`llm/base.py`、`llm/providers/deepseek.py`、`llm/providers/openai_compatible.py` | P0；`chat` / `structured` / `health` 三方法；重试/token 成本在 `llm/base.py`；厂商 SDK 只出现在 `llm/providers/` |

> 编号说明：本表**没有 S0-07**——03 把「测试与规范」并入 S0-06，而 [04 任务清单](04-task-checklist.md) 拆为 S0-06（CLI 骨架）+ S0-07（测试骨架）。v0.3 新增的 S0-08 编号沿用 04，此前各阶段编号一律未动。

**关键决策**：`TaskDraft` 用 Pydantic 模型定义，**同时**导出 JSON Schema 注入 Prompt。模型与 Schema 只维护一份，避免"文档写的和代码校验的不一致"。LLM 调用同样在 S0 就做薄抽象（[ADR-008](02-architecture.md)）：S3 生成只依赖 `LLMClient.structured`，不把任何厂商 SDK 写进 `agents/`。

**验收（DoD）**

- `python -m gameconfig_copilot.cli schema dump` 输出合法 JSON Schema；
- `pytest` 全绿（此时只有 schema 校验测试）；
- 任意函数调用能写入一条带 `trace_id` 的 JSONL 事件；
- 默认 provider 可 `health()` 调通；切 `GCC_LLM_PROVIDER=openai_compatible` + `GCC_LLM_BASE_URL` 指向 mock 服务时业务代码零改动；`llm/` 之外不出现厂商 SDK import。

**风险**：依赖版本冲突（LangGraph / Pydantic / Ragas 对 Python 版本要求不同）→ S0 先锁定可共存版本，再开工。

---

### S1 知识库与混合检索（3–5 天）

**目标**：给定 query，返回"可复用配置片段"，且每条可回溯来源。

**任务**

| # | 任务 | 产出文件 | 要点 |
| --- | --- | --- | --- |
| S1-01 | 演示语料 | `data/wiki/*.md`、`data/tasks/*.json`、`data/constraints/*.yaml` | 见 §3 数据准备 |
| S1-02 | 加载与切分 | `rag/corpus.py`、`rag/chunker.py` | JSON 感知切分；父块回链；元数据抽取 |
| S1-03 | 索引构建（VectorStore Protocol + Chroma adapter + BM25 持久化） | `rag/index.py`、`rag/store.py`、`rag/stores/chroma.py` | 索引构建只调 `VectorStore.upsert`；payload 映射在 adapter 内 |
| S1-04 | 混合检索（filters 中性化 + 自研 RRF） | `rag/retriever.py`、`rag/fusion.py` | 中性 `filters: dict`；RRF 纯函数 k=60；过滤先于向量检索 |
| S1-05 | 检索评测 | `evaluation/retrieval_eval.py`、`data/golden/qrels.json` | 20 条 query 标注；Recall@5 / MRR |
| S1-06 | CLI 检索 | `cli.py search` | 输出片段、分数、来源、父块 |

> 编号说明：本节 S1-03 / S1-04 对应 [04 任务清单](04-task-checklist.md) 的 S1-05（索引构建）/ S1-06（混合检索）——04 把「演示语料」拆成了 S1-01~03，故编号整体后移。v0.2 未改动任一文档的任务编号；v0.3 同样只新增 S0-08，未改动既有编号。

**实现要点**

- **JSON 切分**：整任务作为父块；`objectives` / `rewards` / `branches` 各成子块，子块 payload 带 `parent_id` 与 `task_id`。生成时取"子块定位 + 父块补全结构"。
- **薄抽象**：`rag/store.py` 定义 `VectorStore` Protocol 与中性 `Chunk`/`Hit`；`rag/stores/chroma.py` 只做 `filters` 翻译与 `upsert`/`search`，不把 Chroma/Qdrant 语法泄露给上层。
- **过滤优先**：已知等级段时先 `level_band` 过滤，再做向量召回；否则"30 级任务的奖励结构"会污染 6 级草案。
- **术语归一**：维护 10–20 条同义词（矿洞/矿车/采掘 → `MAP_MINE_03`），提升 BM25 命中。
- **RRF 自研**：`rag/fusion.py` 纯函数（k=60），输入 dense 与 BM25 两路排名，输出融合排序；不依赖向量库内置 hybrid API，可单测。

**验收**

- 20 条 query 的 Recall@5 记录进 `reports/retrieval-baseline.json`（初始低于 0.85 可接受，作为基线）；
- 每条命中能显示 `source_id + 位置`；
- 过滤条件生效：等级段外的片段不出现在结果里；
- `rag/fusion.py` 的 RRF 有单测（同分/空列表/乱序输入结果稳定，k 可配）；
- 切换存储实现只需在 `rag/stores/` 新增 adapter，`agents/` 与 `rag/retriever.py` 零改动。

**风险**：embedding 模型下载慢/无 GPU → 预计算缓存；或先接云端 embedding。

---

### S2 QueryAgent 与只读闭环（2–3 天）

**目标**：跑通"自然语言 → 路由 → 检索 → 带引用回答 → SSE 推送"。

**任务**

| # | 任务 | 产出文件 | 要点 |
| --- | --- | --- | --- |
| S2-01 | Supervisor 路由 | `agents/supervisor.py` | 规则 + 小模型；输出 `query/task/other` |
| S2-02 | QueryAgent | `agents/query_agent.py` | 工具：`search_wiki` / `find_similar_tasks` / `list_constraints` |
| S2-03 | 引用约束 | `prompts/query.md` | 回答必须带 `[source]`；无依据必须说"未检索到" |
| S2-04 | SSE API | `api/main.py`、`api/routes.py`、`api/sse.py` | 事件按架构文档 §10.2 |
| S2-05 | 冒烟用例 | `tests/e2e/test_query_flow.py` | 3 条问题，校验引用存在 |

**实现要点**

- 回答通过 `status(answered)` 的 `answer` 字段下发（不新增 `token` 事件，事件表以架构文档 §10.2 冻结版为准）；草案阶段必须等完整 JSON 再推 `event: draft`；
- QueryAgent **没有**写工具，从工具注册层面就杜绝越权；
- "未检索到"是显式分支，不允许模型自由发挥补全设定。

**验收**

- 3 个问答（NPC 立场 / 相似任务 / 约束查询）有引用且引用可点开；
- 问知识库外的问题，回答明确说"知识库未覆盖"；
- 轨迹里能看到 `route` → `retrieval` → `llm` 三个 span。

---

### S3 结构化草案生成（3–4 天）

**目标**：一句话需求 → 通过 Schema 校验的 `TaskDraft`。

**任务**

| # | 任务 | 产出文件 | 要点 |
| --- | --- | --- | --- |
| S3-01 | 上下文拼装 | `agents/context_builder.py` | token 预算 6000；按区块分配；记录截断 |
| S3-02 | 结构化生成 | `agents/task_agent.py` | 经 `LLMClient.structured` 生成 `TaskDraft`；解析失败（`LLMStructuredOutputError`）重试 1 次 |
| S3-03 | 草案节点 | `agents/graph.py` | `draft` 节点输出草案 + provenance |
| S3-04 | 黄金集 v1 | `data/golden/requests_v1.jsonl` | 10 条需求（正常 7 / 边界 3） |
| S3-05 | 首次通过率基线 | `evaluation/agent_eval.py` | 只统计 Schema 通过率（业务校验 S4 才接入） |

**实现要点**

- **不要把所有检索结果塞进 Prompt**：按"2 条最相似任务 + 1 条同 NPC 任务 + 2–3 条世界观 + 相关约束"组织，预算见架构文档 §5.3；
- 生成时要求模型**先输出 `reasoning` 字段（不落盘）再输出 draft**，可提升字段完整率（在 Prompt 中要求，解析后丢弃）；
- `task_id` 由规则生成（类型 + 最大序号 +1），**不由模型自由编造**，从源头消除 ID 冲突。

**验收**

- 10 条黄金需求中 ≥ 7 条生成合法 JSON（Schema 层）；
- 草案包含 `provenance.evidence`，能指回检索片段；
- 记录 S3 基线 FPVR（预计 40–60%）。

---

### S4 校验管线与自动修复（3–4 天）

**目标**：把"格式对"提升到"业务对"，并让错误可解释、可修复。

**任务**

| # | 任务 | 产出文件 | 要点 |
| --- | --- | --- | --- |
| S4-01 | 规则引擎 | `mcp_server/validator/engine.py` | `Rule` 协议 + 注册表；YAML 加载；消息模板 |
| S4-02 | Schema 规则 | `validator/schema_rules.py` | 必填、枚举、正则、序号连续 |
| S4-03 | 业务规则 | `validator/business_rules.py` | 奖励区间、等级区间、对白长度、区域合法性 |
| S4-04 | 冲突检测 | `validator/conflicts.py` | ID 唯一、引用存在、依赖成环 |
| S4-05 | 修复节点 | `agents/graph.py` `repair` | 把 `ValidationReport` 回灌；≤2 轮 |
| S4-06 | 规则单测 | `tests/unit/test_rules.py` | 每条规则至少 3 个用例（通过/越界/边界） |

**实现要点**

- 规则返回**结构化建议**（`field + value`），而不是"请修改奖励"；
- `severity` 区分 `error`（阻断）与 `warning`（提示），修复节点只处理 error；
- 规则引擎**纯函数化**：输入 `(draft, constraints, repo_snapshot)`，输出 `report`，不碰 IO，方便单测与回放；
- 修复 Prompt 必须包含"原始需求 + 当前草案 + 错误列表 + 相关约束原文"，禁止让模型猜。

**验收**

- 规则单测覆盖全部类型，边界值（等于上限）通过；
- 对抗草案（越界奖励、重复 ID、前置任务不存在、循环依赖）全部被拦截且错误码正确；
- 修复 2 轮仍失败时，最终状态为 `rejected`，且不产生任何写操作。

---

### S5 MCP 工具层与安全写入（3–5 天）

**目标**：让 Agent 的手"有闸门"——默认只读，写入可审计、可回滚、原子。

**任务**

| # | 任务 | 产出文件 | 要点 |
| --- | --- | --- | --- |
| S5-01 | MCP Server | `mcp_server/server.py` | stdio 启动；按 mode 注册工具 |
| S5-02 | 只读工具 | `mcp_server/tools.py` | 读取、列表、约束、校验 |
| S5-03 | 预览 | `tools.py` `preview_draft` | unified diff + fingerprint |
| S5-04 | 写入器 | `mcp_server/writer.py` | 路径守卫、文件锁、临时文件、`os.replace`、备份、审计 |
| S5-05 | 提交工具 | `tools.py` `commit_draft` | 指纹 + revision + 内部复检 |
| S5-06 | 人工确认流 | `api/routes.py` | `commit_token` 签发与校验 |
| S5-07 | 破坏性测试 | `tests/integration/test_writer.py` | 并发写、进程中断、越权路径、只读模式 |

**实现要点**

- 写模式开关在**启动参数**而非运行时 API，避免 Prompt 注入临时提权；
- 路径守卫用规范化后的绝对路径前缀比较，拒绝 `..`、符号链接、盘符切换；
- 指纹绑定"草案内容 + 目标路径 + 基础版本"，防止确认 A 写入 B；
- 提交前**强制**再跑一次完整校验（不信任上游），这是"最后一道闸"；
- 备份先于替换；审计记录 diff 摘要而非全文，控制日志体积。

**验收**

- 只读模式下列表里没有写工具；
- 越权路径（`../../etc/passwd`、绝对路径、软链）全部 `E_PATH_DENIED`；
- 模拟写入中断后目标文件仍完整，临时文件可被启动清理；
- 两进程同时提交同一任务，后者收到 `E_STALE_REVISION` 而非覆盖；
- 反例演示：1 级 1000 金币 → 零写入。

---

### S6 分层记忆（2–3 天）

**目标**：Agent 记得"这个策划的习惯"，但不越权覆盖显式指令。

**任务**

| # | 任务 | 产出文件 | 要点 |
| --- | --- | --- | --- |
| S6-01 | 会话记忆 | `memory/session.py` | LangGraph checkpointer（SQLite） |
| S6-02 | 偏好存储 | `memory/preference.py` | 接口 + 本地 JSON 实现；Redis 可插拔 |
| S6-03 | 偏好抽取 | `memory/preference.py` `extract()` | 从提交结果与人工修改中学习 |
| S6-04 | 上下文注入 | `agents/context_builder.py` | 偏好以结构化字段注入，≤300 token |
| S6-05 | 记忆测试 | `tests/unit/test_preference.py` | 滑动平均、异常样本不更新、可清除 |

**验收**

- 同一策划连续两次生成支线任务，第二次奖励落在偏好带内，且报告标注"来源：偏好"；
- 用户显式说"这次给 500 金币"，偏好不覆盖，走正常校验；
- `gcc memory show --designer u_1024` 能看到偏好与来源，可清除。

---

### S7 评测与可观测（3–4 天）

**目标**：能回答"这次改动让首次通过率提升了多少"。

**任务**

| # | 任务 | 产出文件 | 要点 |
| --- | --- | --- | --- |
| S7-01 | 黄金集扩充 | `data/golden/requests_v2.jsonl` | 30 条（正常 20 / 边界 5 / 对抗 5） |
| S7-02 | 检索评测 | `evaluation/retrieval_eval.py` | Recall@k、MRR、nDCG |
| S7-03 | Ragas 评测 | `evaluation/ragas_eval.py` | faithfulness、answer_relevancy 等 |
| S7-04 | Agent 评测 | `evaluation/agent_eval.py` | FPVR、修复轮次、拦截率、延迟、成本 |
| S7-05 | 轨迹埋点补全 | 全链路 | 每个 span 带 tokens/cost |
| S7-06 | 面板 | `scripts/dashboard.py`（Streamlit） | 轨迹列表 / 详情 / 指标 |
| S7-07 | 回归对比 | `evaluation/compare.py` | 与 `baseline.json` 对比，超阈值失败 |

**实现要点**

- Ragas 的 judge 模型固定版本，Prompt 与指标口径写进文档，保证可比；
- 黄金集用 JSONL，每条带 `expected`（期望通过/拦截 + 关键字段断言），便于自动判定；
- 评测用"录制回放"：LLM 响应缓存（fixture），CI 不依赖网络，指标稳定；
- 指标下降阈值：FPVR −3pt、Recall@5 −2pt、成本 +20% 触发告警。

**验收**

- `gcc eval --suite all` 一键输出 JSON + Markdown 报告；
- 面板能打开任意 trace，看到检索片段、工具调用、校验错误、是否写入；
- 基线报告入库（`docs/reports/baseline.md`），后续每次对比有据可查。

---

### S8 演示打磨与交付（2–3 天）

**任务**

| # | 任务 | 要点 |
| --- | --- | --- |
| S8-01 | Docker Compose | api + mcp + qdrant（可选）+ redis |
| S8-02 | 演示脚本 | 见 §7，逐句写台词与预期输出 |
| S8-03 | 录屏 | 5 分钟，含反例与轨迹面板 |
| S8-04 | README 更新 | 替换"规划中命令"，补真实指标、截图、架构图 |
| S8-05 | 简历与问答 | 用真实数字替换 XX% |
| S8-06 | 代码清理 | 删除 TODO 死代码，统一日志与错误码 |

**验收**：5 分钟演示全程无手工救场；断网环境下用缓存索引也能跑。

---

## 3. 数据准备方案

### 3.1 为什么自建语料

真实游戏 Wiki 与配置涉及版权与保密，且"能公开拿到的"往往结构不完整。方案：自建虚构 IP《星港纪元》，**结构对齐真实项目**（Wiki 条目、任务 JSON、约束表三件套），保证技术链路真实可迁移。

### 3.2 语料清单

| 类型 | 数量 | 内容 | 构造方式 |
| --- | --- | --- | --- |
| Wiki | 3 篇 | 世界观与势力、NPC 档案、任务设计规范 | 每篇 800–1500 字，含明确实体 ID |
| 任务配置 | 10 条 | 主线 3（Lv1–15）、支线 4（Lv5–20）、日常 3（Lv10–30） | 手写，覆盖不同结构（有无分支、多目标、多奖励） |
| 约束 | 17 条 | 奖励 6（含金币/阅历下限各 1）、依赖 3、唯一性 2、区域 2、对白 2、活动 2 | YAML DSL，含 `rationale` |
| 黄金需求 | 30 条 | 正常 20 / 边界 5 / 对抗 5（含 2 条提示注入） | 人工编写 + 交叉检查 |
| 检索标注 | 20 条 | query → 相关 chunk_id 列表 | 先检索后人工修正，避免"用模型标模型" |

### 3.3 设定骨架（避免写作时卡壳）

- **势力**：星港议会（秩序）、铁砧工会（工业/中立）、深井教团（神秘/对立）
- **地点**：星港城 `MAP_STARPORT_01`、废弃矿洞 `MAP_MINE_03`、荒原哨站 `MAP_WASTELAND_02`
- **NPC**：铁匠老陈 `NPC_013`（工会，直爽）、议长 `NPC_001`（议会，谨慎）、矿工阿岚 `NPC_021`（教团线关键）
- **术语**：矿车/矿洞/采掘统一映射到 `MAP_MINE_03`

> 写作要求：每个实体在 Wiki 中有唯一 ID 与一段可被引用的描述，保证"检索 → 引用 → 校验引用存在"闭环。

---

## 4. 测试策略

| 层 | 范围 | 工具 | 关键用例 |
| --- | --- | --- | --- |
| 单元 | 切分器、RRF、规则引擎、指纹、路径守卫 | pytest | 边界值、非法输入、幂等 |
| 契约 | MCP 工具输入输出 vs Pydantic 模型 | pytest + JSON Schema | 字段缺失、类型错误、多余字段 |
| 集成 | 校验 + 写入 + 备份 + 回滚 | pytest + tmp_path | 并发、中断、只读模式 |
| 端到端 | 黄金需求全链路 | pytest + LLM 录制回放 | 正常生成、修复、拦截 |
| 属性 | 规范化 JSON 与指纹 | hypothesis（可选） | 键序无关、Unicode 稳定 |
| 人工 | 演示清单 | checklist | 5 分钟演示全流程 |

**覆盖率目标**：规则引擎与写入器 ≥ 85%，整体 ≥ 70%。

**CI（本地 pre-push）**：`ruff check` → `pytest -q` → `gcc eval --suite retrieval`（快速集）。

---

## 5. 评测执行流程

```text
1. S4 结束：跑第一次全量 → reports/baseline-YYYYMMDD.json（含各指标）
2. 每次影响检索/生成/校验的改动：
   a. 跑快速集（检索 + 10 条黄金）→ 约 2 分钟
   b. 通过后跑全量（30 条 + Ragas）→ 约 15 分钟
   c. compare.py 与基线对比，输出 diff 表
3. 指标回退 → 查轨迹定位层（检索命中？生成字段？规则误判？）
4. 每次优化在 docs/reports/ 留一页：改了什么 → 指标变化 → 结论
```

**报告必须包含**：指标表（本次 vs 基线）、失败样本清单（按错误码分组）、耗时与成本、结论与下一步。

---

## 6. 工程规范

| 项 | 约定 |
| --- | --- |
| 提交粒度 | 一个任务一个提交；提交信息 `feat(s1): json-aware chunker` |
| 分支 | `main` 保护；每阶段一个 `feat/sN-*` 分支，自测后合并 |
| 依赖 | `pyproject.toml` 固定大版本；锁文件入库 |
| 配置 | 全部走 `GCC_*` 环境变量 + `config/*.yaml`；密钥只进 `.env` |
| 日志 | 结构化（logger + trace_id）；禁止 `print` 进主流程 |
| 错误 | 业务错误用 `AppError(code, message, details)`；禁止裸 `except` |
| 文档 | 每个模块 docstring 写明"输入/输出/失败语义" |

---

## 7. 演示脚本（5 分钟）

| 时间 | 动作 | 讲解要点 |
| --- | --- | --- |
| 0:00–0:30 | 一句话定位 + 架构图 | "不是 RAG 问答，是能安全动手的 Agent" |
| 0:30–2:00 | 输入正常需求："给铁匠老陈加一条 6 级支线，去矿洞捡 4 块矿石" | 展示检索证据 → 草案 → 校验通过 → diff → 确认 → 文件变化 + 备份 |
| 2:00–2:40 | 追问："奖励改成 150 金币" | 展示会话记忆与增量修改 |
| 2:40–3:40 | **反例**："1 级任务给 1000 金币" | Agent 检索到约束 → 拒绝 → 解释依据 → 给出合规方案；强调"零写入" |
| 3:40–4:30 | 打开轨迹面板 | 定位刚才三次请求：检索了什么、校验报了什么、是否写入 |
| 4:30–5:00 | 指标页 | 首次通过率、拦截率、延迟；下一步计划 |

**演示前检查清单**：索引已构建、MCP 为写模式、备份目录可写、轨迹目录为空、网络可用（或已缓存）。

---

## 8. 交付物清单

| 类别 | 交付物 | 用于 |
| --- | --- | --- |
| 代码 | 可运行仓库 + Docker Compose | 面试现场 / GitHub |
| 文档 | README、架构、评测报告 | 简历附件 |
| 数据 | 演示语料 + 黄金集 + 基线报告 | 复现实验 |
| 演示 | 5 分钟录屏 + 现场脚本 | 面试 |
| 叙事 | 简历 bullet + 高频问答 | 简历 / 面试 |

---

## 9. 时间与取舍

若时间不足，按以下顺序砍（保主链路）：

1. 砍 Streamlit 面板 → 用 CLI 输出轨迹摘要替代（省 1–2 天）；
2. 砍 Redis → 偏好用本地 JSON（省 0.5 天，演示无感）；
3. 砍 reranker → 仅用 RRF（省 0.5 天，先看召回是否够）；
4. 砍 rollback → 保留备份与手工恢复说明（省 0.5 天）；
5. **不能砍**：校验管线、两阶段提交、原子写、评测基线、反例演示。

> 判断标准：能不能在 5 分钟内讲清"安全写入闭环"和"评测如何证明有效"。能，则项目成立。

---

## 10. 变更记录

| 版本 | 日期 | 变更 | 原因 |
| --- | --- | --- | --- |
| v0.1 | 2026-11-12 | 初始实施路线（S0–S8） | 立项 |
| v0.2 | 2026-11-13 | 新增检索层 VectorStore 抽象 + 误记勘误：S1-03/S1-04 改为 Protocol + adapter + 自研 RRF，补验收口径 | 保证「先 Chroma、S7 迁 Qdrant 只换 adapter」；关闭待拍板项 |
| v0.3 | 2026-11-13 | S0 表新增 S0-08「LLMClient Protocol + DeepSeek adapter」并补编号脚注/S0 DoD；S3-02 改为经 `LLMClient.structured` 生成、解析失败重试 1 次 | 用户新决策：先用 DeepSeek + 保留可替换接口（ADR-008）；S3 生成不绑厂商 SDK |
