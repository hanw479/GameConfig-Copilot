# GameConfig Copilot · 任务清单（存档基线 v0.3）

> **用途**：编码阶段的唯一进度事实源。完成一项勾一项，每周更新一次状态。
> **规则**：不插队——新想法先记到 §4「候选池」，评审后再进主清单；状态只在验证通过后变更。
> **配套**：[实施路线](03-implementation-plan.md) 说明每个阶段的目标与验收标准。

---

## 0. 全局完成定义（DoD）

任一任务标记 ✅ 前，必须同时满足：

- [ ] 代码通过 `ruff check` 与 `pytest`；
- [ ] 有对应的测试（单测/集成/e2e 至少一种），失败路径也覆盖；
- [ ] 对外接口（工具/端点/CLI）的输入输出已写入文档或 docstring；
- [ ] 轨迹中有可观测 span，能定位该功能；
- [ ] 若影响检索/生成/校验，已跑评测并记录指标变化；
- [ ] 无密钥、无本地绝对路径硬编码。

---

## 1. 任务清单

### S0 工程地基与数据契约（0.5–1 天）

- [ ] **S0-01** 初始化 `pyproject.toml`（依赖分组、Python ≥3.10、ruff/pytest 配置）· P0
  - 验收：`pip install -e ".[dev]"` 成功；`python -c "import gameconfig_copilot"` 无报错
- [ ] **S0-02** 配置管理 `config.py`（`GCC_*` 环境变量、`.env` 加载、默认值）· P0
  - 验收：缺关键配置时启动报错并提示变量名；`.env` 被 `.gitignore` 覆盖
- [ ] **S0-03** 定义 `TaskDraft` Pydantic 模型 + JSON Schema 导出 · P0
  - 验收：`schema dump` 输出合法 JSON Schema；非法枚举被拒
- [ ] **S0-04** 定义 `ValidationReport` / `Constraint` 模型 · P0
  - 验收：`E_*` 错误码枚举与大纲 §7.4 一致
- [ ] **S0-05** 轨迹记录器 `observability/tracer.py`（contextvar、JSONL、脱敏）· P0
  - 验收：嵌套 span 父子关系正确；`sk-` 开头字符串被脱敏
- [ ] **S0-06** CLI 骨架 `cli.py`（`schema` / `ingest` / `search` / `ask` / `eval` 占位）· P1
  - 验收：`gcc --help` 可用，未知子命令有友好提示
- [ ] **S0-07** 测试与规范骨架（`tests/`、`conftest.py`、`make test`）· P0
  - 验收：`pytest` 全绿（允许仅 schema 测试）
- [ ] **S0-08** LLMClient Protocol + DeepSeek adapter（可替换内部模型）· P0
  - 验收：DeepSeek 可调通；切 `openai_compatible` + `base_url` 指向 mock 服务时业务代码零改动；`llm/` 外不出现厂商 SDK import

### S1 知识库与混合检索（3–5 天）

- [ ] **S1-01** 编写 Wiki 语料 3 篇（世界观/势力、NPC 档案、任务设计规范）· P0
  - 验收：每篇含唯一实体 ID；字数 800–1500；可被标题切分
- [ ] **S1-02** 编写历史任务配置 10 条（主线 3 / 支线 4 / 日常 3）· P0
  - 验收：全部通过 `TaskDraft` Schema；覆盖有分支/多目标/多奖励
- [ ] **S1-03** 编写约束表 17 条（YAML DSL，含 `rationale`；含金币/阅历奖励下限各 1 条）· P0
  - 验收：加载器能解析；每条有唯一 ID
- [ ] **S1-04** JSON 感知切分器 `rag/chunker.py`（父块回链、元数据抽取）· P0
  - 验收：任务被拆为父块 + 子块；子块可反查 `task_id`
- [ ] **S1-05** 索引构建 `rag/index.py`、`rag/store.py`、`rag/stores/chroma.py`（VectorStore Protocol + Chroma adapter + BM25 持久化）· P0
  - 验收：重建索引 ≤ 5 分钟；重复构建幂等；payload 映射只在 adapter 内；切换存储实现只需新增 adapter，业务代码零改动
- [ ] **S1-06** 混合检索 `rag/retriever.py`、`rag/fusion.py`（filters 中性化 + 自研 RRF k=60 + top-k）· P0
  - 验收：等级段过滤生效；返回带来源与分数；`rag/fusion.py` 的 RRF 可单测；`agents/` 与 `rag/retriever.py` 不出现库特有语法
- [ ] **S1-07** 检索标注 20 条 + `retrieval_eval.py`（Recall@5 / MRR）· P0
  - 验收：输出基线报告 `reports/retrieval-baseline.json`
- [ ] **S1-08** CLI `search` 子命令 · P1
  - 验收：终端输出片段、分数、来源路径

### S2 QueryAgent 与只读闭环（2–3 天）

- [ ] **S2-01** Supervisor 路由节点（规则 + 小模型，输出 query/task/other）· P0
  - 验收：20 条样例意图判定准确率 ≥ 90%
- [ ] **S2-02** QueryAgent 与只读工具集（`search_wiki` / `find_similar_tasks` / `list_constraints`）· P0
  - 验收：工具列表不含写工具；越权工具不可见
- [ ] **S2-03** 引用约束 Prompt + 无依据拒答逻辑 · P0
  - 验收：知识库外问题回答"未检索到"，不编造
- [ ] **S2-04** SSE API（`/sessions`、`/messages`）· P0
  - 验收：事件顺序符合架构文档 §10.2；断流有 `error`
- [ ] **S2-05** 会话管理（创建、恢复、超时）· P1
  - 验收：同一 session 多轮上下文连续
- [ ] **S2-06** 只读闭环 e2e 测试 3 条 · P0
  - 验收：回答含引用且引用可解析

### S3 结构化草案生成（3–4 天）

- [ ] **S3-01** 上下文拼装 `context_builder.py`（6000 token 预算、分区块、截断记录）· P0
  - 验收：超预算时按优先级截断并发出 `warning`
- [ ] **S3-02** 结构化生成（经 `LLMClient.structured` 生成 + 解析失败重试 1 次）· P0
  - 验收：10 次生成至少 9 次返回可解析 JSON
- [ ] **S3-03** `draft` 节点接入 LangGraph（含 `provenance.evidence`）· P0
  - 验收：图可编译运行；草案带证据引用
- [ ] **S3-04** 黄金需求 v1（10 条：正常 7 / 边界 3）· P0
  - 验收：每条有期望字段断言
- [ ] **S3-05** Schema 层首次通过率基线 · P0
  - 验收：`reports/fpvr-baseline.json` 有数字与样本明细

### S4 校验管线与自动修复（3–4 天）

- [ ] **S4-01** 规则引擎（`Rule` 协议、注册表、YAML 加载、消息模板）· P0
  - 验收：新增规则类型 ≤ 30 行且自动注册
- [ ] **S4-02** Schema 规则（必填/枚举/正则/序号连续/ID 格式）· P0
  - 验收：每条规则 3 个用例（通过/越界/边界）
- [ ] **S4-03** 业务规则（奖励区间、等级区间、对白长度、区域合法）· P0
  - 验收：`constraint_ref` 能指回 YAML 条目
- [ ] **S4-04** 冲突检测（ID 唯一、引用存在、依赖成环）· P0
  - 验收：成环样例返回 `E_DEP_CYCLE`
- [ ] **S4-05** 修复节点（错误回灌、≤2 轮、超限 `rejected`）· P0
  - 验收：修复后仍失败不写入；轮次记录进轨迹
- [ ] **S4-06** 规则单测覆盖（≥85%）· P0
  - 验收：`pytest --cov` 达标
- [ ] **S4-07** 对抗草案集（越界/重复/缺失引用/成环/注入）· P0
  - 验收：5 条全部拦截，错误码正确

### S5 MCP 工具层与安全写入（3–5 天）

- [ ] **S5-01** MCP Server 骨架（stdio、按 mode 注册工具）· P0
  - 验收：只读模式下工具列表无写工具
- [ ] **S5-02** 只读工具实现（`get_task_config` / `list_recent_tasks` / `list_constraints`）· P0
  - 验收：返回结构符合契约；不存在时返回结构化错误
- [ ] **S5-03** `validate_draft` 工具（内部复用规则引擎）· P0
  - 验收：与本地调用结果一致
- [ ] **S5-04** `preview_draft`（unified diff + fingerprint）· P0
  - 验收：指纹对相同输入稳定、对改动敏感
- [ ] **S5-05** 写入器（路径守卫、文件锁、临时文件、`os.replace`、备份、审计）· P0
  - 验收：中断测试后目标文件完整；越权路径 `E_PATH_DENIED`
- [ ] **S5-06** `commit_draft`（指纹 + revision + 强制复检）· P0
  - 验收：指纹不符/版本过期均拒绝；成功后备份文件存在
- [ ] **S5-07** API 确认流（`commit_token` 签发、校验、过期）· P0
  - 验收：token 过期或跨草案使用被拒
- [ ] **S5-08** 破坏性测试（并发写、进程中断、只读模式、软链逃逸）· P0
  - 验收：并发下后者 `E_STALE_REVISION`；无半写文件
- [ ] **S5-09** `rollback_commit`（备份恢复 + 新提交记录）· P1
  - 验收：回滚后文件与备份一致，历史不删除

### S6 分层记忆（2–3 天）

- [ ] **S6-01** 会话 checkpointer（SQLite）接入 LangGraph · P0
  - 验收：进程重启后同 session 可继续
- [ ] **S6-02** 偏好存储（接口 + 本地 JSON 实现，Redis 可插拔）· P0
  - 验收：`memory show` 可读；可清除
- [ ] **S6-03** 偏好抽取（滑动平均 α=0.3，异常样本不更新）· P1
  - 验收：3 次提交后偏好带收窄且可解释
- [ ] **S6-04** 偏好注入 `context_builder`（≤300 token，结构化）· P0
  - 验收：第二次生成奖励落入偏好带
- [ ] **S6-05** 记忆测试（不覆盖显式指令、TTL、并发更新）· P1
  - 验收：显式"给 500"优先于偏好

### S7 评测与可观测（3–4 天）

- [ ] **S7-01** 黄金需求 v2（30 条：正常 20 / 边界 5 / 对抗 5）· P0
  - 验收：每条含 `expected` 断言；对抗含 2 条提示注入
- [ ] **S7-02** Ragas 评测（faithfulness / answer_relevancy / context_*）· P0
  - 验收：固定 judge 模型与版本；结果可复现
- [ ] **S7-03** Agent 指标（FPVR、修复轮次、拦截率、P95、成本）· P0
  - 验收：报告含分母与失败样本分组
- [ ] **S7-04** 检索指标纳入统一入口 · P0
  - 验收：`gcc eval --suite all` 一次跑完
- [ ] **S7-05** 轨迹补全（tokens/cost/工具入参摘要）· P0
  - 验收：单条 trace 可还原完整决策链
- [ ] **S7-06** Streamlit 面板（列表/详情/指标）· P1
  - 验收：能按 trace_id 搜索并查看时间轴
- [ ] **S7-07** 回归对比 `compare.py` + 阈值告警 · P0
  - 验收：FPVR −3pt 触发失败退出码
- [ ] **S7-08** 基线报告入库 `docs/reports/` · P1
  - 验收：报告含"改动 → 指标 → 结论"

### S8 演示打磨与交付（2–3 天）

- [ ] **S8-01** Docker Compose（api + mcp + 可选 qdrant/redis）· P1
  - 验收：`docker compose up` 后 healthz 正常
- [ ] **S8-02** 演示脚本排练（含反例与断网兜底）· P0
  - 验收：全程无手工改文件救场
- [ ] **S8-03** 5 分钟录屏 · P1
  - 验收：画面清晰、字幕/旁白完整
- [ ] **S8-04** README 更新（真实命令、指标、截图、架构图）· P0
  - 验收：新人按 README 能跑起来
- [ ] **S8-05** 简历 bullet + 问答数字校准 · P0
  - 验收：所有数字可在报告中找到出处
- [ ] **S8-06** 代码清理（TODO、死代码、错误码统一）· P1
  - 验收：`ruff` 无警告

---

## 2. 资源与前置条件

| 项 | 状态 | 说明 |
| --- | --- | --- |
| LLM API Key（DeepSeek 默认；内部模型经 `openai_compatible`） | ⬜ 待申请 | 本地演示与批量评测需要；密钥只进 `.env` |
| Embedding 模型（BGE-M3） | ⬜ 待下载 | 约 2 GB；或改用云 API |
| 向量库（Chroma / Qdrant） | ⬜ 待安装 | Chroma 可 pip 安装，零部署 |
| Redis（可选） | ⬜ 待决定 | 仅偏好记忆需要，可用本地 JSON 替代 |
| Docker Desktop | ⬜ 待确认 | S8 兜底环境需要 |
| 《星港纪元》设定文档 | ⬜ 待写 | S1-01 的前置 |
| 演示设备与录屏工具 | ⬜ 待准备 | S8 |

> 注：本机当前未安装 Git，仓库已手工初始化 `.git` 骨架；安装 Git 后执行 `git status` 校验，首次提交建议 `chore: init project docs`。

---

## 3. 风险登记（执行期跟踪）

| # | 风险 | 触发信号 | 应对 | 状态 |
| --- | --- | --- | --- | --- |
| R1 | 数据准备超时 | S1-01 超过 2 天 | 先写 1 篇 Wiki + 4 条任务跑通链路，再补量 | 监控中 |
| R2 | 结构化输出不稳 | 解析失败率 > 20% | 缩小 Schema、拆分字段、加 few-shot、改用支持 JSON mode 的模型 | 监控中 |
| R3 | 检索召回不达标 | Recall@5 < 0.7 | 先查切分与标注，再考虑 reranker / query 改写 | 监控中 |
| R4 | Ragas 不稳定 | 两次同数据波动 > 10% | 固定 judge 版本与 Prompt；只做相对比较 | 监控中 |
| R5 | MCP SDK 变动 | 升级后工具注册失败 | 锁定版本；适配层隔离 | 监控中 |
| R6 | 进度落后 | 单阶段超预估 50% | 按 §9 取舍顺序砍功能，保主链路 | 监控中 |
| R7 | 范围蔓延 | 清单外需求 > 3 条 | 进候选池，评审后再排期 | 监控中 |

---

## 4. 候选池（未排期）

| # | 想法 | 价值 | 成本 | 备注 |
| --- | --- | --- | --- | --- |
| C1 | 批量生成多条任务 | 中 | 中 | 接口预留，MVP 不做 |
| C2 | 对白生成与语气一致性检查 | 高 | 中 | 依赖更多语料 |
| C3 | 数值模拟（奖励对经济曲线的影响） | 高 | 高 | 超出 MVP |
| C4 | 接入真实项目管理工具（Jira/飞书） | 中 | 中 | 演示价值有限 |
| C5 | 多人协作与审批流 | 低 | 高 | 非目标 |
| C6 | 自动生成任务流程图 | 中 | 低 | 可做演示加分项 |

---

## 5. 变更记录

| 版本 | 日期 | 变更 | 原因 |
| --- | --- | --- | --- |
| v0.1 | 2026-11-12 | 初始基线：8 阶段 / 61 项任务（原记 52 项，v0.2 勘误） | 立项 |
| v0.2 | 2026-11-13 | 新增检索层 VectorStore 抽象 + 误记勘误：S1-05/S1-06 补「切换 adapter 业务零改动」「RRF 可单测」；变更记录 52→61 | 锁定「先 Chroma、S7 迁 Qdrant 只换 adapter」；关闭待拍板项 |
| v0.3 | 2026-11-13 | S0 新增 S0-08「LLMClient Protocol + DeepSeek adapter（可替换内部模型）」P0；S3-02 改为经 `LLMClient.structured` 生成；§2 资源行同步 DeepSeek 默认；变更记录 61→62，共 62 项 | 用户新决策：先用 DeepSeek + 保留可替换接口（ADR-008），换内部模型业务层零改动 |
