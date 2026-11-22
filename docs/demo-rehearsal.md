# 手动 Live 排练包 · `demo_live.py` + 旁白 / 检查清单

> 任务：`tsk_30977c49e5`（手动排练包）。配套脚本：[`scripts/demo_live.py`](../scripts/demo_live.py)。
> 基线：2026-11-19 checkout。API 契约以 `src/gameconfig_copilot/api/{routes,sse}.py` 为准，演示编排沿用 [demo-script.md](demo-script.md)。
>
> ✅ **已实跑（2026-11-19）**：真实 API + 真实 key，`GCC_MCP_MODE=write`，base_url `http://127.0.0.1:8000`；`python scripts/demo_live.py` 三幕全部符合预期，总耗时 5.7s。数据、trace 与排练前踩到的环境坑见 **⑨ 实测记录（2026-11-19）**。
>
> ⚠️ 沙箱/CI 里仍然不能跑：不能启动 API、不能调真实模型、不能写 `data/`（ACL 不批）。脚本在沙箱里只做 `ast.parse` 与 `ruff check`，真实链路必须在本机终端执行。**如果执行报错，把完整输出（含 `trace_id` 和错误码）贴回**，不要只贴最后一行。

---

## ① 前提与检查清单

### 1.1 一次性准备

| # | 项 | 命令 / 检查 | 通过标准 |
| --- | --- | --- | --- |
| 1 | 依赖 | `pip install -e ".[dev,api,rag]"` | 能 `python -c "import httpx, fastapi"`；`gcc` 可执行（否则用 `python -m gameconfig_copilot.cli` 代替） |
| 2 | 模型 key | `python -c "from gameconfig_copilot.config import get_settings; s=get_settings(); print(s.llm_provider, s.llm_model, 'key=set')"` | 输出 `deepseek deepseek-chat key=set`（或其他已配置 provider）；**不要把 key 打到录屏画面** |
| 3 | 检索索引 | `gcc ingest --source data` | `索引已是最新` 或 `索引构建完成`，并打印 `chunk 总数：81（constraint 17 / task 40 / wiki 24）`；以你的 `data/` 实际输出为准 |
| 4 | 写模式 | 启动 API 的**同一个窗口**执行 `$env:GCC_MCP_MODE="write"` | `healthz.mcp.mode == "write"`；否则 `/confirm` 会 403 `E_WRITE_DISABLED` |
| 5 | 端口 | `Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue` | 无输出 = 8000 空闲；被占用就换 8001，并给脚本加 `--base-url` |
| 6 | 语料基线 | `(Get-ChildItem data\tasks\*.json).Count` | 当前为 **10**；最大支线 `T_SIDE_0216`。记在便签上，幕 4 后对照 |
| 7 | 终端 | 字号 16–18pt、窗口 ≥1200×800、深色主题、提示符不含用户名 | 中文不出现方块/问号；建议先 `chcp 65001` |
| 8 | OBS | 1920×1080 / 30fps / 窗口捕获终端；麦克风 −12dB，先录 10 秒试听 | 通知、IM、浏览器标签全部关闭；API key 不入镜 |
| 9 | 开场 30 秒 | 只保留 `healthz` 输出在屏幕上；确认脚本 `--help` 可用；深呼吸 | 30 秒内能说出第一句旁白 |

关于第 4 项，有一个**本仓库特有的坑**：

- `.env` 当前写的是 `GCC_MCP_MODE=readonly`；
- 但 `api/main.py` 的 `_mcp_mode()` 读的是**进程环境变量** `os.environ`，不是 `Settings` 的 `.env`；
- 所以只改 `.env` 不一定生效，也**不要**用 `uvicorn --env-file .env` 启动（会把 `readonly` 灌进环境）。正确做法是 PowerShell 里显式 `$env:GCC_MCP_MODE="write"`，启动后用 `healthz` 验证。

第 3 项的 chunk 数与 README 里的 `79` 不同：当前语料快照是 **81**（constraint 17 / task 40 / wiki 24），README 的 79 是旧文案。`40` 是 task 类 chunk 数，不是任务文件数（当前 10 个任务文件）。

### 1.2 现场检查（录制前 5 分钟）

- [ ] `healthz` 返回 `mode=write`，`graph_ready=false`（第一次发消息后才变 `true`，是懒加载，不是故障）。
- [ ] `data/tasks` 文件数 = 便签上的基线；`traces/commits.jsonl` 行数也记一下（没有文件就是 0）。
- [ ] 两个终端：左边 uvicorn 日志，右边脚本；两边都 `chcp 65001`。
- [ ] 脚本窗口 `cls` 清屏，只留 `healthz` 一行。
- [ ] `python scripts/demo_live.py --help` 已在本机跑过（不要录进正片）。
- [ ] 手机静音、OBS 开始录但先不讲话，确认只有终端画面。

---

## ② 启动命令（PowerShell 两行）

```powershell
$env:GCC_MCP_MODE = "write"
python -m uvicorn gameconfig_copilot.api.main:app --host 127.0.0.1 --port 8000
```

> key 已在本机 `.env` 中；换机器时先补 `$env:GCC_LLM_API_KEY="sk-..."`，再执行上面两行。
> 必须在**仓库根目录**启动，服务端才会按相对路径写 `data/tasks`。

另开一个窗口做健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/healthz | ConvertTo-Json -Compress
```

预期（写模式、首次启动、尚未发消息）：

```json
{"status":"ok","service":"gameconfig-copilot","wave":"S2+S3+S4+S5","sessions":0,"graph_ready":false,"mcp":{"connected":true,"mode":"write"}}
```

说明：

- `connected:true` 是当前实现：`healthz` 把 `mcp.connected` 直接映射为 `write_enabled`。README 里写的 `false` 是旧文案，以代码/本命令为准。
- `graph_ready:false` 正常；第一条 `/messages` 之后变 `true`。
- 如果 `mode=readonly`：停掉 uvicorn，确认**同一个窗口**里 `$env:GCC_MCP_MODE` 是 `write`，再重启；不要继续往下录。
- 如果 8000 被占用：换 `--port 8001`，脚本加 `--base-url http://127.0.0.1:8001`。
- 如果首次写入报 `E_IO: 无法创建运行目录 data\backups`（WinError 5）：`data/` 的 ACL 不允许建子目录，不是代码问题；用隔离数据目录 `$env:GCC_TASKS_DIR` 绕开，具体命令见 9.2。

---

## ③ 跑脚本

```powershell
# 全流程：三幕，两次确认写盘 + 一次反例拒绝
python scripts/demo_live.py
```

只预览、绝不写盘（用于开录前彩排）：

```powershell
python scripts/demo_live.py --skip-confirm
```

换端口 / 模型很慢：

```powershell
python scripts/demo_live.py --base-url http://127.0.0.1:8001
python scripts/demo_live.py --timeout 300
```

| 参数 | 默认 | 作用 |
| --- | --- | --- |
| `--base-url` | `http://127.0.0.1:8000` | API 根地址 |
| `--skip-confirm` | 关 | 只预览，不调 `/confirm`；零写入，也看不到确认时的 `E_REWARD_RANGE` |
| `--timeout` | `180` | SSE 读取超时秒数；模型慢就调大 |
| `--designer` | `u_1024` | 会话的 `designer_id` |

脚本行为：

- 只发 HTTP 请求，**不直接读写 `data/`**；写盘全部由 API 的 `/confirm` 在写模式 + 校验通过后完成。
- SSE 用 `iter_lines()` 解析；每幕异常会被捕获并继续下一幕。
- 429/502/503/504 最多重试 3 次，按 `Retry-After` 等待（上限 10 秒）。
- 退出码：`0` 全部符合预期；`1` 某幕结果不符或抛异常；`2` 健康检查/建会话/写模式检查失败。

**建议彩排顺序**：先 `--skip-confirm` 跑一遍（确认网络、key、事件流、中文显示都正常），再全量跑。全量跑会真的写两个任务文件；本包不自动清理，是否保留（或手工删掉新增的 `data/tasks/T_SIDE_02xx.json`）由你决定。

---

## ④ 幕 1–4 旁白（中文，可直接照读）

> 文档的「幕 1–4」是**演示分镜**；脚本内部的「第 1–3 幕」是三次 API 请求。对应关系如下，别在口播里说错编号。

| 文档幕 | 时间 | 脚本对应 | 画面 |
| --- | --- | --- | --- |
| 幕 1 · 定位 | 0:00–0:30 | 无（脚本启动前） | 架构图 / 终端 |
| 幕 2 · 正常生成 | 0:30–2:20 | 脚本第 1 幕 | SSE + confirm + 文件 |
| 幕 3 · 奖励改 150 | 2:20–3:20 | 脚本第 2 幕 | SSE + confirm |
| 幕 4 · 反例 1000 | 3:20–4:30 | 脚本第 3 幕 | SSE + confirm 被拒 |
| 收尾 | 4:30–5:00 | 脚本总览 | 终端 |

四幕旁白加收尾合计约 **2 分 30 秒**（按 4 字/秒），其余时间是命令执行、模型等待和指屏幕。每段正式旁白都在 20–40 秒内。

### 幕 1 · 定位（0:00–0:30，口播约 28 秒）

**画面**：架构图，或只有 `healthz` 的终端。鼠标依次指「交互 / 编排 / 工具与记忆 / 横切」。

> 游戏策划配一条任务，要在世界观 Wiki、历史任务配置和数值约束之间来回翻，重复而且容易错。我做的不是 RAG 问答，而是一个能安全动手的配置 Agent：先查依据，再出结构化草案，跑校验，给人看 diff，确认之后才原子写入。核心不是生成得像，而是有依据地拒绝、每一步可审计。

### 幕 2 · 正常生成（0:30–2:20，口播约 37 秒）

**画面**：右侧窗口执行 `python scripts/demo_live.py`；跟着事件走，指 `evidence`、`awaiting_confirmation` 的 diff、`committed`。

> 先跑一条正常需求：6 级支线，去矿洞捡 4 块矿石，奖励别太高。它先检索可复用的配置片段：老陈的档案、相似支线、金币约束；然后生成严格符合 Schema 的 JSON。注意现在还没有写盘，只给了 diff、目标路径和一次性 token。我确认之后，服务端会再跑一遍完整校验，通过才原子替换。新建任务没有旧版本，所以备份为空；覆盖已有任务才会先备份。

**垫场句**（模型超过 10 秒没出下一个事件时用，不算正式旁白）：

- “现在停在 `drafting`，等模型返回；SSE 是逐事件推的，不是最后一次性吐出来。”
- “事件顺序是固定的：`meta` 第一，`done` 最后。”

### 幕 3 · 奖励改 150（2:20–3:20，口播约 31 秒）

**画面**：脚本自动进入第 2 幕；指 `draft.rewards` 里的 `150`，再指 confirm 的 `committed`。

> 同一条需求，只把奖励改成 150 金币。我重述一遍完整需求——当前版本没有把上一轮草案注入上下文，所以不能只说“改成 150”。它重新检索、重新生成，仍然停在预览。修改需求不等于绕过闸门，还是要我确认；确认后写入新文件，事件里能看到 `committed` 和 `commit_id`。

**垫场句**：

- “这里没有把上一轮草案塞回上下文，所以我要把需求说全；这是当前实现的边界，不是模型听不懂。”

### 幕 4 · 反例 1000 金币（3:20–4:30，口播约 36 秒）

**画面**：脚本第 3 幕；先指 `validation(ok=true)` 和预览，再指 confirm 后的错误码那几行（`E_REWARD_RANGE`、字段、约束来源）。

> 现在故意给一个破坏数值的需求：1 级任务给 1000 金币。它照样生成了草案和预览，但我不会相信生成结果，我点确认。写入前的强制复检把它拦下：错误码 `E_REWARD_RANGE`，字段 `rewards[0].amount`，约束 `C_REWARD_GOLD_01`，1 级上限 200。关键是校验是确定性的，不靠模型自省；拒绝发生在写盘前，目标文件一个字节都没动。

**垫场句**：

- “预览阶段只过 Schema，业务规则在确认时由 MCP 在文件锁内强制复检——这是深度防御，所以不要期待它在生成阶段就拒绝。”

### 收尾（4:30–5:00，口播约 16 秒）

**画面**：脚本的「总览」和三条 trace_id。

> 所以这套东西的价值不是写得更快，而是写错的时候能拦住、拦下之后能解释、每一步都能回放。后面可以看轨迹和指标，数字只讲有出处的。

---

## ⑤ 预期输出

> 以下是**契约级**的预期：事件名、字段、错误码是确定的；具体 `task_id`、文案、耗时、`commit_id` 以现场为准。脚本会把每行截断到适合上镜的长度。
>
> 2026-11-19 实跑已逐条对上（三幕结果、耗时、`commit_id` 见 ⑨），下面是“应该长这样”，不是“希望长这样”。

### 5.1 幕 2（脚本第 1 幕）：生成 → 确认成功

`POST /api/v1/messages` 的事件序列：

```text
meta
status(routing)
status(gathering_context)
evidence
status(drafting)
draft
status(validating)
validation(ok=true)
status(previewed)
awaiting_confirmation
status(awaiting_confirmation)
done(status="awaiting_confirmation")
```

关键字段：

- `awaiting_confirmation.draft_id`：`dr_` + 12 位 hex；
- `commit_token`：`ct_` 开头的一次性 token，10 分钟有效（`expires_in: 600`）；
- `target_path`：`data/tasks/T_SIDE_0217.json`（当前最大支线是 `T_SIDE_0216`，新任务按“同类型最大序号 + 1”分配）；
- `diff`：`unified_diff` + `stats.added/removed`。

> `draft` 事件里的字段名是 `task_type` 与 `level_range`（如 `level_range.min=6`），脚本会显示成 `level=6-9 type=side`；不要念成 `draft.level`。

`POST /api/v1/drafts/{draft_id}/confirm` 的事件序列：

```text
meta
status(confirmed, "已确认，准备写入")
status(committed, "写入成功")
committed
done(status="committed")
```

- `committed.commit_id`：12 位 hex；
- `committed.backup`：**新建任务为 `null`**（脚本打印为 `None`）；
- `committed.revision`：新内容的哈希字符串（不是整数版本号）；
- 成功路径**不会再发 `validation` 事件**（当前实现只在提交返回 report 时发）；不要在旁白里承诺“确认时再看到一次 validation ok”。

### 5.2 幕 3（脚本第 2 幕）：150 金币

事件序列与 5.1 相同。关键断言：

- `draft.rewards` 里金币为 `150`；
- 目标文件是 `data/tasks/T_SIDE_0218.json`（同一个 graph 进程内，`TaskAgent` 会记住上一轮分配的 `T_SIDE_0217`，所以第二轮是 `0218`；即使检索索引还是旧的，也不会撞 ID）；
- 仍是新建，`backup: null`。

如果现场模型复用了已存在的 `task_id`（例如你重跑过、文件已在），预览会变成“修改”，`committed.backup` 才会出现：

```text
data/backups/<task_id>/<UTC 时间戳>-<commit_id>.json
```

这不是错误，但旁白不要把它说成“每次都会备份”。

### 5.3 幕 4（脚本第 3 幕）：1000 金币 → 零写入

生成阶段与 5.1 前半段相同，**注意**：

```text
status(drafting) → draft → status(validating) → validation(ok=true)
→ status(previewed) → awaiting_confirmation → done(awaiting_confirmation)
```

这里的 `validation(ok=true)` 只代表 **Schema 通过**；业务规则不在这一层。不要口播“它在生成阶段就拒绝了”。

`/confirm` 的事件序列：

```text
meta
status(confirmed, "已确认，准备写入")
status(rejected, "已拒绝")
validation(ok=false, errors=[E_REWARD_RANGE, ...])
error(code="E_REWARD_RANGE", stage="rejected")
done(status="rejected")
```

`validation.errors[0]` 预期形态：

```json
{
  "code": "E_REWARD_RANGE",
  "severity": "error",
  "field": "rewards[0].amount",
  "message": "Lv1 的任务金币奖励不得超过 200",
  "constraint_ref": "C_REWARD_GOLD_01",
  "suggestion": {"field": "rewards[0].amount", "value": 200}
}
```

等级以草案为准：若模型给的等级是 1，消息就是 `Lv1 ... 不得超过 200`；6 级同样是 200（Lv≤10 档）。错误码与约束来源是稳定的。

零写入的证据：

- 没有 `committed` 事件；
- `data/tasks` 文件数与幕 4 之前相同；
- `traces/commits.jsonl` 行数不变。

现场核对：

```powershell
(Get-ChildItem data\tasks\*.json).Count
@(Get-Content traces\commits.jsonl -ErrorAction Ignore).Count
```

### 5.4 文件 / 备份 / 审计变化

| 动作 | `data/tasks` | `data/backups` | `traces/commits.jsonl` |
| --- | --- | --- | --- |
| 幕 2 committed | 新增 `T_SIDE_0217.json` | 不新增（新建任务） | +1 条 |
| 幕 3 committed | 新增 `T_SIDE_0218.json` | 不新增（新建任务） | +1 条 |
| 幕 4 rejected | 无变化 | 无变化 | 不新增 |

`traces/traces.jsonl` 会额外记录每次 `/messages` 和 `/confirm` 的 span；确认动作是独立的 `trace_id`，幕 5 讲轨迹时用得上。

### 5.5 现场输出不一样怎么办

- **没有 `awaiting_confirmation`**：看 `error.code` 和 `error.stage`；可能是 Schema 校验失败（修复 2 轮后拒绝）、意图被判成查询、或模型超时。不要硬演“已经生成”。
- **草案阶段 `validation(ok=false)`**：说明 Schema 没过；脚本会停在 `repairing` 或直接 `rejected`。这是真实失败，重跑一次；连续两次失败就切 `--skip-confirm` 只讲预览，或改用离线回放。
- **1000 金币被模型自己压到 200**：`/confirm` 会成功写入。脚本会打印 `[!!]` 和“预期 rejected”。**不要假装被拒**；重跑并更明确地说“必须写 1000，不要修正”。如果仍不出现反例，按 7 的话术说明“模型这次自行修正了，闸门没有被触发”，不要剪成假镜头。
- **中文乱码**：两个窗口都 `chcp 65001`；仍乱码就用 Windows Terminal，或把输出重定向到文件再看。

---

## ⑥ 5 分钟时间轴

| 时间 | 画面 / 动作 | 旁白 | 缓冲 |
| --- | --- | --- | --- |
| 0:00–0:30 | 架构图或 `healthz`；不操作 | 幕 1 | 15 秒 |
| 0:30–0:45 | 切到脚本窗口，执行 `python scripts/demo_live.py` | 报幕：“先跑一条正常需求” | 命令启动 |
| 0:45–2:20 | 事件滚动；指 `evidence`、`diff`、`committed`；可切到 `data/tasks` / `commits.jsonl` | 幕 2 + 垫场 | 模型等待 30–60 秒 |
| 2:20–3:20 | 脚本进入第 2 幕；指 `150` 和 `committed` | 幕 3 | 20 秒 |
| 3:20–4:30 | 第 3 幕；先指 `validation(ok=true)`，再指 `E_REWARD_RANGE`；核对文件数 | 幕 4 | 20 秒 |
| 4:30–5:00 | 脚本「总览」；指三条 trace_id | 收尾 | 10 秒 |

脚本墙钟时间实测 **5.7 秒**（2026-11-19：3 次生成 + 3 次确认，预览 1.7–2.1 s/幕、确认 25–61 ms；模型或网络慢时会更久）。也就是说正片的时间几乎全靠旁白、指屏幕和留白填满：跑得快就停在 `diff` 或 `commits.jsonl` 上多讲两句，**不要为了“看起来在干活”重跑**；跑得慢就用垫场句，不要催促模型。

---

## ⑦ 失败兜底

| 现象 | 判断 | 应对 | 话术（可直接照读） |
| --- | --- | --- | --- |
| `/confirm` 返回 403 `E_WRITE_DISABLED` | 启动时不是 write 模式 | 停 uvicorn，检查同窗口 `$env:GCC_MCP_MODE`，重启；或本次改 `--skip-confirm` 只演预览 | “写模式没开，服务端拒绝写入；这是默认的 fail-closed，不是脚本坏了。今天先看预览，写入路径在完整环境里演示。” |
| 409 `E_COMMIT_TOKEN_CONSUMED` | token 被重复使用（双击/重跑确认） | 不要重试；用新消息重新生成草案 | “token 是一次性的，重复确认会被拒绝——这本身就是防重复写。” |
| 409 `E_FINGERPRINT_MISMATCH` | 草案在预览后被改动 | 停止本幕，重跑一次；不要绕过 | “确认的草案和预览时不一致，服务端拒绝写入；宁可失败也不写错。” |
| 409 `E_STALE_REVISION` | 预览后目标文件被别人改过 | 展示错误，说明零写入；重新生成 | “文件在预览和确认之间变了，所以这次提交被拒绝；这正是乐观锁要挡的。” |
| 410 `E_COMMIT_TOKEN_EXPIRED` | 超过 token 10 分钟有效期 | 重新跑一场；不要手工延长 | “token 过期了，重新生成一份草案。” |
| 404 `E_COMMIT_TOKEN_UNKNOWN` | token/草案 ID 对不上 | 重新复制 `draft_id` 和 token；建议直接用脚本 | “这个 token 不属于当前草案，服务端不认。” |
| 429 / 502 / 503 / 504 | 模型侧限流或上游不可用 | 脚本自动重试 3 次；仍失败就停，不要连点 | “模型侧在限流，先停一下；已经生成的部分都是真实事件，不补假镜头。” |
| SSE 读取超时 | 模型单次调用超过 `--timeout` | 本幕会标 `error` 并继续；换 `--timeout 300` 重跑该幕 | “这次等太久了，先跳过，后面单独重跑这一幕。” |
| `E_INTERNAL` 且消息提到 `GCC_LLM_API_KEY` | key 缺失/为空 | 补 `.env` 或环境变量，**重启 uvicorn**（进程内配置有缓存） | “API 进程没拿到 key；配置是启动时读的，需要重启。” |
| 提示找不到索引 / `out/index` | 没跑 `gcc ingest` 或目录不对 | 仓库根目录执行 `gcc ingest --source data`，重启 | “检索索引没构建，先跑一次 ingest。” |
| 中文显示为乱码 | 控制台代码页不是 UTF-8 | 两个窗口 `chcp 65001`；或用 Windows Terminal | 不解释，直接调好再录。 |
| 脚本连不上 API | 端口/地址不对 | 确认 `healthz`；用 `--base-url` 指到实际端口 | “先确认服务在 8000。” |

**通用原则**：被拒绝的确认都发生在文件替换之前；不要为了画面重试到成功，也不要把拒绝剪掉。拒绝本身就是演示内容。

---

## ⑧ 手动 curl 版（不想用脚本时）

以下命令均在 **PowerShell** 中执行；先确保 API 已按第 ② 节启动。

### 8.1 建会话

```powershell
$sid = (Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8000/api/v1/sessions `
  -ContentType "application/json" `
  -Body '{"designer_id":"u_1024"}').session_id
$sid
```

### 8.2 幕 2：发消息（SSE）

```powershell
$body = @{
  session_id = $sid
  message    = "给铁匠老陈加一条 6 级支线，去矿洞捡 4 块矿石，奖励别太高"
} | ConvertTo-Json -Compress

curl.exe -N -X POST http://127.0.0.1:8000/api/v1/messages `
  -H "Content-Type: application/json" `
  --data-binary $body
```

把输出同时存一份，方便后面自动取 `draft_id` / `commit_token`：

```powershell
curl.exe -N -X POST http://127.0.0.1:8000/api/v1/messages `
  -H "Content-Type: application/json" `
  --data-binary $body | Tee-Object -FilePath .\act1.sse
```

### 8.3 取出 `draft_id` 和 `commit_token`

```powershell
$raw = Get-Content .\act1.sse -Raw
$match = [regex]::Match(
  $raw,
  'event: awaiting_confirmation\s+data: (\{.*?\})\r?\n\r?\n',
  'Singleline'
)
if (-not $match.Success) { throw "没有 awaiting_confirmation：先看 act1.sse 里的 error 事件" }
$await = $match.Groups[1].Value | ConvertFrom-Json
$draftId = $await.draft_id
$token = $await.commit_token
"$draftId  $($await.target_path)  expires_in=$($await.expires_in)"
```

### 8.4 确认写盘

```powershell
$confirm = @{ commit_token = $token; note = "manual" } | ConvertTo-Json -Compress
curl.exe -N -X POST "http://127.0.0.1:8000/api/v1/drafts/$draftId/confirm" `
  -H "Content-Type: application/json" `
  --data-binary $confirm
```

正常应看到 `status(confirmed)` → `status(committed)` → `committed` → `done(status="committed")`。

### 8.5 幕 3 / 幕 4

每次只换 `message`，然后重新执行 8.2 的 `curl.exe ... --data-binary $body`（建议把输出存成 `act3.sse` / `act4.sse`，并把 8.3 的 `Get-Content` 文件名一起改掉）：

```powershell
# 幕 3
$body = @{ session_id = $sid; message = "给铁匠老陈加一条 6 级支线，去矿洞捡 4 块矿石，奖励改成 150 金币" } | ConvertTo-Json -Compress

# 幕 4（反例）
$body = @{ session_id = $sid; message = "给新手村 1 级任务加 1000 金币奖励" } | ConvertTo-Json -Compress
```

每次拿到新的 `draft_id` / `token` 后重复 8.3–8.4。幕 4 的确认应返回 `E_REWARD_RANGE`，且**不要**再确认第二次。

### 8.6 编码与解析注意

- PowerShell 7 默认以 UTF-8 传参；Windows PowerShell 5.1 若中文乱码，先 `chcp 65001`，或把 `$body` 写到文件再用 `--data-binary "@.\body.json"`：

  ```powershell
  [IO.File]::WriteAllText("$PWD\body.json", $body, [Text.UTF8Encoding]::new($false))
  curl.exe -N -X POST http://127.0.0.1:8000/api/v1/messages `
    -H "Content-Type: application/json" `
    --data-binary "@.\body.json"
  ```

- `.sse` 文件里是原始帧；`event:` 和 `data:` 分两行，帧之间是空行，不要用 `ConvertFrom-Json` 直接读整份文件。
- 不想手动复制 token 时，直接用 `python scripts/demo_live.py`；它做的就是 8.1–8.5 这些请求。

---

## ⑨ 实测记录（2026-11-19）

> 真实 API + 真实 key，`GCC_MCP_MODE=write`，base_url `http://127.0.0.1:8000`，`python scripts/demo_live.py` 三幕全跑。下面的数字来自当次终端输出与 trace，不是推算。

### 9.1 三幕实测

| 幕 | 预览耗时 | 确认耗时 | 结果 | 关键字段 | tokens |
| --- | --- | --- | --- | --- | --- |
| 1 正常生成 | 2123 ms | 61 ms | `committed` | `commit_id=8f276e108890`、`revision=6af7afc410c3`、`backup=None`（新建任务无旧版本） | 2983 |
| 2 奖励改 150 | 1676 ms | 25 ms | `committed` | `commit_id=364227ac8115`、`revision=dfe2c7450972` | 3043 |
| 3 反例 1000 金币 | 1782 ms | 28 ms | `rejected`（零写入） | `E_REWARD_RANGE` ×2：`C_REWARD_GOLD_01`（Lv1 上限 200）、`C_REWARD_GOLD_MIN_01`（60–200）；另 1 条 warning | 2777 |

总耗时 **5.7 s**。

**成片**：[`demo/recording/gcc-demo-2026-11-19.mp4`](../demo/recording/gcc-demo-2026-11-19.mp4)（2026-11-19 录屏，14.75 MB · 00:18.9 · 1758×930）；交付索引见 [`demo/README.md`](../demo/README.md)。

三条主 trace：`tr_0ccc2eb68f48428387f054e546012c6a`、`tr_c109b7758c174f5eb71dd1a4eb50ccd2`、`tr_7176440d2e84473dae59fd1d55acaad5`；
三条确认 trace：`tr_73910da0c1c44b97af1d9eae98e2d007`、`tr_f66230d7f28f4f10997790b95f1b2d71`、`tr_b3349d3c36b9454dafb419c76586f052`。
都在 `traces/traces.jsonl` 里，按 `trace_id` 过滤即可回放。

### 9.2 排练前故障与修复（复现用）

| # | 现象 | 原因 | 处置 |
| --- | --- | --- | --- |
| 1 | 首跑 `E_IO: 无法创建运行目录 data\backups`（WinError 5） | `data/` 的 ACL 不允许创建子目录，`backups/` 建不出来；与代码逻辑无关 | 不去改仓库 ACL，改用隔离数据目录（#2） |
| 2 | 写入无处落地 | 同上 | `$env:GCC_TASKS_DIR="$env:TEMP\gcc-demo\data\tasks"`，三幕全通；写入落在临时目录，但预览里的 `target_path` 仍显示 `data/tasks/...` 相对路径（路径是相对结构，别被误导） |
| 3 | 启动报 `WinError 10048`（端口被占） | 上一次 uvicorn 残留进程还在监听 8000 | 先停旧进程再启动，别直接换端口，否则脚本和 `healthz` 会对不上 |

可复现的启动顺序：

```powershell
# ① 先看 8000 有没有残留监听；有就先停掉那个 PID
Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue

# ② 同一个窗口里设两个环境变量，再启动
$env:GCC_MCP_MODE  = "write"
$env:GCC_TASKS_DIR = "$env:TEMP\gcc-demo\data\tasks"   # 绕开 data/ 的 ACL
python -m uvicorn gameconfig_copilot.api.main:app --host 127.0.0.1 --port 8000

# ③ 另开窗口
python scripts/demo_live.py --base-url http://127.0.0.1:8000
```

用 `GCC_TASKS_DIR` 时，⑤.4 和 1.2 里「`data/tasks` 文件数」的核对要对着 `$env:TEMP\gcc-demo\data\tasks` 数；`traces/` 仍在仓库下。

### 9.3 观察

1. **`validation(ok=true)` 是设计，不是漏检**：草案阶段只跑 Schema，业务规则在 `/confirm` 写盘前的复检里执行——第 3 幕因此才在确认时被 `E_REWARD_RANGE` 拦下，而不是在生成阶段。旁白按 ⑤.3 的说法讲，别承诺“生成时就拒绝”。
2. **`backup=None` 对新建任务是正确的**：幕 1–3 都是新建，没有旧文件可备；只有更新已有任务才会出现 `data/backups/...`。不要为了画面去翻备份目录。
3. **一次拒绝可能带多条错误**：第 3 幕实际是 `E_REWARD_RANGE` ×2（上限 `C_REWARD_GOLD_01` + 区间 `C_REWARD_GOLD_MIN_01`）再加 1 条 warning。口播只念第一条即可，别说“只有一个错误”。
4. **`cost_cny=0.0` 是展示问题**：tokens 是真实计数（约 3k/次），费用字段没接上。录屏时不必念成本，只念 tokens。
5. **比预期快得多**：全流程 5.7 s，远低于 ⑥ 原先按 30–90 s 留的缓冲。5 分钟正片实际上全靠旁白和指屏幕撑，垫场句基本用不上；反过来也别为了填时长重跑。

### 9.4 结论

> 三幕在真实 API + write 模式下全部符合预期：两次 `committed` 落盘、一次 `rejected` 零写入，事件序列、错误码、`commit_id` 形态与 ⑤ 的契约级预期逐条对上——可以按本包直接录屏，只需先处理 `data/` ACL 与 8000 端口这两个环境坑。

**录屏后仍需回答**

1. 哪一幕的模型等待最长？下次要不要把该幕的垫场句写进提词卡？（本次实测预览 1.7–2.1 s/幕，几乎不用等。）
2. 反例是真的被服务端拒绝，还是模型自己修正了？——**本次已有答案：服务端拒绝**（`rejected` + `E_REWARD_RANGE`，零写入），模型没有自行把 1000 压到 200。
3. 画面里有没有出现 key、用户名、绝对路径？回看时逐帧检查开头和结尾。

---

> 再次说明：本文件描述的手动链路已于 **2026-11-19 用真实 key 实跑通过**（三幕结果见 ⑨）。沙箱内仍然不能启动 API、不能调真实模型、不能写 `data/`，所以换机器复现时先按 9.2 处理隔离目录与 8000 端口；脚本另通过 `python -B -c "import ast; ast.parse(...)"` 与 `ruff check`。第一次跑建议先 `--skip-confirm` 走通网络与事件流，再全量跑。执行报错请把完整终端输出（含 `trace_id`、错误码）贴回。
