# GameConfig Copilot · 基线报告（S7-08）

| 项 | 值 |
| --- | --- |
| 文档版本 | v2（2026-11-18 真实 LLM 重跑） |
| 生成时间 | 2026-11-18 |
| 对应任务 | S7-02 Ragas / S7-03 Agent 指标 / S7-05 轨迹审计 / S7-07 回归对比 / S7-08 基线入库 |
| 数据来源 | `data/golden/requests_v2.jsonl`（30 条）、`reports/fpvr-baseline.json`、`reports/observability-summary.json`、`reports/retrieval-baseline.json` |
| 状态 | **检索 + Agent 已真实测量**；Ragas 仍待安装 |
| 机器可读 | `docs/reports/baseline.json`（`compare.py` 默认基线，含 retrieval + agent 两个 suite） |

> **2026-11-18 更新：真实 DeepSeek 跑完 30 条 v2 黄金需求，FPVR = 91.30%（21/23）。**
> 历史 S7-08 阶段的 **BM25-only** 检索口径、**mock 生成 / 待真实 LLM** 标注仍保留在
> §1、§3、§7，用于解释早期数字的来源；Agent 数字以 §2 为准。
>
> 本轮同时落地了奖励下限：`C_REWARD_GOLD_MIN_01` / `C_REWARD_EXP_MIN_01`，
> 黄金集的金币/阅历下限断言恢复为硬断言（详见 §2.2）。

## 1. 检索基线（已测，BM25-only）

| 指标 | 值 | 口径 |
| --- | --- | --- |
| Recall@1 | 0.3750 | 相关块 ∩ top-1 / 相关块，20 条 query 宏平均 |
| Recall@5 | **0.7750** | 相关块 ∩ top-5 / 相关块，20 条 query 宏平均 |
| Recall@10 | **0.8750** | 相关块 ∩ top-10 / 相关块，20 条 query 宏平均 |
| hit@5 | 0.9500 | top-5 至少命中一个相关块的 query 比例 |
| MRR@10 | **0.7625** | 首个相关块排名倒数，前 10 未命中记 0 |
| nDCG@10 | 0.7382 | 分级增益 `2^rel-1`，ideal 排序取该 query 全部相关块 |
| source_type_precision@10 | 0.7300 | top-10 中来源类型与标注一致的占比 |

- qrels：`data/golden/qrels.json` v2，20 条 query / 34 个相关标注 / 14 条多标注 query。
- 检索配置：`top_k=10`、`candidate_k=30`、RRF `fusion_k=60`；索引由 `gcc ingest` 写入 `out/index`。
- 2026-11-18 重建索引后为 **81 chunks（constraint 17 / task 40 / wiki 24）**。
- 复现：`gcc eval --suite retrieval` → `reports/retrieval-baseline.json`。

## 2. Agent 指标（已测，2026-11-18 真实 LLM）

本轮运行：`deepseek/deepseek-chat`，30 条 `requests_v2`（20 normal / 5 boundary / 5 adversarial），
真实 `TaskAgent` + `out/index` 检索 + S4 规则引擎 + 真实 `TaskAgent.repair`（最多 2 轮）。
原始报告：`reports/fpvr-baseline.json`（`generated_at=2026-11-18T18:57:49+08:00`）。

| 指标 | 目标（README §7） | 实测 2026-11-18 | 口径 / 说明 |
| --- | --- | --- | --- |
| FPVR（首次通过率） | ≥ 80% | **91.30%（21/23）** | 首轮同时通过 Schema + validator + 黄金硬断言 |
| 修复后通过率 | — | **100%（23/23）** | 2 条首轮失败均在 ≤2 轮内修复 |
| 平均修复轮次 | ≤ 1.2 | **0.13** | 21 条 0 轮、1 条 1 轮、1 条 2 轮 |
| 危险写入有效拦截率 | 100% | **100%（5/5）** | 对抗子集 `effective_security_rate`（代码命中 ∪ 攻击未采纳 ∪ 结构保证） |
| 期望拦截样本错误码命中率 | — | 42.86%（3/7） | 7 条 block 样本中 G10/G25/G29 命中期望错误码 |
| 对抗子集代码命中率 | — | 20%（1/5） | G29 命中 `E_REF_MISSING`；G26/G27/G28/G30 靠「未采纳/结构不可能」兜底 |
| P95 端到端延迟 | ≤ 15s | **4083 ms** | 逐样本 `duration_ms` nearest-rank；trace 端到端 P95 为 2049 ms |
| 单次成本 | 待定 | **¥0.016232 / 样本** | 30 样本摊销；34 次 LLM 调用合计 ¥0.486968；按 LLM trace 摊为 ¥0.014323 |
| 首轮 Schema 通过率 | — | 100% | 30/30 |
| 整轮耗时 | — | 63.8 s | 含生成 + 2 条修复 |

子集：

| 子集 | 条数 | FPVR | 拦截率 | 说明 |
| --- | --- | --- | --- | --- |
| normal | 20 | 90.00%（18/20） | — | 失败集中在引用缺失，不在奖励下限 |
| boundary | 5 | 100%（3/3） | 100%（2/2） | 上限/等级边界全部通过 |
| adversarial | 5 | — | 20%（1/5 代码命中） | 有效拦截 100%（5/5） |

首轮失败样本（均非奖励下限问题）：

| 样本 | 首轮结果 | 失败原因 | 修复 |
| --- | --- | --- | --- |
| G16 | `assertion_failure` | 缺前置引用 `T_MAIN_0102`（validator 通过，黄金断言未满足） | 1 轮修复通过 |
| G21 | `validation_error` | `E_REF_MISSING` + `E_SCHEMA_002`（引用/字段类型问题，奖励 150/1050 在区间内） | 2 轮修复通过 |

### 2.1 前后对比（奖励下限上线）

| 项 | 2026-11-18 首轮（仅上限） | 2026-11-18 重跑（上限 + 下限） |
| --- | --- | --- |
| FPVR | 86.96%（20/23） | **91.30%（21/23）** |
| 首轮失败 | 3 条（G16 引用、G21/G23 奖励超上限） | 2 条（G16 引用、G21 引用/类型） |
| G17（22–27 级日常） | gold 350 / exp 2400 | **gold 420 / exp 3200**（下限 360/2880） |
| G18（14–19 级支线） | gold 150 / exp 1000 | **gold 320 / exp 2400**（下限 150/1200） |
| G23（10–15 级支线） | gold 320 / exp 2200（超上限） | **gold 160 / exp 1200**（区间 60–200 / 480–1600） |

结论：下限规则 + 区间注入后，低奖励失败（G17/G18）与超上限失败（G21/G23 的奖励部分）都消失；
首轮剩余失败是引用/字段类型问题，与奖励区间无关。FPVR 已达 ≥80% 目标，本轮未触发「加样例后重跑」。

### 2.2 下限约束已加（2026-11-18）

- **约束语料**：新增 `C_REWARD_GOLD_MIN_01`、`C_REWARD_EXP_MIN_01`，`type=level_band_range`，
  每个 band 同时给 `min_amount` / `max_amount`；下限 = 同档上限的 30%：
  - 金币：Lv≤10 `[60, 200]`、Lv≤20 `[150, 500]`、Lv≤30 `[360, 1200]`
  - 阅历：Lv≤10 `[480, 1600]`、Lv≤20 `[1200, 4000]`、Lv≤30 `[2880, 9600]`
  - `rationale` 写明经济节奏：防止奖励过低破坏成长曲线；低于下限与超上限同样报 `E_REWARD_RANGE`。
- **规则引擎**：`ConstraintType.LEVEL_BAND_RANGE` + `LevelBandRangeRule`；按 `level_range.min` 取档，
  双边界判定，`suggestion` 取最近边界（低于取下限、高于取上限），`constraint_ref` 指回约束。
- **Prompt/上下文**：`ContextBuilder` 从约束 chunk 的 `bands` 元数据动态生成
  `reward-interval-gold` / `reward-interval-exp` 行（按需求等级取档），置于约束区块最前；
  `prompts/task.md` 明确「金币/阅历必须落在 [min,max]、不允许只满足上限」。
  观测到的总上下文 2263–3371 tokens，约束区块 ≤1033 tokens，仍守住 6000 总预算。
- **黄金集**：`gold_between` / `exp_between` 的下限恢复为硬断言（validator 已可执行）；
  NPC 精确值、目标条数、等级 min/max 精确值仍为 advisory，不改变 FPVR。

## 3. Ragas（未安装，未测）

- `ragas` / `langchain-openai` 未安装：`ragas_eval.run_ragas_eval(...)` 抛
  `RagasUnavailable`，**不写 `reports/ragas-baseline.json`**，不伪造分数。
- judge 固定配置：`deepseek-chat@2026-11-01`（`GCC_RAGAS_JUDGE_MODEL` +
  `GCC_RAGAS_JUDGE_VERSION`；浮动版本如 `latest` 直接拒绝）。
- 指标：`faithfulness` / `answer_relevancy` / `context_precision` / `context_recall`；
  每条指标单独记录分母（无 context / 无 ground truth 的样本不参与该指标）。
- 安装后复现：`pip install 'ragas>=0.2' langchain-openai`，设置 `GCC_LLM_API_KEY` 后调用
  `run_ragas_eval(samples=...)`。

## 4. 轨迹与成本（S7-05 审计快照）

2026-11-18 本轮轨迹：`traces/fpvr-2026-11-18.jsonl`（gitignored；`GCC_TRACE_PATH` 指定）。

| 项 | 值 |
| --- | --- |
| JSONL 行数 / 可解析 | 133 / 133 |
| `llm.*` span | 34 |
| 带 LLM 调用的 trace | 34 |
| 有 `cost_cny` 的调用 | 34（`unpriced_calls=0`） |
| 总 token | 205,909（prompt + completion） |
| 总成本 | **¥0.486968** |
| 单次成本（按 LLM trace） | **¥0.014323** |
| 单次成本（按 30 条黄金样本） | **¥0.016232** |
| LLM span P95 | 2448 ms；trace 端到端 P95 2049 ms |

复现：

```bash
python -m gameconfig_copilot.evaluation.trace_audit --path traces/fpvr-2026-11-18.jsonl
```

聚合入口（报告 + 轨迹合并）：

```bash
python -c "from gameconfig_copilot.evaluation.agent_eval import run_observability_summary; print(run_observability_summary().message)"
```

## 5. 回归门（S7-07）

`compare.py` 与 `docs/reports/baseline.json` 对比，阈值（含等于）触发失败退出码 1：

| 指标 | 阈值 | 行为 |
| --- | --- | --- |
| FPVR | −3pt | baseline − current ≥ 0.03 → FAIL |
| Recall@5 | −2pt | baseline − current ≥ 0.02 → FAIL |
| 单次成本 | +20% | current / baseline − 1 ≥ 0.20 → FAIL |

`docs/reports/baseline.json` 已升级为 v2：retrieval suite 保留原数字，新增 agent suite
（FPVR 0.913、post-repair 1.0、P95 4083 ms、单次成本 0.016232 CNY），FPVR 与成本不再被跳过。

## 6. 测试基线（2026-11-18）

| 项 | 值 |
| --- | --- |
| 全量 pytest | **672 passed / 1 skipped**（`pytest -q -p no:cacheprovider`） |
| 跳过项 | 1 条 fastmcp 用例（本机 Python 3.10，无 fastmcp） |
| Lint | `ruff check src tests` → **All checks passed** |
| 历史对照 | S8 基线 547 passed / 1 skipped；交付时 651 passed / 1 skipped |
| 运行方式 | `pytest -p no:cacheprovider`（CI/本地一致） |

## 7. 下一步

1. 安装 ragas 后产出 `reports/ragas-baseline.json`（judge 固定 `deepseek-chat@2026-11-01`）；
2. 接 dense adapter（BGE-M3 / Qdrant），用同一份 qrels 重跑并对比 Recall@5 0.775；
3. 把 `gcc eval --suite agent` 接到 `run_agent_eval`，让 FPVR/修复轮次/P95/成本可一条命令复现；
4. 每次影响检索/生成/校验的改动执行 `compare.py`，把 diff 表追加到 `docs/reports/`；
5. 继续收敛 G16/G21 这类引用/字段类型失败（与奖励下限无关）。
