---
name: database-maintenance
description: 使用来源脚本采集、写入、备份并调度量化研究数据。
---

# 数据库维护

本 skill 维护上游行情、财务、行业、社交数据的采集、规范化、分区写入、备份与定时调度。来源脚本按数据源组织，统一调度器管理运行时间、交易日、日志和通知。

属于 [Quant Tools](../../README.md)。依赖安装和项目配置见插件说明，配置模板见 [.env.example](../../.env.example)。下文 `<skill-root>` 指本 `SKILL.md` 所在目录；使用安装后的实际目录执行脚本。

## 目录结构

```text
<skill-root>/
├── SKILL.md
├── agents/
│   └── openai.yaml
├── references/
│   ├── data-catalog.md
│   ├── eastmoney.md
│   ├── huggingface.md
│   ├── ricequant.md
│   └── xueqiu.md
├── scripts/
│   ├── common/
│   │   └── scheduler.py
│   ├── eastmoney/
│   │   ├── client.py
│   │   ├── fetch_eastmoney_intraday_changes.py
│   │   └── fetch_eastmoney_stock_sentiment.py
│   ├── huggingface/
│   │   └── sync_huggingface_bucket.py
│   ├── ricequant/
│   │   ├── client.py
│   │   └── fetch_ricequant.py
│   ├── xueqiu/
│   │   ├── client.py
│   │   ├── fetch_xueqiu_cube_rebalance.py
│   │   ├── fetch_xueqiu_hot_rank.py
│   │   ├── fetch_xueqiu_social_count.py
│   │   └── fetch_xueqiu_status.py
│   └── scheduler.py
```

`client.py` 封装来源认证、请求、重试和原始记录解析；`fetch_*.py` 完成数据清洗、主键检查和 Hive 分区 Parquet upsert。`scripts/scheduler.py` 按配置调用对应采集脚本的 `main()`，并管理交易日检查、运行日志、邮件通知及上游完成标记。Hugging Face 备份脚本按表白名单同步数据目录。

## 参考资料

- 数据表字段、分区和写入契约：[数据表目录](references/data-catalog.md)
- RiceQuant 下载与本地导入：[RiceQuant 采集说明](references/ricequant.md)
- 雪球身份凭据、代理、热度和组合数据：[雪球采集说明](references/xueqiu.md)
- EastMoney 个股情绪与盘中异动：[EastMoney 采集说明](references/eastmoney.md)
- Hugging Face Storage Bucket 备份与恢复：[备份说明](references/huggingface.md)

## 运行方式

从目标项目根目录使用虚拟环境中的 Python 运行脚本，路径使用正斜杠。也可通过 `--project-root <project-root>` 指定项目；配置从该目录的 `.env` 读取，数据、日志和其他相对路径均以该目录为基准。长期调度使用统一调度器：

```shell
.venv/Scripts/python <skill-root>/scripts/scheduler.py
```

查看启用的任务与调度时间：

```shell
.venv/Scripts/python <skill-root>/scripts/scheduler.py --list-jobs
```

对指定日期执行全部启用任务的 dry-run：

```shell
.venv/Scripts/python <skill-root>/scripts/scheduler.py --run-once --date 2026-06-04 --dry-run
```

选择任务执行一次：

```shell
.venv/Scripts/python <skill-root>/scripts/scheduler.py --run-once eastmoney_intraday_changes xueqiu_cube_rebalance --date 2026-06-04 --dry-run
```

也可直接运行单个来源脚本，通过 `--help` 查看日期、数据根目录或采集范围。EastMoney、雪球和备份的数据目录依次读取本次 `--data-root`、来源专用目录设置、`DATA`、默认 `data`；RiceQuant 使用 `DATASET_PATH`，未设置时读取 `DATA`。

## 调度任务

| 任务 | 默认时间 | 日期条件 | 入口脚本 |
| --- | --- | --- | --- |
| `eastmoney_intraday_changes` | `15:05` | A 股交易日 | `scripts/eastmoney/fetch_eastmoney_intraday_changes.py` |
| `xueqiu_cube_rebalance` | `15:30` | A 股交易日 | `scripts/xueqiu/fetch_xueqiu_cube_rebalance.py` |
| `ricequant` | `19:30` | A 股交易日 | `scripts/ricequant/fetch_ricequant.py` |
| `eastmoney_stock_sentiment` | `22:00` | 配置日期 | `scripts/eastmoney/fetch_eastmoney_stock_sentiment.py` |
| `xueqiu_social_count` | `22:30` | 配置日期 | `scripts/xueqiu/fetch_xueqiu_social_count.py` |
| `xueqiu_hot_rank` | `23:00` | 配置日期 | `scripts/xueqiu/fetch_xueqiu_hot_rank.py` |
| `xueqiu_status` | `01:30` | 配置日期 | `scripts/xueqiu/fetch_xueqiu_status.py` |
| `huggingface_backup` | `04:00` | 配置日期 | `scripts/huggingface/sync_huggingface_bucket.py` |

调度参数、凭据和通知配置放在项目根目录 `.env`：

```dotenv
DATABASE_MAINTENANCE_ENABLED_JOBS=all
DATABASE_MAINTENANCE_RUN_ON_START=0
DATABASE_MAINTENANCE_NOTIFY=1
DATABASE_MAINTENANCE_TIMEZONE=Asia/Shanghai
DATABASE_MAINTENANCE_MISFIRE_GRACE_SECONDS=21600
DATABASE_MAINTENANCE_CALENDAR_RETRY_SECONDS=60
DATABASE_MAINTENANCE_EMAIL_TO=<notification-recipient>
HUGGINGFACE_TOKEN=<private-token>
HUGGINGFACE_BUCKET_ID=<user-or-org>/<private-bucket>
HUGGINGFACE_BACKUP_DATA_ROOT=data
HUGGINGFACE_BACKUP_QUOTES_MIN_START_DATE=2015-01-01
HUGGINGFACE_BACKUP_SCHEDULE_TIME=04:00
HUGGINGFACE_BACKUP_MAX_RETRIES=5
HUGGINGFACE_BACKUP_RETRY_SLEEP_SECONDS=60
DATABASE_MAINTENANCE_UPSTREAM_JOB=ricequant
DATABASE_MAINTENANCE_UPSTREAM_READY_FILE=.tmp/database-maintenance/upstream_ready.json
SENDER=<smtp-sender>
EMAIL_PASSWORD=<smtp-password>
SMTP_SERVER=<smtp-server>
SMTP_PORT=465
SMTP_USE_SSL=1
```

`DATABASE_MAINTENANCE_ENABLED_JOBS` 和 `DATABASE_MAINTENANCE_RUN_ON_START` 支持 `all` 或逗号分隔的任务名。dry-run 时可将 `DATABASE_MAINTENANCE_NOTIFY=0` 设为静默运行。`DATABASE_MAINTENANCE_MISFIRE_GRACE_SECONDS` 设置长任务跨过后续任务时间时的补运行窗口。邮件发件凭据和收件人保存在 `.env`。


## 调度与数据约定

- 统一调度器将所有来源任务的输出写入 `.tmp/database-maintenance/DatabaseMaintenanceScheduler.log`，保留任务名、运行标识和日期；RiceQuant 不再生成独立文件日志。来源脚本单独运行时输出到终端。
- 每项任务完成后发送通知，主题包含任务名、Success/Failed 和目标日期；正文只列 Status、起止时间、耗时及实际完成的数据表/路径/写入输入条数；成功状态为 Success，失败状态显示错误摘要，不附完整日志、用途或计数说明行。upsert 条数包含覆盖已有记录，不代表净新增；失败前已完成写入仍保留摘要，尚未完成的写入不计数。跳过的任务只记录日志。
- A 股交易日任务使用交易日历计算可运行日期与下一次唤醒时间。日历获取失败时暂缓这些任务，按 `DATABASE_MAINTENANCE_CALENDAR_RETRY_SECONDS` 设置的正整数秒间隔重试；其余任务继续运行。服务恢复后按补运行窗口检查待执行任务。单次执行中日历不可用的任务返回非零状态。
- 使用 DuckDB 与 Hive 分区 Parquet 完成数据写入，并延续各表现有主键、分区与 upsert 语义。
- 备份任务通过表白名单选择数据，以 `hf buckets sync` 同步 Storage Bucket；生产备份纳入审查过的数据表。
- 小范围验证使用 `_test` 表名；验证结束后清理测试表。
- 调整采集路径、字段、分区、数据契约或调度任务时，同步更新本 skill 和对应来源参考文档。
- 将凭据、数据、日志、临时文件与下载包留在 Git 忽略范围，仓库提交脚本和说明文档。

## 验证

修改脚本后运行语法检查、调度清单和 dry-run：

```shell
.venv/Scripts/python -m compileall <skill-root>/scripts
.venv/Scripts/python <skill-root>/scripts/scheduler.py --list-jobs
.venv/Scripts/python <skill-root>/scripts/huggingface/sync_huggingface_bucket.py --include-table eastmoney_intraday_changes --dry-run
.venv/Scripts/python <skill-root>/scripts/scheduler.py --run-once --date 2026-06-04 --dry-run
```

涉及认证、采集和写入逻辑时，按来源参考文档使用本地样本或小范围任务验证，并记录真实执行范围、数据分区、日志和通知结果。

## 交付记录

汇报修改的数据来源、实际运行的任务、是否写入数据、日志位置、通知状态、最新分区或样本行数，以及仍需处理的账号、代理、WebDriver 或数据源服务依赖。

配置 `DATABASE_MAINTENANCE_UPSTREAM_JOB` 后，该任务成功时调度器原子写入包含任务名、状态、目标日期和完成时间的 JSON 就绪标记，供项目下游调度读取。来源采集参数见对应参考文档。
