# 5 分钟演示脚本 · 分镜与录制清单

> 对应任务：**S8-02 演示脚本** + **S8-03 录屏清单**（只写脚本与分镜，**不实际录制**）。
> 版本基线：2026-11-15 checkout；实现事实以本仓库当前代码为准，与 `docs/03 §7` 的差异见 §7。
> 配套文档：[README](../README.md) · [指标口径](05-interview-kit.md#4-指标口径防止被追问翻车) · [架构](02-architecture.md)

---

## 0. 先看清当前环境：两种模式

| 能力 | **live**（`GCC_LLM_API_KEY` 已配置） | **mock / 无 key**（当前环境） |
| --- | --- | --- |
| `GET /healthz`、会话创建 | ✅ | ✅ |
| `gcc ingest / search / eval / memory` | ✅ | ✅（不触网） |
| 生成 → 校验 → 预览 → 确认写入 | ✅ 真实模型 | ⚠️ 仅离线 harness（附录 A，用 `tests/support/fakes.py` 的脚本化 LLM） |
| 1000 金币反例拦截 | ✅（确认时强制复检） | ✅（附录 A 可复现） |
| 轨迹（`/traces/{id}`、`traces/traces.jsonl`） | ✅ | ✅ |
| 指标页（检索） | ✅ `gcc eval` + Streamlit「指标」页 | ✅ 同左 |
| FPVR / Ragas / P95 | ❌ 未接线 | ❌ 未接线 |
| Streamlit 轨迹/指标面板 | ✅ 已交付（`scripts/dashboard.py`；需装 streamlit） | ✅ 同一脚本（本机未装，UI 未实测） |
| Docker | ✅ 文件已交付，**本机无 Docker CLI，未实际 build** | 同左 |

**当前机器实测（2026-11-15）**

```text
Python 3.10.2
GCC_LLM_API_KEY 未设置
Docker CLI 不存在
Streamlit / fastmcp 未安装（dashboard 已交付，需补装 streamlit）
pytest -q  ->  547 passed, 1 skipped（S8 基线；交付时全量 651）
ruff check ->  All checks passed!
```

因此：**live 台词中的模型输出是“预期形态”**（事件名、字段、校验码是确定的；具体任务 ID、文案、耗时以现场为准）。
正式录屏前必须用有 key 的环境完整试跑 2 遍，并把 §6 的兜底方案放在手边。

---

## 1. 演示前 10 分钟检查清单

### 1.1 依赖与索引（一次）

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -e ".[dev,api,rag]"     # 可选组已在 pyproject 声明；面板再加 .[ui]
gcc ingest --source data            # 预期：chunk 总数：79（constraint 15 / task 40 / wiki 24）
gcc eval --suite retrieval          # 预期：recall@5 0.7750（见 §5）
pytest -q                           # 预期：547 passed, 1 skipped（S8 基线；交付时 651）
```

> 命令以 bash 书写。Windows 建议用 Git Bash / WSL（`curl` → `curl.exe` 亦可）；
> `gcc` 等价于 `python -m gameconfig_copilot.cli`；`head/tail/ls/rm` 在 PowerShell 里对应
> `Get-Content -TotalCount` / `-Tail`、`Get-ChildItem`、`Remove-Item`。

### 1.2 启动 API（live）

```bash
# 密钥只进环境变量，不要出现在屏幕/命令历史里
export GCC_LLM_API_KEY="sk-..."          # PowerShell: $env:GCC_LLM_API_KEY="sk-..."
export GCC_MCP_MODE=write                # 演示“确认后落盘”必须显式开写模式
python -m uvicorn gameconfig_copilot.api.main:app --host 127.0.0.1 --port 8000
```

另开一个终端：

```bash
curl -s http://127.0.0.1:8000/healthz
# 预期：{"status":"ok",...,"graph_ready":false,"mcp":{"connected":false,"mode":"write"}}
# graph_ready 首次为 false 是故意的懒加载；发出第一条消息后变 true。
```

轨迹/指标面板（另开终端，演示幕 5/6 用；不装也能用 API 兜底）：

```bash
pip install -e ".[ui]"                     # streamlit
streamlit run scripts/dashboard.py --server.port 8501
# 浏览器打开 http://localhost:8501（轨迹列表 / 轨迹详情 / 指标）
```

### 1.3 演示现场清理

```bash
rm -f traces/traces.jsonl                           # PowerShell: Remove-Item traces/traces.jsonl -ErrorAction Ignore
git status --short data/tasks                        # 确认语料干净；演示后 git restore data/tasks
```

- 终端：字号 ≥ 16pt，窗口 1200×800 以上，深色主题，提示符里不要出现用户名/主机名。
- 浏览器/IM 通知全部关闭；录屏工具只录终端 + 架构图窗口。
- 复述一遍任务需求，确保一句话里包含：**NPC、等级、任务类型、目标、地点、数量**。

---

## 2. 分幕总表

| 时间 | 幕 | 画面 | 一句话目的 |
| --- | --- | --- | --- |
| 0:00–0:30 | 定位 | 架构图（README §3） | 不是 RAG 问答，是“有依据地拒绝、可审计地执行” |
| 0:30–2:00 | 正常生成 | 终端 SSE + 文件 | 检索 → 草案 → 校验 → diff → 确认 → 原子写 |
| 2:00–2:40 | 改奖励 | 终端 SSE | 同一需求改 150 金币，仍然停在预览，不绕过闸门 |
| 2:40–3:40 | 反例 | 终端 SSE | 6 级任务给 1000 金币（上限 200）→ 约束拒绝、零写入 |
| 3:40–4:30 | 归因 | `/traces/{id}` + JSONL | 失败能定位到检索/生成/校验/写入哪一层 |
| 4:30–5:00 | 指标 | `gcc eval` 面板 | 只讲有出处的数字：0.775 / 651 / 5/5 |

---

## 3. 逐句台词与预期输出

### 幕 1 · 0:00–0:30 定位

**画面**：README 的架构图；终端在右侧备用。

**台词**

> ① 游戏策划配一个任务，要在世界观 Wiki、历史任务配置、数值约束三个地方来回翻，重复而且容易错。
> ② 我做的不是一个 RAG 问答，而是一个**能安全动手**的配置 Agent：先查依据，再出结构化草案，跑三层校验，给人看 diff，确认之后才原子写入。
> ③ 核心不是“生成得像”，而是两件事：**有依据地拒绝**，和**每一步都可审计**。

**预期**：无命令输出；30 秒内必须切到下一幕。

---

### 幕 2 · 0:30–2:00 正常生成

**画面**：终端 1，先建会话，再发一条消息。

```bash
SESSION=$(curl -s -X POST http://127.0.0.1:8000/api/v1/sessions \
  -H "Content-Type: application/json" -d '{"designer_id":"u_1024"}' \
  | python -c "import json,sys; print(json.load(sys.stdin)['session_id'])")

curl -N -X POST http://127.0.0.1:8000/api/v1/messages \
  -H "Content-Type: application/json" \
  -d "{\"session_id\":\"$SESSION\",\"message\":\"给铁匠老陈加一条 6 级支线，去矿洞捡 4 块矿石\"}"
```

**台词（跟着事件走，一句一个画面变化）**

> ① “先去查依据——它检索的是**可复用的配置片段**，不是整篇文章：老陈的档案、一条相似支线的结构、还有金币约束。”
> ② “生成的是严格符合 `TaskDraft` Schema 的 JSON，不是一段看起来像配置的文字。”
> ③ “Schema 过了，但它还没有写盘——现在给我的是 diff、目标路径和一个一次性指纹。”
> ④ “我确认之后才写：token 只能用在**这一个草案**上，服务端会再跑一遍完整校验，然后同目录临时文件 + `os.replace` 原子替换。”
> ⑤ “新建任务没有旧版本，所以没有备份；覆盖已有任务时才会先备份——两种情况的审计记录都在 `traces/commits.jsonl`。”

**预期输出（事件顺序固定；值以现场为准）**

```text
event: meta
data: {"trace_id":"tr_...","session_id":"sess_..."}

event: status        data: {"stage":"routing","label":"正在判断意图"}
event: status        data: {"stage":"gathering_context","label":"正在加载世界观/相似任务/约束"}
event: evidence      data: {"items":[{"source":"wiki:npc_profiles#...","source_type":"wiki",...},
                                     {"source":"task:T_SIDE_0213#task","source_type":"task",...},
                                     {"source":"constraint:C_REWARD_GOLD_01#rewards.gold",...}]}
event: status        data: {"stage":"drafting","label":"正在生成草案"}
event: draft         data: {"draft":{"task_id":"T_SIDE_0217","rewards":[{"type":"gold","amount":120},...],...},"version":1}
event: status        data: {"stage":"validating","label":"正在校验草案"}
event: validation    data: {"ok":true,"errors":[],"warnings":[],"checked":{"schema":1,"business":0,"conflict":0}}
event: status        data: {"stage":"previewed","label":"已生成预览"}
event: awaiting_confirmation
data: {"draft_id":"dr_...","commit_token":"...","diff":{"unified_diff":"--- /dev/null\n+++ data/tasks/T_SIDE_0217.json ...","stats":{"added":42,"removed":0}},
       "target_path":"data/tasks/T_SIDE_0217.json","expires_in":600}
event: status        data: {"stage":"awaiting_confirmation","label":"等待人工确认"}
event: done          data: {"status":"awaiting_confirmation","elapsed_ms":...,"tokens":...,"cost_cny":...}
```

确认并展示落盘：

```bash
curl -N -X POST http://127.0.0.1:8000/api/v1/drafts/<draft_id>/confirm \
  -H "Content-Type: application/json" \
  -d '{"commit_token":"<上一步的 token>","note":"demo"}'
```

```text
event: status     data: {"stage":"confirmed","label":"已确认，准备写入"}
event: status     data: {"stage":"committed","label":"写入成功"}
event: committed  data: {"commit_id":"<12 位 hex>","backup":null,"revision":"<新版本>"}
event: done       data: {"status":"committed",...}
```

```bash
python -m json.tool data/tasks/T_SIDE_0217.json | head -20
tail -1 traces/commits.jsonl        # 审计记录：target_path / revision / diff_stats
```

> 注意：`task_id` 由“同类型最大序号 + 1”分配（当前语料最大 `T_SIDE_0216`，所以是新任务 `T_SIDE_0217`）。
> 若现场已存在同名文件，预览会变成“修改”，`committed.backup` 才会有值——这是正常的。

---

### 幕 3 · 2:00–2:40 改奖励为 150

**画面**：同一会话，再发一条。**台词按“重述需求”版本准备**（原因见 §7 偏差 1）。

```bash
curl -N -X POST http://127.0.0.1:8000/api/v1/messages \
  -H "Content-Type: application/json" \
  -d "{\"session_id\":\"$SESSION\",\"message\":\"给铁匠老陈加一条 6 级支线，去矿洞捡 4 块矿石，奖励 150 金币\"}"
```

**台词**

> ① “这次奖励从默认的 120 改成 150，重新生成草案。”
> ② “注意它还是停在预览——**修改需求不等于绕过闸门**，仍然要人确认。”
> ③ “确认后生成新文件；如果目标文件已存在，会先写备份再替换。”

**预期输出**：与幕 2 相同的事件序列；关键断言：

```text
event: draft   ... "rewards":[{"type":"gold","amount":150},{"type":"exp",...}]
event: validation ... "ok":true
event: awaiting_confirmation ... "target_path":"data/tasks/T_SIDE_0218.json"
```

确认后 `event: committed` 的 `backup` 仍可能是 `null`（新建）；若覆盖已有文件则为 `data/backups/<task_id>/<UTC>-<commit_id>.json`。

---

### 幕 4 · 2:40–3:40 反例：6 级任务给 1000 金币

**画面**：同一会话；故意输入明显破坏数值的需求。

```bash
curl -N -X POST http://127.0.0.1:8000/api/v1/messages \
  -H "Content-Type: application/json" \
  -d "{\"session_id\":\"$SESSION\",\"message\":\"给铁匠老陈加一条 6 级支线，去矿洞捡 4 块矿石，奖励 1000 金币\"}"
```

**台词**

> ① “现在我要一个明显破坏数值的配置：6 级任务给 1000 金币。”
> ② “它确实生成了草案，甚至给了预览——但**我不会直接相信生成结果**。我点确认。”
> ③ “写入前的强制复检把它拦下了：`E_REWARD_RANGE`，字段 `rewards[0].amount`，约束来源 `C_REWARD_GOLD_01`，规则上限 200，建议值 200。”
> ④ “关键不是它说了‘不行’，而是：**校验是确定性的、不靠模型自省；拒绝发生在写盘前，目标文件一个字节都没动。**”

**预期输出（确认阶段）**

```text
event: status     data: {"stage":"confirmed","label":"已确认，准备写入"}
event: status     data: {"stage":"rejected","label":"已拒绝"}
event: validation data: {"ok":false,
  "errors":[{"code":"E_REWARD_RANGE","severity":"error","field":"rewards[0].amount",
             "message":"Lv6 的任务金币奖励不得超过 200",
             "constraint_ref":"C_REWARD_GOLD_01",
             "suggestion":{"field":"rewards[0].amount","value":200}}],
  "warnings":[...], "checked":{"schema":50,"business":7,"conflict":6}}
event: error      data: {"code":"E_REWARD_RANGE","message":"提交前复检未通过，草案被拒绝，未写入任何文件。","stage":"rejected"}
event: done       data: {"status":"rejected",...}
```

```bash
ls data/tasks | tail -3     # 没有新增/变化的文件
git status --short data/tasks
```

> 讲清楚这是**深度防御**：草案阶段的 `validate` 节点目前只做 Schema；业务规则在 MCP `commit_draft` 内、文件锁保护下强制复检。
> 即使上游校验被绕过或草案被篡改，也写不进去。细节见 §7 偏差 2。

---

### 幕 5 · 3:40–4:30 轨迹归因

**画面**：浏览器里的 Streamlit 面板（`http://localhost:8501`）→ 侧栏「轨迹详情」。**注意确认动作是新的 trace_id**（`api.confirm`）。

没有 streamlit 时用 API 兜底（同一份 JSONL）：

```bash
curl -s http://127.0.0.1:8000/api/v1/traces/<messages 的 trace_id> \
  | python -c "import json,sys; [print(e['name'], '|', e['status'], '|', e['attrs'].get('final_stage','')) for e in json.load(sys.stdin)['events']]"

curl -s http://127.0.0.1:8000/api/v1/traces/<confirm 的 trace_id> \
  | python -c "import json,sys; [print(e['name'], '|', e['status'], '|', e['attrs'].get('final_stage','')) for e in json.load(sys.stdin)['events']]"
```

**台词**

> ① “打开轨迹面板——每次请求都落一条 JSONL span 树，三个问题一眼能答：检索了什么、工具调了什么、最后有没有写。”> ② “先看正常生成这条：route → retrieval → draft → validate → preview → human_gate，`final_stage=awaiting_confirmation`；点开 span 能看到检索参数和校验计数。”
> ③ “再选反例的确认 trace：human_gate → commit → api.confirm，`final_stage=rejected`；`agent.commit` 的 attrs 里带错误码。”
> ④ “所以线上出问题，不用猜是检索没召回、生成字段错，还是规则误判——按 span 就能定位到层。”

**预期输出（面板列 / API 兜底；span 名是实测值）**

```text
# 面板「轨迹列表」：trace_id · session · span 数 · 错误 span · 时长 · final_stage
# 面板「轨迹详情」：上方 4 个指标卡（span 数 / 错误 span / 时长 / session）
#                   下方 span 表 + 每个 span 的 attrs expander

# 生成请求
route        | ok | routing
retrieval    | ok | gathering_context
agent.draft  | ok | drafting
agent.validate | ok | validating
agent.preview  | ok | previewed
agent.human_gate | ok | awaiting_confirmation
api.messages | ok | awaiting_confirmation

# 确认请求
agent.human_gate | ok | confirmed
agent.commit     | ok | rejected
api.confirm      | ok | rejected
```

```bash
tail -3 traces/traces.jsonl   # 原始 JSONL：trace_id / span_id / parent_id / duration_ms / attrs
```

> 面板数据源：`traces/traces.jsonl`（或 `GCC_TRACE_PATH`）。没有 streamlit 时，`GET /api/v1/traces/{id}`
> 与 `http://127.0.0.1:8000/docs` 是同一份数据的兜底入口。

---

### 幕 6 · 4:30–5:00 指标页

**画面**：先切到 Streamlit 面板「指标」页（检索 suite + `docs/reports/baseline.md` 全文），再切终端跑 `gcc eval` 对照。

```bash
gcc eval --suite retrieval
```

**台词**

> ① “指标页读的是 `reports/*.json` 和已入库的 `docs/reports/baseline.md`。检索层 20 条标注 query：**Recall@5 0.775、Hit@5 0.95、MRR@10 0.7625**；这是 BM25 + RRF 的降级基线，dense 分支还是 Noop，所以没到 0.85 的目标。”
> ② “工程质量：**651 个测试全绿、1 个跳过**（fastmcp 需要 Python 3.11）；S8 基线是 547，交付时并行评测测试合入后涨到 651。`ruff` 全绿；对抗集 **5/5** 全部拦截，错误码逐条核对。”
> ③ “指标页现在只有检索这一层，因为 FPVR、Ragas 和 P95 还没跑——当前环境没有真实 key，这些数字跑不出来；简历和 README 只写有出处的数。”
> ④ “下一步三件事：接 dense adapter 重跑同一份 qrels、用真实 key 补 FPVR/Ragas/P95、把 `--suite all` 统一入口接上。”

**预期输出**

```text
┌─────────────────── gcc eval · 检索基线（S1-07） ───────────────────┐
│ query 数：20 · 相关标注：34                                        │
│ recall@1                   0.3750                                  │
│ recall@5                   0.7750                                  │
│ recall@10                  0.8750                                  │
│ mrr@10                     0.7625                                  │
│ ndcg@10                    0.7382                                  │
│ source_type_precision@10   0.7300                                  │
│ 报告：reports/retrieval-baseline.json                              │
└────────────────────────────────────────────────────────────────────┘
```

---

## 4. 时间不够时的删减顺序

保住“安全写入闭环 + 反例 + 指标出处”，按以下顺序砍：

1. 幕 5 轨迹只讲生成 trace，不打开确认 trace（省 20s）；
2. 幕 3 改奖励只看 draft 事件，不做确认（省 20s）；
3. 幕 1 架构图不展开，直接说定位（省 15s）；
4. **不能砍**：幕 4 反例、幕 6 的 0.775/651/5/5 出处。

---

## 5. 指标出处（被追问时直接翻）

| 数字 | 出处 | 口径 |
| --- | --- | --- |
| Recall@5 **0.775** | [docs/reports/baseline.md](../docs/reports/baseline.md) §1 + [baseline.json](../docs/reports/baseline.json)（S7-08 入库快照，`generated_at` 2026-11-15T19:22:43+08:00）；本地重跑 `gcc eval` 会刷新 `reports/retrieval-baseline.json` 的时间戳，数值不变 | 20 条 query、34 个相关标注；`top_k=10`、`bm25s` + RRF、`dense=noop`；按 query 取平均 |
| Hit@5 0.95 / MRR@10 0.7625 / nDCG@10 0.7382 / Recall@10 0.875 | 同上 | 同一次运行 |
| **547 → 651 passed / 1 skipped** | `pytest -q` / [docs/reports/baseline.md](reports/baseline.md) §6（Wave 3 收官 547；Wave 4 报告生成时 651） | 1 skipped = `tests/integration/test_writer.py` 的 fastmcp 用例（Py3.10 无 fastmcp） |
| **对抗 5/5** | `pytest tests/unit/test_rules.py::test_adversarial_case_is_blocked_with_expected_codes -q` → `5 passed` | 5 条 fixture：越界奖励、重复 ID、缺失引用、依赖成环、提示注入；逐条断言错误码 |
| FPVR / 平均修复轮次 / Ragas / P95 | —（未测） | 需要真实 LLM；不写占位数字 |

---

## 6. 录屏分镜（S8-03，只列清单）

### 6.1 录制设置

| 项 | 要求 |
| --- | --- |
| 画布 | 1920×1080，30fps；终端字号 16–18pt，深色主题 |
| 音频 | 外接麦克风，先录 10 秒试听；语速 ~240 字/分钟 |
| 隐私 | API key 只通过环境变量注入；终端提示符改短；关闭通知与无关标签页 |
| 长度 | 目标 5:00 ± 10s；每幕留 2s 静帧方便剪辑 |
| 预跑 | 正式录前完整跑 2 遍；确认 3 次请求都稳定通过 Schema |
| 兜底 | 网络/模型失败 → 切附录 A 的离线 harness，口播“这是离线回放，接口与校验完全一致，只有生成模型被替换” |

### 6.2 分镜表

| # | 时间 | 镜头 | 画面动作 | 口播要点 | 失败兜底 |
| --- | --- | --- | --- | --- | --- |
| 1 | 0:00–0:25 | 架构图 | 鼠标依次指交互层→编排→工具/记忆→横切层 | “不是 RAG 问答，是能安全动手的 Agent” | 无 |
| 2 | 0:25–0:50 | 终端 | `gcc search "铁匠老陈是什么立场" --top-k 3`，指来源与分数 | “检索单元是可复用配置片段，带可回溯来源” | 预先截好的终端图 |
| 3 | 0:50–1:50 | 终端 | 发正常需求；逐个事件停顿；指 `evidence/draft/validation` | “Schema 通过但没写盘；diff + 指纹 + 一次性 token” | 切附录 A |
| 4 | 1:50–2:20 | 终端 | 确认；`cat` 新文件；`tail` 审计日志 | “确认后才原子写；新建无备份，覆盖才有” | 重放附录 A 的 committed 输出 |
| 5 | 2:20–2:45 | 终端 | 发 150 金币需求；指 `rewards.amount=150` | “改需求也要重新过闸门” | 同 #3 |
| 6 | 2:45–3:40 | 终端 | 发 1000 金币需求；点确认；停在校验错误上 3 秒 | “确定性校验、约束来源、建议值、零写入” | 切附录 A 第三段 |
| 7 | 3:40–4:25 | 面板 | Streamlit「轨迹列表」→「轨迹详情」，选生成 trace 与 confirm trace，展开 attrs | “按 span 归因到检索/生成/校验/写入” | 切回 `GET /traces/{id}` |
| 8 | 4:25–5:00 | 面板 | Streamlit「指标」页（检索 suite + baseline.md）；指 0.775；必要时切终端 `gcc eval` | “只讲有出处的数字；未测的不编” | 打开 `docs/reports/baseline.md` |

### 6.3 录制后检查

- [ ] 全片搜索 `sk-`、`API_KEY`、本机绝对路径、用户名，确认零泄露；
- [ ] 事件名与 README/架构文档一致（没有为了好看改词）；
- [ ] 字幕校对：`commit_token`、`os.replace`、`C_REWARD_GOLD_01`、`E_REWARD_RANGE`；
- [ ] 视频 ≤ 5:15；导出 1080p MP4（H.264），文件名 `gameconfig-copilot-demo-YYYYMMDD.mp4`；
- [ ] 把录屏链接补进 README（本任务不产出视频文件）。

---

## 7. 与 `docs/03 §7` 的差异（录屏前必读）

`docs/03 §7` 是设计稿；当前实现的真实行为有 4 处差异，脚本已按实现改写：

1. **“追问：奖励改成 150”不是真正的多轮**：`/api/v1/messages` 每次用 `new_state(message, ...)` 单轮启动，`SessionStore` 只记录消息，没有把上一轮草案/历史注入 graph。因此脚本要求**重述完整需求**。等 S6-04 把会话历史接进 context 后，可恢复逐句追问。
2. **业务校验在确认时，而不是草案阶段**：graph 的 `validate` 节点当前只接 `schema_validate`；校验器在 `commit_draft` 里、文件锁内强制复检。所以 1000 金币会先看到“预览”，点确认才被 `E_REWARD_RANGE` 拒绝。**零写入的结论不变**，且这是更强的深度防御；但不要说“它在生成阶段就拒绝了”。
3. **“确认 → 文件变化 + 备份”只对覆盖成立**：新建任务没有旧版本，`committed.backup` 为 `null`；审计记录始终写 `traces/commits.jsonl`。
4. **面板已交付，但 streamlit 是可选依赖**：`scripts/dashboard.py` 需要 `pip install -e ".[ui]"`；本机未安装，UI 未实测。没有 UI 时轨迹用 `GET /api/v1/traces/{trace_id}` + JSONL，指标用 `gcc eval` 终端面板，台词不变。

---

## 附录 A · 无 key 离线复现（mock，实测可跑）

不是产品入口，只是把真实 API 接到脚本化 LLM 上，用于**无 key 环境下验证全链路**：任务与约束写入临时目录，不污染 `data/`；轨迹仍写仓库 `traces/`（方便演示幕 5）。
保存为 `demo_offline.py`（不入库）后运行；`tests/support/fakes.py` 是仓库自带的测试替身。

```python
"""Offline demo harness: 真实 API + 脚本化 LLM，无网络、无 key。"""
import json, shutil, sys, tempfile
from pathlib import Path

sys.path.insert(0, "tests")
from fastapi.testclient import TestClient
from gameconfig_copilot.api.main import create_app
from gameconfig_copilot.mcp_server.tools import ToolService
from gameconfig_copilot.mcp_server.writer import Writer
from support.fakes import FakeLLM, FakeRetriever, default_hits, valid_task_payload

TASK = "给铁匠老陈加一条 6 级支线，去矿洞捡 4 块矿石"
root = Path(tempfile.mkdtemp()) / "repo"
tasks = root / "data" / "tasks"; tasks.mkdir(parents=True)
constraints = root / "data" / "constraints"; constraints.mkdir(parents=True)
for source, target in ((Path("data/tasks"), tasks), (Path("data/constraints"), constraints)):
    for path in source.glob("*.*"):
        shutil.copy2(path, target / path.name)

llm = FakeLLM(structured_sequence=[
    valid_task_payload(gold=120),
    valid_task_payload(gold=150),
    valid_task_payload(gold=1000),
])
app = create_app(llm=llm, retriever=FakeRetriever(default_hits()),
                 tools=ToolService(Writer(root)), mcp_mode="write",
                 tasks_dir=tasks, session_ttl_s=3600)
client = TestClient(app)
session = client.post("/api/v1/sessions", json={"designer_id": "u_1024"}).json()["session_id"]
print("session:", session)

def events(response):
    for block in "".join(response.iter_text()).strip().split("\n\n"):
        lines = block.splitlines()
        name = next(line[7:] for line in lines if line.startswith("event: "))
        yield name, json.loads(next(line[6:] for line in lines if line.startswith("data: ")))

def turn(message):
    with client.stream("POST", "/api/v1/messages",
                       json={"session_id": session, "message": message}) as response:
        parsed = list(events(response))
    for name, data in parsed:
        if name == "meta":
            print("meta trace_id:", data["trace_id"])
        elif name == "status":
            print("status:", data["stage"], data.get("label", ""))
        elif name == "validation":
            print("validation: ok=", data["ok"], "errors=",
                  [{"code": e["code"], "field": e["field"], "constraint_ref": e["constraint_ref"]}
                   for e in data["errors"]])
        elif name == "awaiting_confirmation":
            print("awaiting:", data["draft_id"], data["target_path"], "expires_in", data["expires_in"])
        elif name == "committed":
            print("committed:", data["commit_id"], "backup=", data.get("backup"))
        elif name == "error":
            print("error:", data["code"], data["message"])
        elif name == "done":
            print("done:", data["status"])
    return parsed

def confirm(parsed):
    awaiting = next(data for name, data in parsed if name == "awaiting_confirmation")
    with client.stream("POST", f"/api/v1/drafts/{awaiting['draft_id']}/confirm",
                       json={"commit_token": awaiting["commit_token"], "note": "demo"}) as response:
        for name, data in events(response):
            if name in {"status", "validation", "committed", "error", "done"}:
                print("  [confirm]", name, json.dumps(data, ensure_ascii=False)[:180])

print("\n== 1) 正常生成（120 金币，只预览不提交）==")
turn(TASK)
print("\n== 2) 改奖励 150，确认提交 ==")
confirm(turn(TASK + "，奖励 150 金币"))
written = json.loads((tasks / "T_SIDE_0215.json").read_text(encoding="utf-8"))
print("written gold:", written["rewards"][0]["amount"],
      "backups:", [p.name for p in (root / "data" / "backups").rglob("*.json")])
print("\n== 3) 反例 1000 金币，确认时被拦 ==")
confirm(turn(TASK + "，奖励 1000 金币"))
```

运行（Windows PowerShell 先设 `$env:PYTHONUTF8="1"`）：

```bash
python demo_offline.py
```

关键预期（实测）：

```text
== 1) 正常生成 ==
status: routing / gathering_context / drafting / validating
validation: ok= True errors= []
awaiting: dr_... data/tasks/T_SIDE_0214.json expires_in 600
done: awaiting_confirmation

== 2) 改奖励 150，确认提交 ==
committed: 5a63fd086d91 backup= data/backups/T_SIDE_0215/20261115T112333Z-5a63fd086d91.json
done: committed
written gold: 150 backups: ['20261115T112333Z-5a63fd086d91.json']

== 3) 反例 1000 金币 ==
[confirm] validation: ok=false errors=[{"code":"E_REWARD_RANGE","field":"rewards[0].amount","constraint_ref":"C_REWARD_GOLD_01"}]
[confirm] error: E_REWARD_RANGE 提交前复检未通过，草案被拒绝，未写入任何文件。
[confirm] done: rejected
```

> mock 下 ID 从 `T_SIDE_0214` 起递增，是因为 `FakeRetriever` 只提供了 `T_SIDE_0213` 一条任务证据；
> live 下按真实索引里最大序号 +1 分配。两者都符合 `TaskIdAllocator` 规则。

---

## 附录 B · 一页速查（贴在屏幕边）

```text
健康检查   GET  /healthz
建会话     POST /api/v1/sessions            {"designer_id":"u_1024"}
发消息     POST /api/v1/messages            {"session_id":"...","message":"..."}   (SSE)
确认       POST /api/v1/drafts/{id}/confirm {"commit_token":"...","note":"demo"}
驳回       POST /api/v1/drafts/{id}/reject  {"commit_token":"...","reason":"..."}
轨迹       GET  /api/v1/traces/{trace_id}

正常链：meta → routing → gathering_context → evidence → drafting → draft
        → validating → validation(ok) → previewed → awaiting_confirmation → done
确认链：confirmed → committed → done(committed)
反例链：confirmed → rejected → validation(ok=false, E_REWARD_RANGE, C_REWARD_GOLD_01)
        → error → done(rejected)          # 零写入
```
