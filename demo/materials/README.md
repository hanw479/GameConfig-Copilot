# 演示素材索引（只放链接，不放正文）

> 本目录按约定**不复制文档正文**：演示相关的说明都在 `docs/` 原文里维护，这里只给出入口，避免抄一份出来再各自漂移。
> 交付主索引见 [`../README.md`](../README.md)；演示日期 2026-11-19。

## 演讲与排练

| 文档 | 用途 | 什么时候看 |
| --- | --- | --- |
| [docs/demo-script.md](../../docs/demo-script.md) | 5 分钟脚本：幕 1–4 分镜、逐句旁白、录屏清单与预期输出 | 背稿、对时间轴、录屏前最后一遍 |
| [docs/demo-rehearsal.md](../../docs/demo-rehearsal.md) | Live 排练包：启动命令、现场检查清单、失败兜底话术、**⑨ 实测记录（2026-11-19）** | 开录前 10 分钟照着跑；出状况翻 9.2 |
| [docs/demo-rehearsal-evidence.md](../../docs/demo-rehearsal-evidence.md) | 实测证据：真实 SSE、耗时/token、落盘与 span 树、零写入证明 | 被追问“数字哪来的”时 |
| [scripts/demo_live.py](../../scripts/demo_live.py) | 三幕演示脚本（只发 HTTP，不直接写 `data/`） | 复现与彩排 |

## 背景与问答

| 文档 | 用途 |
| --- | --- |
| [README.md](../../README.md) | 项目总览：定位、能力、指标、目录结构、快速开始 |
| [docs/01-product-outline.md](../../docs/01-product-outline.md) | 产品定位与用户价值（“为什么做”） |
| [docs/02-architecture.md](../../docs/02-architecture.md) | 架构与数据流（“怎么做的”） |
| [docs/05-interview-kit.md](../../docs/05-interview-kit.md) | 面试/评审问答口径，含指标防被追问的说明 |
| [docs/04-task-checklist.md](../../docs/04-task-checklist.md) | 任务清单与交付边界 |

## 数据快照

| 文件 | 说明 |
| --- | --- |
| [`../reports/fpvr-baseline.json`](../reports/fpvr-baseline.json) | 生成评测快照：FPVR 0.9130、post_repair_pass_rate 1.0 |
| [`../reports/retrieval-baseline.json`](../reports/retrieval-baseline.json) | 检索评测快照：recall@5 0.775、mrr@10 0.7625、ndcg@10 0.7382 |

> 以上两个 JSON 是 2026-11-18 的评测产物；演示（2026-11-19）用的是同一套代码与语料基线。
