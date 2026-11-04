# EastMoney 数据采集

## 脚本分工

| 文件 | 用途 |
| --- | --- |
| `scripts/eastmoney/client.py` | 读取 `.env`、规范股票代码、加载证券范围并请求个股情绪和盘中异动接口。 |
| `scripts/eastmoney/fetch_eastmoney_stock_sentiment.py` | 清洗个股情绪数据，按 `(code, date)` 写入 Hive 分区 Parquet。 |
| `scripts/eastmoney/fetch_eastmoney_intraday_changes.py` | 清洗盘中异动事件，按 `(code, date, event_time, change_type, source_rank)` 写入 Hive 分区 Parquet。 |
| `scripts/scheduler.py` | 在统一调度器中登记两项任务，管理日期、日志和通知。 |

## 身份与请求配置

可在项目根目录 `.env` 中设置请求身份和请求头：

| 变量 | 用途 |
| --- | --- |
| `EASTMONEY_STOCK_CARD_USER_ID` | `stockCard` 请求参数 `args.uid`。 |
| `EASTMONEY_MONI_UT_TOKEN` | `stockCard` 请求头 `EM-UT`。 |
| `EASTMONEY_MONI_CT_TOKEN` | `stockCard` 请求头 `EM-CT`。 |
| `EASTMONEY_APP_DEVICE_ID` | `stockCard` 请求参数 `deviceId` 和请求头 `EM-MD`。 |
| `EASTMONEY_APP_GT` | `stockCard` 请求头 `EM-GT`。 |
| `EASTMONEY_APP_USER_AGENT` | `stockCard` 请求头 `User-Agent`。 |
| `EASTMONEY_WEBVIEW_USER_AGENT` | `stockrank` 与盘中异动请求头 `User-Agent`。 |
| `EASTMONEY_STOCK_CARD_CLIENT_VERSION` | `stockCard` 请求参数 `clientVersion` 和请求头 `EM-VER`。 |
| `EASTMONEY_APP_PLATFORM` | `stockCard` 请求参数 `client` 和请求头 `EM-OS`。 |
| `EASTMONEY_MONI_PROXY` | EastMoney HTTP(S) 代理地址。 |

匿名身份可用于默认请求；接口出现认证限制时，可配置有效的 App token 和设备请求头。

## 任务参数

```dotenv
EASTMONEY_DATA_ROOT=data
EASTMONEY_SCHEDULE_TIME=22:00
EASTMONEY_STOCK_SENTIMENT_TABLE=eastmoney_stock_sentiment
EASTMONEY_STOCK_SENTIMENT_WORKERS=8
EASTMONEY_STOCK_SENTIMENT_SLEEP=0.2
EASTMONEY_INTRADAY_CHANGES_TABLE=eastmoney_intraday_changes
EASTMONEY_INTRADAY_CHANGES_SCHEDULE_TIME=15:05
EASTMONEY_INTRADAY_CHANGES_SCHEDULE_DAYS=weekday
EASTMONEY_INTRADAY_CHANGES_PAGE_SIZE=5000
EASTMONEY_INTRADAY_CHANGES_SLEEP=0.2
```

个股情绪任务默认每天 `22:00` 运行；盘中异动任务默认在 A 股交易日 `15:05` 运行。两个任务的调度状态由统一调度器维护。调度器在表级任务完成后发送状态邮件，邮件正文以表格列出起止时间、耗时和各表写入条数/路径，不附全量日志；日志文件保存在 `.tmp/database-maintenance/`。

## 数据表

| 表 | 写入键 | 主要内容 |
| --- | --- | --- |
| `eastmoney_stock_sentiment` | `code, date` | `tape_z`、`tape_d`、`tape_type`、`hot_rank` 及用户画像比例字段。 |
| `eastmoney_intraday_changes` | `code, date, event_time, change_type, source_rank` | 22 类盘中异动事件、事件时间、类型代码、市场标记和来源排序。 |

情绪数据由 `stockCard` 多空看盘、`stockrank/getHisList` 热榜和 `stockrank/getHisProfileList` 用户画像接口组成。热榜和用户画像接口每次返回滚动窗口，按写入键 upsert 并保留最新非空值。盘中异动表保留来源排序 `source_rank`。`ingested_at` 记录采集时间。

## 运行命令

小范围情绪采集示例：

```sh
uv run --locked python "<path-to-skill>/scripts/eastmoney/fetch_eastmoney_stock_sentiment.py" --date 2026-06-03 --codes 600519.SH --data-root data --table-name eastmoney_stock_sentiment_test --workers 1 --sleep 0 --timeout 20 --retries 0
```

盘中异动采集示例：

```sh
uv run --locked python "<path-to-skill>/scripts/eastmoney/fetch_eastmoney_intraday_changes.py" --date <YYYY-MM-DD> --data-root data --table-name eastmoney_intraday_changes_test --change-types 大笔买入 大笔卖出 --sleep 0 --timeout 20 --retries 0
```

正式运行时使用生产表名和经过确认的请求范围。调度清单与一次性运行统一使用：

```sh
uv run --locked python "<path-to-skill>/scripts/scheduler.py" --list-jobs
uv run --locked python "<path-to-skill>/scripts/scheduler.py" --run-once eastmoney_intraday_changes eastmoney_stock_sentiment --date <YYYY-MM-DD> --dry-run
```

## 故障处理

- 个股情绪采集优先保持较低并发；需要提升吞吐时，先适当增加请求间隔，再逐步调整并发。
- `stockCard` 返回认证错误时，在 `.env` 更新 EastMoney App token 和设备请求头。
- 盘中异动分页行数偏少时，检查 `data.tc`、页面大小和事件类型代码映射，再执行补采。
- 采集任务使用 `_test` 表进行小范围核验，完成后清理测试分区。
