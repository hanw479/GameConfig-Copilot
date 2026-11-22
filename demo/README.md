# GameConfig Copilot · 演示交付包

> **项目定位**：面向游戏策划的任务配置 Agent —— 检索有依据、生成有结构、写入有闸门（BM25 + RRF 混合检索 · 显式状态机编排 · MCP 风格工具层 · 确定性三层校验 · 两阶段原子写入 · JSONL 全链路轨迹）。
> **演示日期**：2026-11-19（真实 DeepSeek key + 真实 API + `GCC_MCP_MODE=write`，`python scripts/demo_live.py` 三幕实跑，见 [demo-rehearsal.md §⑨](../docs/demo-rehearsal.md)）。
> **本目录只做交付归档**：成片、评测快照与索引；文档一律用链接指回仓库原文，不复制正文。

---

## 1. 文件清单

| 路径 | 内容 | 大小 | 备注 |
| --- | --- | --- | --- |
| [`recording/gcc-demo-2026-11-19.mp4`](recording/gcc-demo-2026-11-19.mp4) | 演示成片（终版录屏） | **14.75 MB**（15,463,376 字节） | 时长 **00:18.9**（18.884 s）· 分辨率 **1758×930** · 修改时间 **2026-11-19 16:36:18** · 二进制不入库（见 §5） |
| [`../docs/demo-script.md`](../docs/demo-script.md) | 5 分钟演示脚本：分镜、旁白、录屏清单、预期输出 | 28.4 KB | 演示编排以此为准（幕 1–4 与时间轴） |
| [`../docs/demo-rehearsal.md`](../docs/demo-rehearsal.md) | Live 排练包：启动命令、现场检查清单、兜底话术、⑨ 实测记录 | 28.3 KB | 排练与录屏 runbook；实测数字来自当次终端与 trace |
| [`reports/fpvr-baseline.json`](reports/fpvr-baseline.json) | 生成评测快照：FPVR **0.9130**（21/23 首轮）、`post_repair_pass_rate` 1.0 | 38.5 KB | 2026-11-18 19:13:19 生成，`suite=agent` / `requests_v2`，与 `reports/` 原件逐字节一致 |
| [`reports/retrieval-baseline.json`](reports/retrieval-baseline.json) | 检索评测快照：recall@5 **0.775**、mrr@10 0.7625、ndcg@10 0.7382 | 22.6 KB | 2026-11-18 19:13:23 生成，`dense_backend=noop`（BM25 + RRF） |
| [`../README.md`](../README.md) | 项目总览：定位、能力、指标、目录结构、快速开始 | 21.7 KB | 交付入口文档 |

> 视频元数据由 MP4 容器（`moov/mvhd`、`avc1` 采样项）读取；本机**没有 ffprobe/ffmpeg**，故未用 ffprobe 复核。时长与 Windows 属性面板显示的 `00:00:18` 一致。

---

## 2. 复现命令

在**仓库根目录**执行（PowerShell；`gcc` 不可用时用 `python -m gameconfig_copilot.cli` 代替）：

```powershell
# 0) 依赖（一次）
pip install -e ".[dev,api,rag]"

# 1) 建索引：离线、不需要 key；应打印 chunk 总数与 dense 降级信息
gcc ingest --source data

# 2) 起 API：写模式必须显式设进程环境变量（改 .env 不生效）
$env:GCC_MCP_MODE = "write"
python -m uvicorn gameconfig_copilot.api.main:app --host 127.0.0.1 --port 8000

# 3) 另开一个窗口，跑三幕演示（两次 committed + 一次 rejected）
python scripts/demo_live.py
```

现场校验与备用参数：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/healthz | ConvertTo-Json -Compress   # mcp.mode 必须是 write
python scripts/demo_live.py --skip-confirm              # 只预览、零写入，开录前彩排
python scripts/demo_live.py --base-url http://127.0.0.1:8001 --timeout 300   # 换端口 / 模型慢
```

判读：退出码 `0` = 三幕全部符合预期；`2` = 健康检查 / 建会话 / 写模式检查失败（先看 `healthz.mcp.mode`）。

---

## 3. 环境与已知限制

| 项 | 现状 | 对演示的影响 |
| --- | --- | --- |
| **DeepSeek 真实 key** | 本机 `.env` 已配置真实 key（`deepseek-chat`）；无 key 时 `/messages` 返回 `event: error` + `E_INTERNAL`，**不伪造生成结果** | key 绝不出现在画面里；换机器前先补 `$env:GCC_LLM_API_KEY` |
| **dense=noop** | 向量检索未启用，`dense_available=False`，实际为 BM25 + RRF 混合 | 检索快照里 `dense_backend=noop` 是**预期**，不是故障；讲解时说“降级为 BM25 + RRF” |
| **MCP 本地注册表** | MCP 风格工具层是**进程内本地注册表**，不是远程 MCP server；`healthz.mcp.connected` 直接映射 `write_enabled` | `connected:true` 只代表写模式已开；默认 `readonly`，`/confirm` 会 403 `E_WRITE_DISABLED` |
| **演示数据隔离目录** | `data/` 的 ACL 不允许建子目录（`data/backups` → WinError 5），实跑用 `$env:GCC_TASKS_DIR="$env:TEMP\gcc-demo\data\tasks"` | 写盘落在临时目录，但预览里的 `target_path` 仍显示 `data/tasks/...` 相对路径，**别被路径误导**；`traces/` 仍在仓库下 |
| **端口 8000** | 上一次 uvicorn 残留会让启动报 `WinError 10048` | 先停旧进程再启动，不要直接换端口（脚本与 `healthz` 会对不上） |
| **视频工具** | 本机无 ffprobe/ffmpeg | 视频元数据由容器解析；需要精确码率/帧率时另装 ffprobe 复核 |
| **沙箱/CI** | 不能起 API、不能调真实模型、不能写 `data/` | 真实链路只能在本机终端跑；脚本在沙箱里只做 `ast.parse` + `ruff check` |

---

## 4. 试拍说明

`C:\Users\jzlnh\Videos\屏幕录制\屏幕录制 2026-11-19 163508.mp4`（35.8 MB，16:35:08）是同日的**早期试拍**，未纳入交付：文件仍留在原目录，未移动、未删除、未复制进本仓库。交付只认 `recording/gcc-demo-2026-11-19.mp4`。

---

## 5. 归档约定

- `recording/*.mp4` 已在 `.gitignore` 中忽略：**视频不进版本库**，随目录手工分发。
- `reports/*.json` 是交付快照，随仓库保留（`demo/reports/` 不受根级 `/reports/` 忽略规则影响）。
- 文档不复制正文，全部以相对链接指回 `docs/`、`scripts/`、仓库 `README.md`。
- 只整理了文件位置，**未改动** `src/`、`tests/`、`data/` 与任何代码。

```
demo/
├── README.md                  # 本文件（交付索引）
├── recording/
│   └── gcc-demo-2026-11-19.mp4
├── reports/
│   ├── fpvr-baseline.json
│   └── retrieval-baseline.json
└── materials/
    └── README.md              # 演示相关文档的链接索引
```
