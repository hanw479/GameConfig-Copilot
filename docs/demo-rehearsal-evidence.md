# Live 排练实测证据 · 用脚本原措辞跑（2026-11-19 16:14）

> 任务：`tsk_3ac8fbc837`（Live 排练，真实 API + 真实模型 + write 模式，**不录屏**）。
> 配套文件：[demo-rehearsal.md](demo-rehearsal.md)（录制 runbook：旁白、检查清单、兜底话术，本文不重复）。
> 本文只记**可复现的实测事实**：真实 SSE 与到达时间、耗时/token/成本、落盘与审计、span 树、零写入证明，以及**录制前必须改的两处口径**。

**本文与 runbook 的差异一句话**：runbook 为了绕开模型自纠正，把反例需求改成了「给新手村 1 级任务加 1000 金币奖励」；本文证明**`docs/demo-script.md` 的原文措辞（6 级支线 + 1000 金币）在 live 下会写入成功**，并给出保留原文的最小修法（加一句限定语，3/3 复现）。

---

## 1. 结论速览

| 幕 | 需求（脚本原文） | 结果 | 生成耗时 | 确认耗时 | 关键值 |
| --- | --- | --- | --- | --- | --- |
| 1 正常生成 | `…6 级支线，去矿洞捡 4 块矿石，奖励别太高` | `committed` | 2738 ms | 60 ms | `T_SIDE_0217`、金币 **90**、`fa6b771b760b`、diff +67 行、`backup=null` |
| 2 改奖励 150 | `…奖励 150 金币` | `committed` | 3070 ms | 30 ms | `T_SIDE_0218`、金币 **150**、`ee06a7d377ca` |
| 3 反例（原文） | `…奖励 1000 金币` | ⚠️ **`committed`**（模型自压到 200） | 2116 ms | 31 ms | `T_SIDE_0219`、金币 **200** |
| 3 反例（加限定语） | `…奖励 1000 金币，禁止修改我给的数值` | ✅ `rejected`、零写入 | 2034 ms | 31 ms | `E_REWARD_RANGE` ×2、`checked={50,9,6}` |

- 模型：`deepseek-chat`；3 次生成合计约 **¥0.042**（单次 ¥0.0139 / ¥0.0141 / ¥0.0143，取自 trace 的 `llm.structured` span）。
- 环境：Python 3.10.2、`out/index` 81 chunks（constraint 17 / task 40 / wiki 24）、语料 10 个任务（最大 `T_SIDE_0216`）。
- 排练用端口 8231、写入根重定向到 `data/{tasks,constraints}` 的逐字节副本（原因同 runbook §9.2 的 `data/` 权限坑）；SSE 里的 `target_path`、diff 头、审计 `target_path` 仍是 `data/tasks/...` 相对路径，**画面上与正式录制一致**。

---

## 2. 幕 1 · 正常生成

需求原文：`给铁匠老陈加一条 6 级支线，去矿洞捡 4 块矿石，奖励别太高`

### 2.1 事件到达时间（本机实测，用来卡台词节奏）

| 事件 | 到达 | 备注 |
| --- | --- | --- |
| `meta` | 1 ms | `trace_id` 立刻可见 |
| `status(routing)` | 183 ms | 含首次图构建（约 190 ms，仅第一次） |
| `status(gathering_context)` / `evidence` | 188 / 191 ms | 检索 4 ms，7 条证据 |
| `status(drafting)` → `draft` | **2734 ms** | 2.5 s 屏幕静止，只有「正在生成草案」 |
| `validation` | 2735 ms | 0 ms 完成 |
| `awaiting_confirmation` | 2737 ms | diff 67 行 |
| `status(awaiting_confirmation)` / `done` | 2738 ms | — |
| 确认段 `committed` | +60 ms | 同秒完成 |

### 2.2 关键帧（真实输出，diff 与 evidence 截断）

```text
event: evidence
data: {"items":[
  {"source":"wiki:npc_profiles#六、NPC 索引","source_type":"wiki","score":0.016393,...},
  {"source":"wiki:npc_profiles#三、NPC_013 铁匠 老陈","source_type":"wiki",...},
  {"source":"wiki:world_overview#五、实体索引","source_type":"wiki",...},
  {"source":"wiki:quest_design_spec#四、ID 命名规则","source_type":"wiki",...},
  {"source":"task:T_SIDE_0213#task","source_type":"task",...},
  {"source":"task:T_SIDE_0213#objectives","source_type":"task",...},
  {"source":"task:T_DAILY_0301#task","source_type":"task",...}]}

event: draft
data: {"draft":{"schema_version":1,"task_id":"T_SIDE_0217","title":"铁匠的委托：矿洞铁尘",
  "task_type":"side","level_range":{"min":6,"max":9},
  "giver":{"npc_id":"NPC_013","dialogue_ref":"DLG_013_03"},
  "prerequisites":[{"type":"task_completed","ref":"T_MAIN_0102"}],
  "objectives":[{"seq":1,"type":"talk_to","target":"NPC_013","count":1,"area":"MAP_STARPORT_01"},
                {"seq":2,"type":"collect","target":"ITEM_ORE_02","count":4,"area":"MAP_MINE_03"}],
  "rewards":[{"type":"gold","amount":90,"item_id":null},{"type":"exp","amount":600,"item_id":null}],
  "branches":[],"tags":["side","chapter01","npc_blacksmith","mine"],
  "provenance":{"model":"deepseek-chat","created_at":"2026-11-19T16:14:24.603000+08:00",...}},
  "version":1}

event: validation   data: {"ok":true,"errors":[],"warnings":[],
                           "checked":{"schema":1,"business":0,"conflict":0}}
event: awaiting_confirmation
data: {"draft_id":"dr_12766bf8afa1","commit_token":"ct_****","expires_in":600,
       "target_path":"data/tasks/T_SIDE_0217.json",
       "diff":{"unified_diff":"--- a/data/tasks/T_SIDE_0217.json\n+++ b/data/tasks/T_SIDE_0217.json\n@@ -0,0 +1,67 @@\n+{...}",
               "stats":{"added":67,"removed":0}}}
event: done         data: {"status":"awaiting_confirmation","elapsed_ms":2736,"tokens":2983,"cost_cny":0.0}
```

确认后：

```text
event: status       data: {"stage":"confirmed","label":"已确认，准备写入"}
event: status       data: {"stage":"committed","label":"写入成功"}
event: committed    data: {"commit_id":"fa6b771b760b","backup":null,"revision":"9a3586cc075f"}
event: done         data: {"status":"committed","elapsed_ms":57,"tokens":2983,"cost_cny":0.0}
```

### 2.3 落盘与审计（真实审计行）

```json
{"author":"u_1024","backup_path":null,"base_revision":"e3b0c44298fc","commit_id":"fa6b771b760b",
 "diff_stats":{"added":67,"removed":0},"event":"commit","new_revision":"9a3586cc075f","note":"demo",
 "schema_version":1,"status":"committed","target_path":"data/tasks/T_SIDE_0217.json",
 "task_id":"T_SIDE_0217","ts":"2026-11-19T08:14:24.659+00:00"}
```

写入文件 67 行，`rewards[0].amount=90`、`rewards[1].amount=600`；`provenance.request` 原样保存了策划原话，`provenance.evidence` 只记被引用的 5 条证据。

> 需求里带「别太高」时模型给 **90**；按脚本原文（不带「别太高」）实测是 **120**（runbook §5.2 也按 120 写）。台词别把数字说死。

---

## 3. 幕 2 · 奖励 150

需求原文：`给铁匠老陈加一条 6 级支线，去矿洞捡 4 块矿石，奖励 150 金币`

| 项 | 实测 |
| --- | --- |
| 事件序列 | 与幕 1 完全相同（12 帧生成 + 4 帧确认） |
| 到达节奏 | `evidence` 8 ms → `draft` **3065 ms** → `done` 3070 ms |
| `draft.task_id` | `T_SIDE_0218`（**新建**，不是修改 0217） |
| `draft.rewards` | `[{"gold":150},{"exp":1050}]` |
| `evidence` | `wiki:quest_design_spec#二、奖励区间与等级约束` 首次进入前 5（因为需求提到具体金额） |
| 确认 | 30 ms，`commit_id=ee06a7d377ca`，`revision=7858a0cb96ba`，`backup:null` |

> 因为每一轮都只用 `new_state(message)` 单轮启动（`docs/demo-script.md` §7 偏差 1），**必须重述完整需求**；说「改成 150」而不重述，意图会缺 NPC/地点/数量。runbook §8.5 用的 `…奖励改成 150 金币` 也是完整句，同样可行。

---

## 4. 幕 3 · 反例：脚本原文会**写入成功**（本文最重要的一段）

### 4.1 复现：用脚本文案，闸门没有被触发

需求：`给铁匠老陈加一条 6 级支线，去矿洞捡 4 块矿石，奖励 1000 金币`（= `docs/demo-script.md` 幕 4 原文）

```text
gold=200   validation.ok=True   → awaiting_confirmation → committed  ← 真的写进了 T_SIDE_0219
```

原因：这条需求检索到了 `wiki:quest_design_spec#二、奖励区间与等级约束`（正文含「Lv≤10 不超过 200」），模型据此**主动把 1000 改成 200**。草案合规 → 预览 → 确认 → 落盘，`E_REWARD_RANGE` 一次都没出现。

**后果**：按脚本原文录制，反例幕会变成「它自己就守规矩了」，而且屏幕上会多写一个 200 金币的任务；`data/` 从零写入变成 +1 文件。

### 4.2 修法：加一句限定语，3/3 稳定

4 组措辞探针（同一服务、真实模型、每轮新会话）：

| 措辞 | 产出金币 | 结果 |
| --- | --- | --- |
| `…奖励 1000 金币` | 200 | ❌ 自纠正，提交成功 |
| `…金币奖励固定 1000，必须原样写入，不要按约束调整` | 1000 | ✅ 预览 → 确认被拒 |
| `…奖励 1000 金币，禁止修改我给的数值` | **1000 / 1000 / 1000** | ✅ **推荐**，3 次全中 |
| `…奖励 9999 金币` | 9999 | ✅ 被拦，但台词不好念 |

### 4.3 生成段（真实，2034 ms）

```text
event: draft        data: {"draft":{"task_id":"T_SIDE_0224",...,"rewards":[{"type":"gold","amount":1000,...},
                                                          {"type":"exp","amount":800,...}],...}}
event: validation   data: {"ok":true,"errors":[],"warnings":[],"checked":{"schema":1,"business":0,"conflict":0}}
event: awaiting_confirmation data: {"draft_id":"dr_14c99013b129","target_path":"data/tasks/T_SIDE_0224.json",
                                    "diff":{"stats":{"added":66,"removed":0}},"expires_in":600}
event: done         data: {"status":"awaiting_confirmation","elapsed_ms":2033,"tokens":3104,"cost_cny":0.0}
```

> **草案阶段 `ok:true` 是对的**：这一层只有 Schema。屏幕上「预览成功」和「等会儿被拒」不矛盾，别提前说「它已经拒绝了」。

### 4.4 确认段（真实，31 ms）—— 录制时要停 3 秒的那一屏

```text
event: status       data: {"stage":"confirmed","label":"已确认，准备写入"}
event: status       data: {"stage":"rejected","label":"已拒绝"}
event: validation   data: {"ok":false,
  "errors":[
    {"code":"E_REWARD_RANGE","severity":"error","field":"rewards[0].amount",
     "message":"Lv6 的任务金币奖励不得超过 200","constraint_ref":"C_REWARD_GOLD_01",
     "suggestion":{"field":"rewards[0].amount","value":200}},
    {"code":"E_REWARD_RANGE","severity":"error","field":"rewards[0].amount",
     "message":"Lv6 的任务金币奖励必须落在 60–200 之间","constraint_ref":"C_REWARD_GOLD_MIN_01",
     "suggestion":{"field":"rewards[0].amount","value":200}}],
  "warnings":[{"code":"W_DIALOGUE_ASSIGN_CONFLICT","severity":"warning","field":"giver.dialogue_ref",
               "message":"对白 DLG_013_02 已绑定任务 T_SIDE_0213","constraint_ref":"C_DIALOGUE_ASSIGN_UNIQUE_01"}],
  "checked":{"schema":50,"business":9,"conflict":6}}
event: error        data: {"code":"E_REWARD_RANGE","message":"提交前复检未通过，草案被拒绝，未写入任何文件。",
                           "stage":"rejected"}
event: done         data: {"status":"rejected","elapsed_ms":27,"tokens":3104,"cost_cny":0.0}
```

实测零写入：`data/tasks/T_SIDE_0224.json` 未创建、`commits.jsonl` 无该 `task_id`、`data/.locks` 无残留锁与 `.tmp-*`。

> 业务计数是 **9**（`docs/demo-script.md` 写 7，约束文件已增至 17 条）。另有一条 `W_DIALOGUE_ASSIGN_CONFLICT` 警告：**warning 不阻断**，顺口说一句是加分项。

---

## 5. 轨迹（幕 4）实测 span 树

生成 trace（`api.messages`，11 个 span；**按结束时间落盘，子 span 在父 span 之前**）：

| span | 状态 | 耗时 | 关键 attrs |
| --- | --- | --- | --- |
| `route` | ok | 0 ms | `agent.node=route`, `stage=routing` |
| `tool.search_wiki` / `tool.find_similar_tasks` / `tool.list_constraints` | ok | 0 ms ×3 | `ok=true` |
| `retrieval` | ok | 4 ms | `agent.node=gather_context` |
| `llm.structured` | ok | **2542 ms** | `provider=deepseek`, `model=deepseek-chat`, `schema=TaskDraft`, `prompt_tokens=5743`, `completion_tokens=304`, `cost_cny=0.013918` |
| `agent.draft` | ok | 2546 ms | `stage=drafting` |
| `agent.validate` | ok | 0 ms | `stage=validating` |
| `agent.preview` | ok | 2 ms | `stage=previewed` |
| `agent.human_gate` | ok | 0 ms | `stage=awaiting_confirmation` |
| `api.messages` | ok | 2736 ms | **`final_stage=awaiting_confirmation`** |

确认 trace（成功）：`agent.human_gate`(0) → `commit`(56 ms, `commit_id=fa6b771b760b`, `backup_path=null`) → `agent.commit`(56 ms) → `api.confirm`(57 ms, **`final_stage=committed`**)

确认 trace（被拒）：`agent.human_gate`(0) → **`commit_rejected`**(25 ms, `ok=false`, **`error_code=E_REWARD_RANGE`**) → **`agent.commit_rejected`**(26 ms) → `api.confirm`(27 ms, **`final_stage=rejected`**)

> 拒绝路径的 span **改名**了（`commit_rejected` / `agent.commit_rejected`），刻意避免被读成一次成功提交。`docs/demo-script.md` §3 幕 5 写的 `agent.commit` 只适用于成功路径。

取迹命令（PowerShell）：

```powershell
curl.exe -s http://127.0.0.1:8000/api/v1/traces/<trace_id> | ConvertFrom-Json |
  ForEach-Object { $_.events } |
  Format-Table name, status, duration_ms, @{n='final_stage';e={$_.attrs.final_stage}} -AutoSize
```

---

## 6. 两处必须纠正的口径（顺手纠正 runbook §9.3）

runbook §9.3 第 1、4 条说「tokens 是真实计数、只念 tokens、cost 字段没接上」。实测**不是这样**：

| 字段 | 实际含义 | 证据 |
| --- | --- | --- |
| `done.tokens` | **上下文预算估算**（`BuiltContext.total_tokens`），不是模型用量 | 幕 1：`done.tokens=2983`，而同一 trace 的 `llm.structured` 是 `prompt 5743 + completion 304 = 6047` |
| `done.cost_cny` | 恒 `0.0`（图状态未聚合 LLM 成本） | 3 幕全是 `0.0`，而 span 里是 `0.013918` |

**录制口径**：要讲用量/成本，就指着 trace 的 `llm.structured` span 说「prompt 5743、completion 304、一次 1.4 分钱」；`done` 事件只念 `elapsed_ms`。按 runbook 现在的写法念 `done.tokens` 会把用量说小一半。

---

## 7. 零写入 SHA256 证明

排练前后对 `docs/demo-script.md` §1.3 的三处对象做 SHA256 快照（完整清单：`tmp/rehearsal/snapshot-before.json`、`tmp/rehearsal/verification.json`）：

| 对象 | 前 | 后 | 结论 |
| --- | --- | --- | --- |
| `data/tasks`（10 个 json） | 10 个哈希 | **逐字节一致** | 零写入 ✅ |
| `traces/commits.jsonl` | 不存在 | **仍不存在** | 零写入 ✅ |
| `data/backups` | 不存在 | 空目录，0 个文件 | 无备份产物 ✅ |

审计与备份只落在排练用的语料副本（`tmp/rehearsal/corpus`）里，采集完证据后整目录删除。

---

## 8. 与 `docs/demo-script.md` 的偏差（供录制前更新脚本）

| # | 脚本写的 | 实测 | 处置 |
| --- | --- | --- | --- |
| 1 | 幕 4 需求 `…6 级支线…1000 金币` | live 下模型自压到 200 并**提交成功** | 加「**禁止修改我给的数值**」，或按 runbook 改成「新手村 1 级任务」文案 |
| 2 | 幕 4 `checked:{schema:50,business:7,conflict:6}` | `{50,9,6}` | 念实测值 |
| 3 | 幕 5 确认 trace `agent.commit` + 错误码 | 拒绝时是 `commit_rejected` / `agent.commit_rejected` | 改词 |
| 4 | 幕 5 span 顺序 `route → retrieval → …` | JSONL 按 span **结束时间**落盘 | 讲「按结束时间，子 span 在前」 |
| 5 | 幕 2 diff 头 `--- /dev/null` | `--- a/data/tasks/… +++ b/data/tasks/…` | 改词（新建也用 a/b 同路径） |
| 6 | 幕 1 索引 `79` chunks（15/40/24） | **81**（17/40/24） | 改数字 |
| 7 | 幕 2 期待「目标已存在 → 有备份」 | ID 分配器强制 max+1，永远是新建，`backup:null` | 改成条件句「覆盖场景才会备份」 |
| 8 | `done` 的 `tokens`/`cost_cny` | 见 §6 | 别念 |

---

## 9. 证据文件与残留

证据（均在 `tmp/rehearsal/`，已被 `.gitignore` 忽略）：

| 文件 | 内容 |
| --- | --- |
| `rehearsal.json` | 三幕完整 SSE 帧 + 每帧到达时刻 + done |
| `act3-counterexample.json` | 反例 3 次尝试 + 拒绝确认的完整帧 |
| `evidence.json` | 草案/校验/证据源/span 树/写入文件/审计行汇总 |
| `raw/*.sse.txt` | 原始 SSE 抓包（带 `[ms]` 时间戳） |
| `verification.json`、`snapshot-before.json`、`traces-before.jsonl` | 零写入校验 |
| `sibling-demo-rehearsal.md` | 本文件写入前，`docs/demo-rehearsal.md` 的逐字节备份（SHA256 `978390CE…D615`） |

**残留（本会话权限删不掉，需在普通终端执行）**：

```powershell
Remove-Item D:\GameConfig-Copilot\docs\_probe-tool.md  -Force   # 6 字节，内容是 "probe"
Remove-Item D:\GameConfig-Copilot\data\_probe-tool.txt -Force   # 6 字节，内容是 "probe"
Remove-Item D:\GameConfig-Copilot\data\backups -Recurse -Force  # 空目录
Remove-Item D:\GameConfig-Copilot\data\.locks  -Recurse -Force  # 空目录
Remove-Item D:\GameConfig-Copilot\traces\traces.jsonl -Force    # 其中 49 行来自本次排练
```

> 两个 `_probe-tool` 文件是定位「`data/` 不可写」时创建的探针；`traces/traces.jsonl` 是 append-only 轨迹日志，脚本 §1.3 本来就要求录制前删除。
