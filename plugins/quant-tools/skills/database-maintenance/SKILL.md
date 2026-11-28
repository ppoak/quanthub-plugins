---
name: database-maintenance
description: 使用一次性脚本采集、写入和备份量化研究数据。
---

# 数据库维护

本 Skill 提供上游行情、财务、行业和社交数据的采集、规范化、分区写入与备份。每个脚本独立运行一次后退出，不在插件内配置或启动定时任务。

`<path-to-skill>` 指本 Skill 的安装目录。复用项目已有的 Python 3.12+ 环境；缺少所需库时，在已有 uv 项目目录安装：

```sh
uv add -r "<path-to-skill>/requirements/base.txt"
```

只安装所用来源的额外依赖：

```sh
uv add -r "<path-to-skill>/requirements/ricequant.txt"
uv add -r "<path-to-skill>/requirements/backup.txt"
```

两条命令分别用于 RiceQuant 和 Hugging Face 备份。RiceQuant 还需本机 Chrome、账号和服务器 notebook。无需重新下载插件或创建环境。

从[配置示例](assets/.env.example)将所需项补充到目标项目的 `.env`。原有凭据、数据路径和来源采集参数继续使用；运行时间、星期和任务启用列表不再由本 Skill 读取。

## 参考资料

- 数据表字段、分区和写入契约：[数据表目录](references/data-catalog.md)
- RiceQuant 下载与本地导入：[RiceQuant 采集说明](references/ricequant.md)
- 雪球身份凭据、代理、热度和组合数据：[雪球采集说明](references/xueqiu.md)
- EastMoney 个股情绪与盘中异动：[EastMoney 采集说明](references/eastmoney.md)
- Hugging Face Storage Bucket 备份与恢复：[备份说明](references/huggingface.md)

## 一次性运行入口

从目标项目根目录使用项目 Python 运行下表任一脚本。例如：

```sh
uv run --locked python "<path-to-skill>/scripts/ricequant/fetch_ricequant.py"
```

也可用虚拟环境中的 Python，传入 `--project-root <project-root>`。脚本从该目录读取 `.env`，相对路径以该目录为基准。命令行参数优先于对应环境变量；EastMoney、雪球采集和备份的数据目录按 `--data-root`、来源专用目录、`DATA`、默认 `data` 的顺序选取。RiceQuant 依次读取 `DATASET_PATH`、`DATA`、默认 `data`。

| 原任务 | 直接执行的脚本及必要参数 |
| --- | --- |
| `eastmoney_intraday_changes` | `scripts/eastmoney/fetch_eastmoney_intraday_changes.py` |
| `eastmoney_stock_sentiment` | `scripts/eastmoney/fetch_eastmoney_stock_sentiment.py` |
| `ricequant` | `scripts/ricequant/fetch_ricequant.py` |
| `xueqiu_cube_rebalance` | `scripts/xueqiu/fetch_xueqiu_cube_rebalance.py` |
| `xueqiu_social_count` | `scripts/xueqiu/fetch_xueqiu_social_count.py` |
| `xueqiu_hot_rank` | `scripts/xueqiu/fetch_xueqiu_hot_rank.py` |
| `xueqiu_status` | `scripts/xueqiu/fetch_xueqiu_status.py` |
| `xueqiu_relogin` | `scripts/xueqiu/email_relogin.py --check-accounts` |
| `huggingface_backup` | `scripts/huggingface/sync_huggingface_bucket.py` |

六个 EastMoney 和雪球采集脚本省略 `--date` 时，使用 `DATABASE_MAINTENANCE_TIMEZONE`（默认 `Asia/Shanghai`）下的当天。跨午夜处理上一日数据时，请显式传 `--date YYYY-MM-DD`；该参数不保证实时接口能返回历史快照。

## Hermes 调度迁移

- 在部署机上直接调用表中的脚本。运行时间、交易日判断、任务先后、整项重跑、日志收集和任务结果通知由外部编排负责。
- 如需保持原运行条件，只在 A 股交易日触发盘中异动、雪球组合调仓和 RiceQuant。依赖 `quotes_day` 的任务要等对应数据可用；雪球状态依赖热榜、讨论数和调仓候选，备份应在本批次入库完成后运行。
- 以进程退出码判断执行结果，并检查下游所需分区是否已产出。脚本不再写旧的上游就绪文件，也不发送统一任务摘要邮件；雪球二维码邮件和 Token 失效提醒仍由来源脚本处理。

## 验证

修改后运行语法检查，并用对应脚本的 `--help` 检查参数：

```shell
uv run --locked python -m compileall "<path-to-skill>/scripts"
uv run --locked python "<path-to-skill>/scripts/xueqiu/email_relogin.py" --help
```

涉及认证、采集和写入时，按来源参考文档用本地样本或 `_test` 表验证。记录实际执行范围、分区、行数和未验证的外部服务；完成后清理测试表，不提交凭据、数据或下载包。
