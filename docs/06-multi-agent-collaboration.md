# GameConfig Copilot · 多 Agent 协作开发计划

| 项目 | 内容 |
| --- | --- |
| 文档版本 | v0.3 |
| 日期 | 2026-11-13 |
| 状态 | 可直接下发执行（代码尚未开始） |
| 上游文档 | [项目大纲](01-product-outline.md) · [架构设计](02-architecture.md) · [实施路线](03-implementation-plan.md) · [任务清单](04-task-checklist.md) |
| 定位 | 把 S0–S8 从「单人顺序推进」改为「多 Worker 并行推进」的调度依据 |

> **对齐约定（先读）**
> - 任务编号、交付物、验收口径一律以 [04-task-checklist.md](04-task-checklist.md) 为准（它是进度唯一事实源）。
> - 阶段 DoD 与演示物以 [03-implementation-plan.md](03-implementation-plan.md) 为准；数据契约以 [02-architecture.md](02-architecture.md) 为准。
> - 基线计数（v0.3 已收敛）：04 清单为 **62 项**（S0:8 / S1:8 / S2:6 / S3:5 / S4:7 / S5:9 / S6:5 / S7:8 / S8:6），04 变更记录已同步勘误；03 各阶段任务粒度与 04 不完全一致（详见 §11 附注）。**本文档以 04 的 62 项为唯一事实源**。
> - 本文档是调度计划，不是业务代码；v0.3 与 01–04 同步了生成模型 `LLMClient` 契约（[ADR-008](02-architecture.md)，`llm/` 归 F），v0.2 的检索层 `VectorStore` 契约与 4 项勘误继续有效；其余章节仍只描述分工与验收。

---

## 1. 目标与协作原则

五条原则沿用 [03 §1.1](03-implementation-plan.md) 的既定表述，本节只说明**多 Agent 并行下每条如何落地**。

| 原则 | 单人开发时的含义 | 多 Agent 并行下的落地规则 |
| --- | --- | --- |
| **契约先行** | S0 先冻结数据契约再写实现 | 契约在 **G0 门**一次性冻结，由**地基 Worker 单一 Owner**产出；其余 Worker 只 `import` 不复制、不私改；任何跨 Worker 改动走「协调者批准 + ADR」流程（§5）。Worker 开工前必须引用契约版本号，防止各自基于旧契约返工。 |
| **只读优先** | 先跑通「查 + 生成 + 预览」再开写权限 | 提升为**文件所有权规则**：每个 Worker 默认只读他人路径，只能在自有 Owner 路径写（§5 所有权矩阵）；写入/MCP Worker 的写模式仍是启动参数而非运行时开关，且多 Agent 越权面更大，故只读默认要执行得更严格。 |
| **垂直切片** | 每阶段结束有可演示的端到端能力 | 每个 **Wave 结束时**必须产出可演示切片：Wave 1 = CLI 检索带来源；Wave 2 = 只读问答 + 草案 + 校验拦截；Wave 3 = 安全写入 + 记忆；Wave 4 = 评测 + 5 分钟演示。并行不能牺牲切片——**每个 Wave 都有一句「给面试官看什么」**，否则并行只是「各自写底层，拼不起来」。 |
| **评测前置** | 每阶段结束记基线 | **评测 Worker 从 Wave 1 就参与**（检索标注与基线），不等最后补；每个 Owner Worker 过 Gate 时必须附评测证据（指标报告或基线 diff），没有证据视为未完成（对齐 04 §0 全局 DoD 第 5 条）。 |
| **演示驱动** | 每阶段倒推演示物 | **演示交付 Worker 在 Wave 0 就反写「演示验收规格」**（5 分钟演示需要哪些中间产物、断网兜底条件），作为各 Wave 的出口约束，避免并行时各做各的、最后演示拼不出来。 |

**协作总则（贯穿全文）**

1. **协调者/Router 是唯一仲裁人**，不写业务代码，只做：任务下发、契约冻结审批、ADR 记录、验收门把关、steer/close/重派。
2. **单一 Owner + 只读他人**：每个文件/目录有且仅有一个 Owner Worker，其他人只读（§5）。
3. **契约即真相源**：Worker 之间不口头约定，一切接口以 02/03/04 的冻结文本 + G0 产出的 Schema 为准。
4. **并发上限 ≤4**：每 Wave 同时活跃的 Worker 不超过 4，控制上下文漂移、重复调用与冲突面。
5. **先 Gate 后 Wave**：上一 Wave 的 Gate 通过才发下一 Wave；被 Gate 退回的任务按 §8 处理。

---

## 2. 角色定义

| 角色 | 短码 | 职责 | 主要文件（Owner） | 不负责 |
| --- | --- | --- | --- | --- |
| **协调者 / Router** | C | 任务下发、契约冻结审批、ADR、验收门 G0–G4、steer/close/重派、汇总交接 | `docs/01–05`（含 02 §11 ADR 区）、`docs/06`、进度汇总表 | 任何业务代码 |
| **地基 Worker** | F | 工程地基：依赖、配置、数据契约、tracer、CLI 骨架、LLMClient 抽象与 provider adapter、测试规范 | `pyproject.toml`、`config.py`、`schemas/`、`observability/tracer.py`、`observability/events.py`、`llm/client.py`、`llm/base.py`、`llm/providers/*`、`cli.py`（入口注册）、`tests/conftest.py` | 业务逻辑 |
| **数据 Worker** | D | 语料三源（Wiki/任务/约束）的编写与校验 | `data/wiki/`、`data/tasks/`、`data/constraints/` | 检索/生成代码 |
| **检索 Worker** | R | 切分、索引、混合检索、CLI search | `rag/`、`cli/search.py` | 语料内容、生成 |
| **Agent 编排 Worker** | A | Supervisor、QueryAgent、TaskAgent、LangGraph 图、SSE API、上下文拼装、修复与确认节点 | `agents/`、`api/`、`prompts/`、`cli/ask.py` | 校验规则、写入实现 |
| **校验 Worker** | V | 规则引擎 + Schema/业务/冲突三类规则 + 规则单测 + 对抗草案集 | `mcp_server/validator/`、`tests/unit/test_rules.py`、`tests/fixtures/adversarial/` | 生成、写入 |
| **写入/MCP Worker** | M | MCP Server、只读工具、preview/commit/rollback、写入器、破坏性测试 | `mcp_server/server.py`、`mcp_server/tools.py`、`mcp_server/writer.py`、`tests/integration/test_writer.py` | 校验引擎、路由 |
| **记忆 Worker** | Mem | 会话 checkpointer、偏好存储与抽取、记忆测试 | `memory/`、`cli/memory.py` | 上下文拼装 |
| **评测与可观测 Worker** | E | 黄金集/标注/对抗集（Agent 级）、三套评测、回归对比、Streamlit 面板、轨迹补全契约、基线报告 | `evaluation/`、`data/golden/`、`scripts/dashboard.py`、`docs/reports/` | 线上逻辑 |
| **演示交付 Worker** | P | Docker、演示脚本、录屏、README、简历/问答校准、代码清理收口 | `docker-compose.yml`、`Dockerfile`、`README.md`、`docs/demo-script.md`、录屏产物 | 业务实现 |

### 2.1 合并规则

有限算力下角色可合并，但**必须满足全部条件**，且合并后仍遵守「单一 Owner」：

| 合并对 | 条件 | 备注 |
| --- | --- | --- |
| F + V | 单人可承受 S0+S4（约 2–3 天）；校验仍只对 `mcp_server/validator/` 拥有 Owner，不与生成混写 | 两者都偏「低层 + 契约密集」，天然契合 |
| D + R | 语料作者与检索作者同人；仍保持 `data/wiki`、`data/tasks`、`data/constraints` 与 `rag/` 目录分离 | 检索紧贴语料，合并顺理成章 |
| V + M | 校验引擎与 MCP 写入都在 `mcp_server/` 包内；合并后 `validator/` 与 `tools.py` 仍分文件 | 降低「校验—写入」接口沟通成本 |
| Mem + A | 记忆量小（2–3 天）；合并后 `memory/` 与 `agents/context_builder.py` 仍分文件 | 偏好注入需跨这两个目录，合并省一轮交接 |
| E + P | 均偏「报告/演示」，活跃期都在 S7–S8，时间上基本不重叠 | 但 P 的演示验收规格必须在 Wave 0 由协调者代拟，避免「自己定标准自己验收」 |

**永不合并**（违反架构原则）：

- **C 不与任何 Worker 合并**——仲裁人必须独立，否则无人裁决契约冲突。
- **V 不与 A 合并**——校验必须独立于生成（[02 ADR-005](02-architecture.md)「校验独立于 LLM」）。
- **E 不与被评测的 Worker 合并**（E 不并入 R/A/M）——评测必须独立，否则「自己出题自己判卷」。

---

## 3. 阶段–任务–Worker 映射表

> 编号与验收口径对齐 [04-task-checklist.md](04-task-checklist.md)（62 项）；交付物对齐 04/03；「前置」为任务级依赖（可解耦处用「可 mock/stub」标注）。
> 全局 DoD（[04 §0](04-task-checklist.md)）对所有任务生效：ruff + pytest 通过、有测试、接口有文档/docstring、轨迹可观测、涉检索/生成/校验需附评测、无密钥/绝对路径。

### S0 工程地基与数据契约（Owner：地基 Worker F）

| 编号 | 任务 | Owner | 前置 | 交付物 | 验收口径 |
| --- | --- | --- | --- | --- | --- |
| S0-01 | 初始化 pyproject.toml | F | — | `pyproject.toml` | `pip install -e ".[dev]"` 成功；`import gameconfig_copilot` 无报错 |
| S0-02 | 配置管理 config.py | F | S0-01 | `config.py` | 缺关键配置报错并提示变量名；`.env` 被 `.gitignore` 覆盖 |
| S0-03 | TaskDraft 模型 + JSON Schema 导出 | F | S0-01/02 | `schemas/task.py` | `schema dump` 输出合法 JSON Schema；非法枚举被拒 |
| S0-04 | ValidationReport / Constraint 模型 | F | S0-03 | `schemas/report.py`、`schemas/constraint.py` | `E_*` 错误码与 [01 §7.4](01-product-outline.md) 一致 |
| S0-05 | 轨迹记录器 tracer | F | S0-01 | `observability/tracer.py`、`observability/events.py` | 嵌套 span 父子关系正确；`sk-` 开头字符串脱敏 |
| S0-06 | CLI 骨架 | F | S0-02 | `cli.py` | `gcc --help` 可用；未知子命令友好提示 |
| S0-07 | 测试与规范骨架 | F | S0-03/05 | `tests/`、`conftest.py`、`Makefile` | `pytest` 全绿（允许仅 schema 测试） |
| S0-08 | LLMClient Protocol + DeepSeek adapter（可替换内部模型） | F | S0-02（`GCC_LLM_*` 配置）+ S0-05（cost/latency span） | `llm/client.py`、`llm/base.py`、`llm/providers/deepseek.py`、`llm/providers/openai_compatible.py` | DeepSeek 可调通；切 `openai_compatible` + `base_url` 指向 mock 服务时业务零改动；`llm/` 外无厂商 SDK import |

### S1 知识库与混合检索

| 编号 | 任务 | Owner | 前置 | 交付物 | 验收口径 |
| --- | --- | --- | --- | --- | --- |
| S1-01 | Wiki 语料 3 篇 | D | —（内容，无代码依赖） | `data/wiki/*.md` | 每篇含唯一实体 ID；800–1500 字；可标题切分 |
| S1-02 | 历史任务配置 10 条 | D | S0-03（TaskDraft 冻结） | `data/tasks/*.json` | 全部通过 TaskDraft Schema；覆盖有分支/多目标/多奖励 |
| S1-03 | 约束表 17 条（YAML DSL） | D | S0-04（约束契约冻结） | `data/constraints/*.yaml` | 加载器可解析；每条唯一 ID 且含 `rationale` |
| S1-04 | JSON 感知切分器 | R | S1-01/02 | `rag/chunker.py` | 父块 + 子块；子块可反查 `task_id` |
| S1-05 | 索引构建（VectorStore Protocol + Chroma adapter + BM25 持久化） | R | S1-04 | `rag/index.py`、`rag/store.py`、`rag/stores/chroma.py` | 重建 ≤ 5 分钟；重复构建幂等；payload 映射仅在 adapter；切换 adapter 时业务零改动 |
| S1-06 | 混合检索（filters 中性化 + 自研 RRF k=60） | R | S1-05 | `rag/retriever.py`、`rag/fusion.py` | 中性 `filters` 生效；RRF 纯函数可单测；返回 `Hit` 带来源与分数 |
| S1-07 | 检索标注 20 条 + retrieval_eval | E | S1-06（检索可用）+ S1-01/02 | `data/golden/qrels.json`、`evaluation/retrieval_eval.py` | 输出 `reports/retrieval-baseline.json`（Recall@5 / MRR） |
| S1-08 | CLI search 子命令 | R | S1-06 | `cli/search.py` | 终端输出片段、分数、来源路径 |

### S2 QueryAgent 与只读闭环（Owner：Agent 编排 Worker A）

| 编号 | 任务 | Owner | 前置 | 交付物 | 验收口径 |
| --- | --- | --- | --- | --- | --- |
| S2-01 | Supervisor 路由节点 | A | S1-06 | `agents/supervisor.py` | 20 条样例意图判定准确率 ≥ 90% |
| S2-02 | QueryAgent 与只读工具集 | A | S2-01 + S1-06 | `agents/query_agent.py` | 工具列表不含写工具；越权工具不可见 |
| S2-03 | 引用约束 Prompt + 无依据拒答 | A | S2-02 | `prompts/query.md` | 库外问题回答「未检索到」，不编造 |
| S2-04 | SSE API（/sessions、/messages） | A | S2-01 | `api/main.py`、`api/routes.py`、`api/sse.py` | 事件顺序符合 [02 §10.2](02-architecture.md)；断流有 `error` |
| S2-05 | 会话管理（创建/恢复/超时） | A | S2-04 | `api/`（session 逻辑） | 同一 session 多轮上下文连续 |
| S2-06 | 只读闭环 e2e 测试 3 条 | A | S2-01~05 | `tests/e2e/test_query_flow.py` | 回答含引用且引用可解析 |

### S3 结构化草案生成

| 编号 | 任务 | Owner | 前置 | 交付物 | 验收口径 |
| --- | --- | --- | --- | --- | --- |
| S3-01 | 上下文拼装 context_builder | A | S1-06 + S0-03 | `agents/context_builder.py` | 超 6000 token 预算按优先级截断并 `warning` |
| S3-02 | 结构化生成（`LLMClient.structured` + 重试 1 次） | A | S3-01 + S0-03 + S0-08 | `agents/task_agent.py` | 10 次生成 ≥ 9 次可解析 JSON |
| S3-03 | draft 节点接入 LangGraph | A | S3-02 + S2-01 | `agents/graph.py` | 图可编译运行；草案带 `provenance.evidence` |
| S3-04 | 黄金需求 v1（10 条：正常 7 / 边界 3） | E | S0-03 + S1-01/02（语境） | `data/golden/requests_v1.jsonl` | 每条有期望字段断言 |
| S3-05 | Schema 层首次通过率基线 | E | S3-03（草案可用） | `evaluation/agent_eval.py` | `reports/fpvr-baseline.json` 有数字与样本明细 |

### S4 校验管线与自动修复

| 编号 | 任务 | Owner | 前置 | 交付物 | 验收口径 |
| --- | --- | --- | --- | --- | --- |
| S4-01 | 规则引擎（Rule 协议 + 注册表 + YAML 加载） | V | S0-04 + S1-03 | `mcp_server/validator/engine.py` | 新增规则类型 ≤ 30 行且自动注册 |
| S4-02 | Schema 规则（必填/枚举/正则/序号/ID） | V | S4-01 + S0-03 | `validator/schema_rules.py` | 每条规则 3 用例（通过/越界/边界） |
| S4-03 | 业务规则（奖励/等级/对白/区域） | V | S4-01 + S1-03 | `validator/business_rules.py` | `constraint_ref` 能指回 YAML 条目 |
| S4-04 | 冲突检测（ID 唯一/引用存在/成环） | V | S4-01 | `validator/conflicts.py` | 成环样例返回 `E_DEP_CYCLE` |
| S4-05 | 修复节点（回灌 ≤2 轮、超限 rejected） | A | S4-01（V 引擎）+ S3-03（graph） | `agents/graph.py`（repair 节点） | 失败不写入；轮次记录进轨迹 |
| S4-06 | 规则单测覆盖 ≥85% | V | S4-02/03/04 | `tests/unit/test_rules.py` | `pytest --cov` 达标 |
| S4-07 | 对抗草案集（越界/重复/缺失引用/成环/注入） | V | S4-02/03/04 | `tests/fixtures/adversarial/` | 5 条全部拦截且错误码正确 |

### S5 MCP 工具层与安全写入

| 编号 | 任务 | Owner | 前置 | 交付物 | 验收口径 |
| --- | --- | --- | --- | --- | --- |
| S5-01 | MCP Server 骨架（stdio、按 mode 注册） | M | S0-03/04 + S4-01 | `mcp_server/server.py` | 只读模式工具列表无写工具 |
| S5-02 | 只读工具（get_task_config / list_recent_tasks / list_constraints） | M | S5-01 | `mcp_server/tools.py` | 返回结构符合契约；不存在返回结构化错误 |
| S5-03 | validate_draft 工具（复用规则引擎） | M | S4-01（V 引擎） | `tools.py`（validate_draft） | 与本地调用结果一致 |
| S5-04 | preview_draft（unified diff + fingerprint） | M | S0-03 + S5-03 | `tools.py`（preview_draft） | 指纹对相同输入稳定、对改动敏感 |
| S5-05 | 写入器（路径守卫/锁/临时文件/os.replace/备份/审计） | M | S5-01 | `mcp_server/writer.py` | 中断后目标完整；越权路径 `E_PATH_DENIED` |
| S5-06 | commit_draft（指纹 + revision + 强制复检） | M | S5-04/05 | `tools.py`（commit_draft） | 指纹/版本不符均拒绝；成功后备份存在 |
| S5-07 | API 确认流（commit_token 签发/校验/过期） | A | S5-04/06（M 冻结 commit 契约） | `api/routes.py`（confirm 路由） | token 过期或跨草案使用被拒 |
| S5-08 | 破坏性测试（并发/中断/只读/软链逃逸） | M | S5-05/06 | `tests/integration/test_writer.py` | 并发后者 `E_STALE_REVISION`；无半写文件 |
| S5-09 | rollback_commit（备份恢复 + 新提交记录） | M | S5-05/06 | `tools.py`（rollback_commit） | 回滚后与备份一致；历史不删除 |

### S6 分层记忆

| 编号 | 任务 | Owner | 前置 | 交付物 | 验收口径 |
| --- | --- | --- | --- | --- | --- |
| S6-01 | 会话 checkpointer（SQLite）接入 | Mem | S2-05 + S3-03 | `memory/session.py` | 进程重启后同 session 可继续 |
| S6-02 | 偏好存储（接口 + 本地 JSON，Redis 可插拔） | Mem | —（独立） | `memory/preference.py` | `gcc memory show` 可读；可清除 |
| S6-03 | 偏好抽取（滑动平均 α=0.3，异常不更新） | Mem | S5-06（M commit 事件） | `memory/preference.py`（extract） | 3 次提交后偏好带收窄且可解释 |
| S6-04 | 偏好注入 context_builder（≤300 token） | A | S6-02/03（Mem 接口冻结）+ S3-01 | `agents/context_builder.py` | 第二次生成奖励落入偏好带 |
| S6-05 | 记忆测试（不覆盖显式指令/TTL/并发） | Mem | S6-01/02/03 | `tests/unit/test_preference.py` | 显式「给 500」优先于偏好 |

### S7 评测与可观测（Owner：评测 Worker E）

| 编号 | 任务 | Owner | 前置 | 交付物 | 验收口径 |
| --- | --- | --- | --- | --- | --- |
| S7-01 | 黄金需求 v2（30 条：20/5/5，含 2 注入） | E | S3-04 + S4-07（V 对抗集） | `data/golden/requests_v2.jsonl` | 每条含 `expected`；对抗含 2 条提示注入 |
| S7-02 | Ragas 评测（faithfulness 等） | E | S2-06（问答可用） | `evaluation/ragas_eval.py` | 固定 judge 模型与版本；结果可复现 |
| S7-03 | Agent 指标（FPVR/修复轮次/拦截率/P95/成本） | E | S3-05 + S4 + S5 | `evaluation/agent_eval.py`（扩展） | 报告含分母与失败样本分组 |
| S7-04 | 检索指标纳入统一入口 | E | S1-07 | `evaluation/retrieval_eval.py` + `cli/eval.py` | `gcc eval --suite all` 一次跑完 |
| S7-05 | 轨迹补全（tokens/cost/工具入参摘要） | E（定契约）+ 各 Owner（各自落点） | S0-05 | 埋点规范 + 各层 `attrs` 补齐 | 单条 trace 可还原完整决策链 |
| S7-06 | Streamlit 面板（列表/详情/指标） | E | S7-05 | `scripts/dashboard.py` | 按 trace_id 搜索并查看时间轴 |
| S7-07 | 回归对比 compare.py + 阈值告警 | E | S7-02/03/04 | `evaluation/compare.py` | FPVR −3pt 触发失败退出码 |
| S7-08 | 基线报告入库 | E | S7-07 | `docs/reports/baseline.md` | 报告含「改动 → 指标 → 结论」 |

### S8 演示打磨与交付（Owner：演示交付 Worker P）

| 编号 | 任务 | Owner | 前置 | 交付物 | 验收口径 |
| --- | --- | --- | --- | --- | --- |
| S8-01 | Docker Compose（api + mcp + 可选 qdrant/redis） | P | S5-01 + S2-04 | `docker-compose.yml`、`Dockerfile` | `docker compose up` 后 `/healthz` 正常 |
| S8-02 | 演示脚本排练（含反例 + 断网兜底） | P | S5/S6/S7 全链路 | `docs/demo-script.md` | 全程无手工改文件救场 |
| S8-03 | 5 分钟录屏 | P | S8-02 | 录屏产物 | 画面清晰、字幕/旁白完整 |
| S8-04 | README 更新（真实命令/指标/截图/架构图） | P | S7-08 | `README.md` | 新人按 README 能跑起来 |
| S8-05 | 简历 bullet + 问答数字校准 | P | S7-08 | `docs/05`（数字校准） | 所有数字可在 `docs/reports/` 找到出处 |
| S8-06 | 代码清理（TODO/死代码/错误码统一） | 各 Owner（按 §5 清理自有文件）+ P 汇总 lint | S7 | 全仓 + `ruff` 报告 | `ruff` 无警告 |

---

## 4. 并行波次 Wave 0–4 与依赖图

### 4.1 依赖图

```text
阶段依赖（W=Wave；∥=可并行启动）：

S0 地基(F) ──G0──► 语料3源(D) ─► 检索(R) ─► 只读闭环(A) ─► 草案(A)      [W0–W2]
   │
   ├─► 校验(V) ─► 写入(M) ─► 记忆(Mem)                                   [W1–W3]
   │      ∥S1/S2/S3        ∥S3 核心
   │
   └─► 评测与观测(E) ─► 交付(P)                                          [W4]

跨层合流：A.草案.graph → Mem.会话；M.commit 事件 → Mem.偏好；S3+S4+S5+S6 → E
```

**关键并行化判断**（相对 [03 §1.2](03-implementation-plan.md) 顺序链的显式调整）：

1. **S4 校验可与 S1/S2 并行**——校验引擎输入是 `(draft, constraints, repo_snapshot)` 纯函数，不依赖检索/生成；只要 S0 契约 + S1-03 约束语料冻结即可开工。唯一的例外是 S4-05 修复节点（依赖 S3-03 的 graph），推后到 Wave 3。
2. **S5 写入核心可与 S3 草案并行**——`commit_draft` 只依赖 TaskDraft Schema + 校验引擎，不依赖生成链路；只有端到端「草案 → 校验 → 预览 → 提交」联调（S5-07）需要两者都在。
3. **S6 记忆在 S5 commit 事件之后**——S6-01 会话记忆随 graph 到位即可开始；S6-02/03 偏好抽取需 S5-06 commit 事件，故 S6 整体放 Wave 3，与 S5 收尾、graph 接线并行。

### 4.2 Wave 表

| Wave | 覆盖阶段 | 并行 Worker（并发上限） | 出口 Gate | 可演示切片 |
| --- | --- | --- | --- | --- |
| **Wave 0** | S0 + S1 语料 | F(S0-01~08) + D(S1-01/02/03)，**≤2** | G0 | `schema dump` 输出 JSON Schema + 语料三源就绪 + `LLMClient` 可调通 |
| **Wave 1** | S1 检索 + S4 校验 | R(S1-04/05/06/08) + V(S4-01/02/03/04/06/07) + E(S1-07)，**≤3** | G1（检索）+ G2（校验前半） | CLI 检索带来源；对抗草案被规则引擎拦截 |
| **Wave 2** | S2 + S3 + S5 核心 | A(S2-01~06 → S3-01/02/03) + M(S5-01~06/08/09) + E(S3-04/05)，**≤3** | G1（闭环）+ G2（草案）+ G3（写入核心） | 一句话问答带引用；一句话→草案；越界草案被拦截 |
| **Wave 3** | 接线 + 写入闭环 + 记忆 | A(S4-05 / S5-07 / S6-04) + M(S5 收尾联调) + Mem(S6-01/02/03/05) + E(S7-01)，**≤4** | G3 | 确认后安全落盘 + 备份；第二次生成沿用偏好 |
| **Wave 4** | S7 + S8 | E(S7-02~08) + P(S8-01~06) + 各 Owner(S7-05 埋点)，**≤3** | G4 | 一键评测报告 + 轨迹面板 + 5 分钟完整演示 |

### 4.3 每条并行线的读写边界为何不冲突

| Wave | 并行线 | 写路径（互斥） | 只读依赖（不写） | 冲突点与消解 |
| --- | --- | --- | --- | --- |
| 0 | F ∥ D | F 写 `src/`（schemas/config/observability/llm/cli/tests）；D 写 `data/wiki`、`data/tasks`、`data/constraints` | D 读 F 的 `schemas/`（S1-02/03 校验语料） | 无共享文件。D 只依赖 S0-03/04 的**冻结产物**，不依赖 F 的其余文件 |
| 1 | R ∥ V ∥ E | R 写 `rag/`+`cli/search.py`；V 写 `mcp_server/validator/`+`tests/unit/test_rules.py`+`tests/fixtures/adversarial/`；E 写 `evaluation/retrieval_eval.py`+`data/golden/qrels.json` | 三者都**只读** `data/wiki`、`data/tasks`、`data/constraints`（D 已冻结） | 无重叠写。V 与 E 各自维护不同对抗集（V=tests/fixtures，E=data/golden），§3 已分路径 |
| 2 | A ∥ M ∥ E | A 写 `agents/`+`api/`+`prompts/`；M 写 `mcp_server/server.py`、`mcp_server/tools.py`、`mcp_server/writer.py`+`tests/integration/`；E 写 `data/golden/requests_v1.jsonl`+`evaluation/agent_eval.py` | A/M 只读 `schemas/`；M 只读 `validator/`（V 已冻结）；E 只读语料 | A 与 M 通过「MCP 工具签名」对接，不互相改文件；M 不 import `agents/`（[02 §2](02-architecture.md) 依赖方向） |
| 3 | A ∥ M ∥ Mem ∥ E | A 写 `agents/graph.py`（repair/human_gate）+`api/routes.py`（confirm）+`agents/context_builder.py`；M 写 `tools.py`（rollback/收尾）；Mem 写 `memory/`；E 写 `data/golden/requests_v2.jsonl` | A 读 Mem 冻结的 `Preference` 接口（S6-04）；Mem 读 M 的 commit 事件 | A 与 Mem 都涉及「偏好」但分文件：Mem 拥有 `memory/preference.py`，A 拥有 `agents/context_builder.py`，以 Mem 先冻结的接口对接 |
| 4 | E ∥ P ∥ 各 Owner | E 写 `evaluation/`+`scripts/dashboard.py`+`docs/reports/`；P 写 `Dockerfile/compose`+`README.md`+`docs/05`+`docs/demo-script.md`；各 Owner 写各自文件的 `attrs`（S7-05） | P 只读 `docs/reports/`（S8-04/05 取数字）；各 Owner 只读 E 的埋点规范 | S7-05 由 E 定**规范**、各 Owner 在**自己文件**落点，E 不改他人文件（§5 例外流程） |

---

## 5. 共享契约与文件所有权

### 5.1 原则

1. **每个文件/目录有且仅有一个 Owner Worker**，Owner 是唯一能写它的人，其他人一律只读。
2. **同一文件禁止两个 Worker 同时修改**；需要改非自有文件时，走「契约变更请求 → 协调者批准 → Owner 落盘 → ADR」。
3. **多 Owner 倾向的文件必须拆分**：`cli.py`、`context_builder.py`、`graph.py`、`api/routes.py` 这类天然被多 Worker 触碰的文件，按「每个 Worker 只写自己的子模块」拆解（见下表），而不是共享同一文件。

### 5.2 所有权矩阵（共享契约与高危文件）

| 资源 / 路径 | 单一 Owner | 读者（只读） | 变更流程 |
| --- | --- | --- | --- |
| `schemas/`（TaskDraft / ValidationReport / Constraint） | **F（地基）** | 全体 | G0 冻结；后续变更须 C 批准 + ADR + F 落盘 |
| MCP 工具签名（`mcp_server/server.py`、`tools.py` 注册与签名） | **M（写入）** | A（import）、E（评测） | 变更须 C 批准 + ADR；`validate_draft` 内部引擎归 V，签名归 M |
| SSE 事件（`api/sse.py` + 事件模型，[02 §10.2](02-architecture.md)） | **A（编排）** | E（面板）、P（演示） | 变更须 C 批准 + ADR；事件名/字段是跨层契约 |
| `agents/graph.py`（LangGraph 全图） | **A（编排）** | V（引擎被调用）、M（commit 被调用） | A 是唯一可改图的人；repair（S4-05）/human_gate（S5-07）都是 A 的活 |
| `data/wiki/`、`data/tasks/`、`data/constraints/` | **D（数据）** | R/V/E 只读 | 语料三源冻结；改语料须 D 执行 + 触发索引重建 |
| `data/golden/`（qrels / requests / Agent 级对抗） | **E（评测）** | 全体只读 | E 唯一可写评测夹具；V 的校验级对抗放 `tests/fixtures/adversarial/`，不混入 data/golden |
| `mcp_server/validator/` | **V（校验）** | M（复用）、A（repair 调用） | V 唯一可改规则引擎 |
| `rag/`（含 `rag/store.py`、`rag/fusion.py`、`rag/stores/*`） | **R（检索）** | A/E 只读（只依赖 `VectorStore` Protocol） | 换存储实现只新增 `rag/stores/<backend>.py`，`agents/` 与 `rag/retriever.py` 零改动 |
| `llm/`（`llm/client.py` / `llm/base.py` / `llm/providers/*`） | **F（地基）** | A/E 只读（只依赖 `LLMClient` Protocol） | 换模型/换网关只新增或替换 `llm/providers/<vendor>.py` + 环境变量，`agents/`、`evaluation/` 零改动；厂商 SDK 不得出现在 `llm/` 之外（[ADR-008](02-architecture.md)） |
| `memory/` | **Mem（记忆）** | A（注入时只读接口） | — |
| `observability/tracer.py`、`events.py` | **F（地基，S0 冻结）** | 全体只读 | `attrs` 设计为开放字典；S7-05 只需各 Owner 补 `attrs`，不改结构；确需改结构走契约变更 |
| `cli.py`（入口注册）+ `cli/schema.py` | **F** | — | 每个 Worker 拥有自己的 `cli/<x>.py` 子命令文件 |
| `cli/search.py` / `cli/ask.py` / `cli/eval.py` / `cli/memory.py` / `cli/ingest.py` | 分别归 **R / A / E / Mem / R** | — | 子命令按模块拆文件，互不冲突 |
| `agents/context_builder.py` | **A（编排）** | Mem（注入接口被调用） | S6-04 偏好注入是 A 的活，读 Mem 的冻结接口 |
| `api/routes.py` | **A（编排）** | — | S5-07 confirm 路由是 A 的活，读 M 的 commit 契约 |
| `docs/01–05`（ADR 统一追加在 02 §11） | **C（协调）**（05 的数字校准 S8-05 由 P 提议、C 批准） | 全体只读 | 基线文档冻结；改须 C 批准 + 记录 |
| `docs/reports/` | **E（评测）** | P（取数） | 评测产物 |
| `README.md` | **P（演示交付）** | — | S8-04 更新 |
| `data/backups/`、`data/.locks/`、`traces/` | **运行时生成**（M 写入器、F tracer 各自维护） | — | 不进版本库 |

### 5.3 契约变更流程（ADR）

```text
Worker 发现需要改非自有契约
  → 向 C 提交「契约变更请求」（改什么 / 为什么 / 影响哪些 Worker / 迁移成本）
  → C 评估：是否影响已冻结的 schema / 工具签名 / SSE 事件 / 数据契约
  → 批准 → 由该契约的 Owner 落盘，C 记录 ADR（统一追加到 02 §11 ADR 区，不新建 `docs/adr/`），并广播版本号
  → 未批准 → 冻结，Worker 在原契约内适配，或改走新文件（不破坏旧契约）
```

**广播要求**：契约一旦变更，C 必须在下一 Wave 下发的任务包里附带「契约快照版本号」，所有 Worker 开工前先核对，杜绝各自基于旧契约返工。

### 5.4 检索层契约（VectorStore Protocol）

**Owner：R（检索 Worker）**。这是 R 对 A/E 的只读契约，也是唯一允许跨 Worker 的检索接口。

| 项 | 约定 |
| --- | --- |
| 接口 | `VectorStore.upsert(chunks: list[Chunk])` / `search(query: str, filters: dict, top_k: int) -> list[Hit]` / `health() -> bool` |
| 跨 Worker 契约 | `filters: dict` 的键与值语义（如 `source_type`、`task_type`、`level_band`）由 R 冻结并写入 [02 §5.2](02-architecture.md)；A/E 只构造中性字典，**不得**拼 Chroma `where` 或 Qdrant `Filter` |
| 返回 | `list[Hit]`（`chunk_id` / `text` / `score` / `source_id` / `metadata`）；A/E 只消费 `Hit`/`Evidence`，不接触底层 collection |
| 换库 | 只新增 `rag/stores/<backend>.py` adapter；`agents/`、`rag/retriever.py`、`evaluation/` 零改动 |
| 所有权 | R 拥有 `rag/store.py`、`rag/fusion.py`、`rag/stores/*`、`rag/retriever.py`；A/E 只读；变更走 §5.3 契约变更 + ADR |
| 单测 | `rag/fusion.py` 的 RRF 是纯函数，归 R 的 unit test；adapter 的 `filters` 翻译由 R 用契约测试覆盖 |

> 业务层禁止出现 `collection.get(ids=...)`、`where={"$and": ...}` 等库特有语法；违反视为契约冲突（§8），由 C 回退并记 ADR。

### 5.5 生成模型契约（LLMClient Protocol）

**Owner：F（地基 Worker）**。这是 F 对 A/E 的只读契约，也是业务层唯一允许的生成调用入口。

| 项 | 约定 |
| --- | --- |
| 接口 | `LLMClient.chat(messages, *, temperature=0.2, max_tokens=None) -> LLMResponse` / `structured(messages, schema, *, temperature=0.0) -> dict` / `health() -> bool` |
| 返回 | `chat` 返回 `LLMResponse`（`content` / `model` / `usage` / `latency_ms` / `finish_reason`）；`structured` 返回已通过 Schema 解析的 `dict`，解析失败抛 `LLMStructuredOutputError` |
| 跨 Worker 契约 | A 只调 `structured` 生成 `TaskDraft`（解析失败重试 1 次，对齐 [03 S3-02](03-implementation-plan.md)）；E 的 Ragas judge 只调 `chat`；两者都**不得** import `openai` / `deepseek` 等厂商 SDK，也不得自行拼 HTTP 请求 |
| 配置 | `GCC_LLM_PROVIDER`（默认 `deepseek`）、`GCC_LLM_MODEL`、`GCC_LLM_BASE_URL`、`GCC_LLM_API_KEY`、`GCC_LLM_TIMEOUT_S`；密钥只进 `.env`，不入库、不落轨迹 |
| 换模型 | 只改上述环境变量，或新增 `llm/providers/<vendor>.py` adapter；`agents/`、`evaluation/` 零改动（[ADR-008](02-architecture.md)） |
| 所有权 | F 拥有 `llm/client.py`、`llm/base.py`、`llm/providers/*`；A/E 只读；变更走 §5.3 契约变更 + ADR |
| 单测 | provider adapter 用 mock HTTP 服务做契约测试（不依赖真实密钥/网络）；`llm/base.py` 的重试与 token 成本统计归 F 的 unit test |
| 边界 | embedding **不**并入 `LLMClient`（已由 `VectorStore` adapter 持有）；换 embedding 后续另开 `EmbeddingProvider`，本 Wave 不做 |

> 业务层禁止出现 `openai.OpenAI(...)`、`client.chat.completions.create(...)` 等厂商调用；违反视为契约冲突（§8），由 C 回退并记 ADR。

---

## 6. 验收门 G0–G4

> 每门对齐 [03 各阶段 DoD](03-implementation-plan.md) 与 [04 验收口径](04-task-checklist.md)。「谁检查」区分**自证**（Owner 附证据）与**独立复核**（评测 Worker 或协调者）。
> 通过门槛 = **自证证据齐全 + 独立复核签字**；任一不通过 → 退回对应 Worker（§8），不进下一 Wave。

| 门 | 覆盖阶段 | 检查什么 | 谁检查 | 不通过的退回 |
| --- | --- | --- | --- | --- |
| **G0 地基门** | S0 | `schema dump` 输出合法 JSON Schema；`pytest` 全绿；tracer 能写带 `trace_id` 的 JSONL 且 `sk-` 脱敏；`gcc --help` 可用；`.env` 被 gitignore；依赖可安装；TaskDraft 与 [01 §7.1](01-product-outline.md) 字段一致；默认 provider 可 `health()` 调通且 `llm/` 外无厂商 SDK import（S0-08） | C 复核 + F 自证 + 全体 Worker 交叉读契约确认 | 退回 F，修完再验；契约未冻结前任何 Worker 不得开工写业务 |
| **G1 只读闭环门** | S1 + S2 | 检索基线报告存在（Recall@5 记入 `reports/retrieval-baseline.json`）；过滤生效；CLI search 带来源；3 问答带可解析引用；库外问题拒答；意图判定 ≥90%；SSE 事件顺序符合 [02 §10.2](02-architecture.md)；轨迹见 `route→retrieval→llm` | E 独立复核检索基线；C 复核闭环演示；A 附 e2e 证据 | 检索不合格退回 R（查切分/过滤/标注）；闭环不合格退回 A |
| **G2 生成与校验门** | S3 + S4 | 黄金集 v1 生成合法率达标；FPVR 基线记录；规则单测覆盖 ≥85%；对抗草案（越界/重复/缺失引用/成环/注入）5/5 拦截且错误码正确，并输出 V 的错误码清单供 G4 覆盖核对；修复 ≤2 轮超限 `rejected` 且零写入 | E 独立复核 FPVR 与对抗拦截；C 复核；V 附单测报告 | 生成不合格退回 A；校验不合格退回 V；两者都涉及退回 A+V 联调 |
| **G3 安全写入门** | S5 + S6 | 只读模式工具列表无写工具；越权路径 `E_PATH_DENIED`；中断后目标文件完整；并发写后者 `E_STALE_REVISION`；反例「1 级 1000 金币」零写入；commit_token 过期/跨草案被拒；偏好不覆盖显式指令；`memory show` 可读可清除 | C 复核反例演示；E 复核破坏性测试证据；M/Mem 附测试报告 | 写入不合格退回 M；记忆不合格退回 Mem；确认流不合格退回 A |
| **G4 评测与交付门** | S7 + S8 | `gcc eval --suite all` 一键出报告；面板可按 trace_id 看时间轴；回归对比阈值告警（FPVR −3pt 失败）；指标达标：[01 §3.2](01-product-outline.md) 目标（Recall@5 ≥0.85、FPVR ≥80%、拦截率 100%、faithfulness ≥0.90、P95 ≤15s）；E 的 Agent 级对抗覆盖 V 的全部错误码（以 G2 清单核对）；基线报告入库；5 分钟演示无救场；断网可跑；README 可复现；简历数字有出处 | C 主持最终评审；E 复核指标；P 现场走演示清单 | 指标不达标退回 E 定位归因；演示有卡点退回 P；跨层问题退回对应 Owner |

**退回规则**：退回时 C 必须给出**具体未达标项 + 归因到哪个 Worker 的哪个文件**，不能只写「不通过」；退回任务的修复不插队，占用该 Worker 的下一轮，其他不相关的 Wave 可继续（见 §8）。

---

## 7. 交接协议

### 7.1 Worker 报告模板（每完成一个任务/一批任务必交）

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| 范围 scope | ✅ | 本批覆盖的任务编号（对齐 04）+ 一句话目标 |
| 改动文件 | ✅ | 相对路径清单 + 是否新建；**只允许列出自有 Owner 路径** |
| 测试与证据 | ✅ | pytest 结果、评测指标变化、trace_id 链接、演示截图/输出；无证据视同未完成 |
| 未决问题 | ✅ | 阻塞项、待拍板项、对其它 Worker 的接口疑问 |
| 风险 | ✅ | 影响下一 Wave 的隐患、契约疑点、需要 C 裁决的冲突 |
| 依赖声明 | 建议 | 声明「我读/写了谁的什么」，供 C 做冲突核查 |

### 7.2 协调者汇总与 steer

```text
每个 Worker 交付 → C 校验：① 是否只动了自有路径 ② 证据是否齐全 ③ 是否触碰共享契约
  → C 更新进度表（docs/06 附进度页或 docs/progress/）
  → C 汇总未决问题，逐条决策：steer（给方向）/ 关闭（记录不做）/ 重派（换 Worker）
  → C 按 Gate 复核 → 通过则发下一 Wave（附契约快照版本号 + 本 Wave 只读边界）
```

**steer 原则**：C 只给**方向与边界**（改哪、不改哪、和谁对齐），不替 Worker 写代码；每次 steer 必须明确「下一步做什么、什么时候再交、交什么证据」。

---

## 8. 冲突与阻塞处理

| 冲突/阻塞 | 判定标准 | 处理路径 |
| --- | --- | --- |
| **契约冲突**（契约漂移） | 两个 Worker 对同一契约理解不同，或某 Worker 私改了非自有契约文件 | C 冻结争议契约 → 回退非 Owner 改动 → 以 02/03/04 基线 + G0 产物为准 → 记 ADR → 广播新版本号 |
| **文件冲突**（并发写同一文件） | 检测到同文件被两个 Worker 修改 / git 冲突 | 按 §5 所有权裁决，非 Owner 改动作废；若文件天然多 Owner，则 C 牵头**拆子模块**后再分派 |
| **测试失败**（互相踩） | CI 红且可归因到某 Worker 的改动 | C 归因 → steer 给 Owner 修复（限 1 轮）→ 仍失败则回退该改动并重派；禁止「关掉他人测试」来通过 |
| **依赖未就绪** | 前置 Gate 未过 / 前置交付物缺验收证据 | 阻塞该 Worker，等待前置 Gate；C 评估是否给「mock/stub 最小替代」以解耦（如 S5 联调前用 mock commit 结果） |
| **Worker 卡住** | 连续 2 个报告周期无有效交付物，或同一验收项重试 ≥3 次 | **steer**（注入方向与上下文）→ 仍无进展 **close**（回收已完成文件、记录结论）→ 需要则 **重派**（给新 Worker 完整交接包） |

**steer / close / 重派 定义**

- **steer**：C 给 Worker 继续做下去的方向性反馈（保留任务与已有产物）。
- **close**：终止任务/角色，回收已完成文件并记录「为何不做 / 降级为何」，不重派。
- **重派**：close 后交另一 Worker，附**完整交接包** = 原 Worker 报告 + 已改文件清单 + 契约快照版本 + 未决问题。重派前 C 必须确认新 Worker 与旧 Worker 文件所有权无重叠。

**判定标准的硬性边界**（避免「感觉卡住」的主观判断）：
- 「无有效交付物」= 未提交报告 / 提交的报告无测试证据且无未决问题说明。
- 「重试」= 对同一验收口径的同一失败模式；换了一个独立的新失败不算重试。

---

## 9. 第一波可立即下发的任务清单（Wave 0）

> 只列任务、负责人、依赖、验收，不写代码。这是当前（零代码状态）可直接派发的最小闭环。

| # | 任务 | 负责人 | 依赖 | 验收 |
| --- | --- | --- | --- | --- |
| 1 | S0-01 初始化 `pyproject.toml` | 地基 Worker F | — | `pip install -e ".[dev]"` 成功；`import gameconfig_copilot` 无报错 |
| 2 | S0-02 配置管理 `config.py` | F | #1 | 缺关键配置报错并提示变量名；`.env` 被 gitignore |
| 3 | S0-03 `TaskDraft` + JSON Schema 导出 | F | #1、#2 | `schema dump` 合法 JSON Schema；非法枚举被拒 |
| 4 | S0-04 `ValidationReport` / `Constraint` 模型 | F | #3 | `E_*` 错误码与 [01 §7.4](01-product-outline.md) 一致 |
| 5 | S0-05 轨迹记录器 tracer | F | #1 | 嵌套 span 父子正确；`sk-` 脱敏 |
| 6 | S0-06 CLI 骨架 | F | #2 | `gcc --help` 可用；未知子命令友好提示 |
| 7 | S0-07 测试骨架 | F | #3、#5 | `pytest` 全绿（允许仅 schema 测试） |
| 8 | S0-08 LLMClient Protocol + DeepSeek adapter | F | #2、#5 | DeepSeek 可调通；切 `openai_compatible` + `base_url` 指向 mock 服务时业务零改动；`llm/` 外无厂商 SDK import |
| 9 | S1-01 Wiki 语料 3 篇 | 数据 Worker D | —（内容） | 每篇唯一实体 ID；800–1500 字；可标题切分 |
| 10 | S1-02 历史任务配置 10 条 | D | #3（TaskDraft 冻结后） | 全部通过 TaskDraft Schema；覆盖分支/多目标/多奖励 |
| 11 | S1-03 约束表 17 条（YAML DSL） | D | #4（约束契约冻结后） | 加载器可解析；唯一 ID 且含 `rationale` |

**下发顺序**：先发 #1–#8（F，串行推进）；#9（D）可同步发（无代码依赖）；#3/#4 冻结后立即触发 #10/#11（D）。C 在 Wave 0 同时让 P 提交「演示验收规格」草案（不占并发名额，用于 §1 演示驱动）。

---

## 10. 风险与取舍

| 风险（多 Agent 最常见） | 表现 | 对策 | 取舍 |
| --- | --- | --- | --- |
| **契约漂移** | 各 Worker 基于不同版本的 schema/工具签名开发，集成时大量返工 | 单一 Owner + G0 冻结 + 变更走 ADR + 每次下发附契约快照版本号 | 牺牲一点「就地改契约」的灵活性，换取集成一次通过率 |
| **重复实现** | 两个 Worker 各自实现同一能力（如两套校验、两套检索封装） | §5 所有权矩阵 + 开工前 C 确认「谁拥有」+ PR 评审发现重复即合并 | 增加 C 的前置核查成本 |
| **测试互相踩** | A 改 graph 导致 M 的集成测试红，或互相关测试 | 测试分层归属（unit 归 Owner、integration 归 M、eval 归 E）+ CI 隔离 + 禁止关他人测试 | 需要更细的测试目录划分 |
| **上下文不一致** | 各 Worker 对需求/约束的记忆漂移，A 与 M 对「commit 该不该复检」认知不同 | 契约文档（02/03/04）为唯一真相源 + Worker 开工前必读 + 报告引用契约版本 + C 下发带契约快照 | 每个 Worker 都要读 4 份基线文档，启动成本高 |
| **评测独立性被破坏** | 生成者自己出黄金集，指标失真 | E 永不与被评测 Worker 合并；黄金集由 E 独立编写、交叉检查 | E 的产出节奏须提前（Wave 1 就参与） |
| **数据与代码节奏错位** | 代码好了语料没齐（或反之） | D 在 Wave 0 就并行产语料；S1-02/03 以 S0-03/04 冻结为触发点；语料变更触发索引重建 | D 与 F 同时开工，前期两线并行 |
| **成本与调度开销放大** | 多 Worker 重复调用 LLM / 频繁交接 | 并发上限 ≤4；评测用「录制回放」缓存 LLM 响应（[03 §2 S7](03-implementation-plan.md)）；交接走统一报告模板 | 调度比单人开发多了协调成本，但换取并行缩短日历时间 |
| **横切任务归属不清** | S7-05 轨迹补全、S8-06 代码清理这类「动全仓」的任务无人认领或乱改 | 明确为「契约者定规范 + 各 Owner 落点 + 汇总者验证」的收口模式（§4.3 Wave 4、§5） | 这类任务拆成多个小派发，比单人扫全仓慢但安全 |

**底线取舍**（沿用 [03 §9](03-implementation-plan.md)）：并行化**不能砍**校验管线、两阶段提交、原子写、评测基线、反例演示；Streamlit 面板 / Redis / reranker / rollback 可按原取舍顺序降级。并行带来的额外取舍是：**为控制冲突，宁可多一个「契约冻结 + ADR」的串行环节，也不允许两个 Worker 同时改同一文件。**

---

## 11. 附注：与 03/04 的一致性检查结论

本文档在编写时对 03/04 做了逐任务比对，以下歧义已按「04 为唯一事实源」处理；v0.2 4 项 + v0.3 1 项待拍板已全部关闭：

| # | 发现的歧义 / 不一致 | 本文档的处理 |
| --- | --- | --- |
| 1 | 04 变更记录原写「8 阶段 / 52 项」，v0.2 实为 **61 项**，v0.3 增 S0-08 后为 **62 项**（03 各阶段合计 53→54 项） | 以 04 清单 **62 项**为准；v0.3 已把 04 变更记录改为 61→62 |
| 2 | 03 与 04 在 S0/S1/S2/S4/S5/S7 的任务粒度与编号不一致（03 S0 无 S0-07，测试规范并入 S0-06；03 把「演示语料」合一，04 拆成 3 项；03 的 S7-02=检索评测，04 的 S7-02=Ragas、S7-04=检索统一入口） | 编号对齐 04；03 的 DoD 作为验收门依据，不做编号引用；v0.3 新增的 S0-08 在 03/04 编号一致（03 表已加脚注说明缺口） |
| 3 | 对抗样本数量：01 §3.2 写「6 条」，05 §4 写「5 条」，04 S4-07/S7-01 均写「5 条」 | 按 5 条（04 为准）；v0.2 已将 01 §3.2/§6.5 的「6 条」勘误为「5 条」 |
| 4 | 03 §1.2 对 S5/S6 关系的表述自相矛盾（「S6 与 S5 可并行」又说「放在 S5 之后」） | 按「S6-01 随 graph 就绪可开，S6-02/03 依赖 S5 commit 事件」拆分（§4.1） |
| 5 | README 仓库结构写 `config/` 目录，03/04 写 `config.py` 单文件 | 以 03/04 的 `config.py` 为准，`config/*.yaml` 作为运行配置目录（不冲突） |
| 6 | 错误码：02 §12 出现 `E_VALIDATOR_CRASH`、`E_IO`，但 01 §7.4 错误码表未收录 | v0.2 已将 `E_VALIDATOR_CRASH`/`E_IO` 正式补入 01 §7.4；S0-04 仍以 01 §7.4 为准 |
| 7 | 评测夹具归属：S4-07（校验级对抗）与 S7-01（Agent 级对抗含注入）语义重叠 | 分路径隔离且不共用：V 拥有 `tests/fixtures/adversarial/`，E 拥有 `data/golden/`；G2/G4 做覆盖核对，E 的 Agent 级对抗须覆盖 V 的全部错误码（§5.4/§6） |
| 8 | 生成模型选型与可替换性：先用 DeepSeek、日后换公司内部模型（01 §12 Q1） | **DeepSeek 默认 + `LLMClient` 薄抽象**：`llm/` 归 F，业务层只读 `LLMClient` Protocol；内部模型经 `openai_compatible` + `base_url` 接入，`agents/`、`evaluation/` 零改动；embedding 不并入（后续 `EmbeddingProvider` 可选）。落地：[02 §11 ADR-008](02-architecture.md)、[01 §9/§12](01-product-outline.md)、03/04 S0-08、06 §2/§3/§5.2/§5.5 |

**协调者拍板结论（v0.2 4 项 + v0.3 1 项，全部 ✅ 已定）**

| # | 待拍板项 | 结论 | 落地位置 |
| --- | --- | --- | --- |
| 1 | 数字勘误 | 以 04 的 **62 项**为准（v0.2 为 61，v0.3 增 S0-08）；01 §3.2 对抗集「6 条」改「5 条」；04 变更记录 52→61→62 | 04 §5；01 §3.2/§6.5 |
| 2 | `E_VALIDATOR_CRASH`/`E_IO` 是否入枚举 | **纳入** 01 §7.4；S0-04 验收沿用「与 01 §7.4 一致」 | 01 §7.4；04 S0-04（口径不变） |
| 3 | 契约变更 ADR 存放 | **统一追加到 02 §11 ADR 区**，不新建 `docs/adr/` | 02 §11；06 §2/§5.2/§5.3 |
| 4 | 对抗集是否共用 | **不共用**：V 拥有 `tests/fixtures/adversarial/`，E 拥有 `data/golden/`；G2/G4 做覆盖核对，E 的 Agent 级对抗须覆盖 V 的全部错误码 | 06 §5.2/§6；04 S4-07/S7-01 |
| 5 | 生成模型选型与可替换性（v0.3） | **DeepSeek 默认 + `LLMClient` 薄抽象**；内部模型经 `openai_compatible`（`base_url` 可配）接入，业务层零改动；embedding 不并入该抽象 | [02 §11 ADR-008](02-architecture.md)；[01 §9/§12](01-product-outline.md)；04 S0-08；06 §5.5 |

---

## 12. 变更记录

| 版本 | 日期 | 变更 | 原因 |
| --- | --- | --- | --- |
| v0.1 | 2026-11-12 | 初始多 Agent 协作计划（Wave 0–4、G0–G4、所有权矩阵、7 项一致性附注） | 立项 |
| v0.2 | 2026-11-13 | 新增检索层 VectorStore 抽象 + 误记勘误：§3 S1-05/S1-06、§5.4 检索层契约、§5.2/§5.3 ADR 归属、§6 G2/G4 覆盖核对、§11 关闭 4 项待拍板 | 锁定「先 Chroma、S7 迁 Qdrant 只换 adapter」；消除跨文档计数与契约歧义 |
| v0.3 | 2026-11-13 | §2 F 的 Owner 文件加 `llm/`；§3 加 S0-08 映射并把 S3-02 改为经 `LLMClient.structured`；§4.2/§4.3/§9 Wave 0 覆盖 S0-08；§5.2 所有权矩阵加 `llm/` 行；§5.5 新增生成模型契约（`LLMClient`）；§6 G0 补 S0-08 验收；§11 计数 61→62 并关闭生成模型选型 | 用户新决策：先用 DeepSeek + 保留可替换接口（ADR-008）；多 Worker 并行下 `llm/` 需单 Owner 归 F |
