# 雪球数据采集

## 脚本分工

| 文件 | 用途 |
| --- | --- |
| `scripts/xueqiu/client.py` | 读取凭据、规范证券代码、绑定代理节点与请求身份、调用雪球 API，并处理 token 失效通知。 |
| `scripts/xueqiu/fetch_xueqiu_social_count.py` | 采集股票关注数与讨论数并写入日分区。 |
| `scripts/xueqiu/fetch_xueqiu_hot_rank.py` | 采集热榜结果并写入日分区。 |
| `scripts/xueqiu/fetch_xueqiu_status.py` | 采集雪球状态评论并写入日分区。 |
| `scripts/xueqiu/fetch_xueqiu_cube_rebalance.py` | 采集组合调仓买卖次数并写入日分区。 |
| `scripts/scheduler.py` | 统一登记四项任务，按设定日期和交易日运行采集脚本。 |

## 访问配置

在项目根目录 `.env` 中配置：

```dotenv
XUEQIU_TOKENS=<token1>,<token2>
XUEQIU_PROXY=http://127.0.0.1:7890
XUEQIU_CLASH_CONTROLLER=http://127.0.0.1:<controller-port>
XUEQIU_CLASH_SECRET=<controller-secret>
XUEQIU_CLASH_GROUP=GLOBAL
XUEQIU_CLASH_NODE_INDICES=<index1>,<index2>
XUEQIU_USER_AGENTS=<ua1>,<ua2>
XUEQIU_DEVICE_IDS=<device-id1>,<device-id2>
```

`XUEQIU_TOKENS` 与 `XUEQIU_CLASH_NODE_INDICES` 按位置配对。每个 token 对应一个代理节点；节点数量少于 token 数时，脚本返回配置错误。状态和评论分页请求期间，每只股票保持 token、节点、User-Agent 和设备 ID 的固定组合。Token 失效与任务运行通知使用共享 SMTP 配置和收件人。

默认调度设置：

```dotenv
XUEQIU_CUBE_REBALANCE_SCHEDULE_TIME=15:30
XUEQIU_CUBE_REBALANCE_SCHEDULE_DAYS=weekday
XUEQIU_SOCIAL_COUNT_SCHEDULE_TIME=22:30
XUEQIU_HOT_RANK_SCHEDULE_TIME=23:00
XUEQIU_STATUS_SCHEDULE_TIME=01:30
XUEQIU_SCHEDULE_DAYS=all
XUEQIU_STATUS_WORKERS=2
XUEQIU_STATUS_BATCH_SIZE=20
XUEQIU_STATUS_SLEEP=1.0
XUEQIU_STATUS_RETRIES=1
XUEQIU_STATUS_MAX_PAGES=20
XUEQIU_STATUS_CANDIDATE_LIMIT=200
```

调度器在同一任务执行时间跨越后续任务的情况下，按 `DATABASE_MAINTENANCE_MISFIRE_GRACE_SECONDS` 配置的窗口补运行到期任务。

## 数据表

| 表 | 内容与写入键 |
| --- | --- |
| `xueqiu_social_count` | 全市场股票的关注数和讨论数；`(code, date)`。 |
| `xueqiu_hot_rank` | 热榜结果；默认类型 `12` 和 `22`。 |
| `xueqiu_status` | 每条状态的精简评论字段；`status_id`。 |
| `xueqiu_cube_rebalance` | 组合买入、卖出次数；`(code, date)`。 |

热榜的 `source_rank` 表示榜内名次，`value_increment` 表示热度增量，`rank_change` 表示名次变化。状态表保存清理 HTML 后的 `text_plain`。雪球数据以 Hive `date` 路径保存，日期从分区目录读取。组合调仓接口返回近期历史窗口，一次采集可能更新多个历史日期分区。

## 小范围采集

```powershell
./.venv/Scripts/python <skill-root>/scripts/xueqiu/fetch_xueqiu_social_count.py --date 2026-06-02 --codes 002384.SZ --data-root data --table-name xueqiu_social_count_test --workers 1 --sleep 0 --timeout 20 --retries 0
./.venv/Scripts/python <skill-root>/scripts/xueqiu/fetch_xueqiu_hot_rank.py --date 2026-06-02 --data-root data --table-name xueqiu_hot_rank_test --timeout 20 --retries 0 --hot-size 5 --hot-rank-types 12
./.venv/Scripts/python <skill-root>/scripts/xueqiu/fetch_xueqiu_status.py --date 2026-06-01 --codes 688008.SH --data-root data --table-name xueqiu_status_test --workers 1 --batch-size 1 --count 20 --max-pages 1 --sleep 0 --timeout 20 --retries 0
./.venv/Scripts/python <skill-root>/scripts/xueqiu/fetch_xueqiu_cube_rebalance.py --date 2026-06-02 --codes 002384.SZ --data-root data --table-name xueqiu_cube_rebalance_test --workers 1 --sleep 0 --timeout 20 --retries 0
```

验证结束后，清理 `_test` 数据表。

## 正式运行参数

```powershell
./.venv/Scripts/python <skill-root>/scripts/xueqiu/fetch_xueqiu_social_count.py --date <YYYY-MM-DD> --data-root data --table-name xueqiu_social_count --workers 2 --sleep 0.2 --timeout 30 --retries 2 --failure-retry-rounds 1 --failure-retry-workers 1 --failure-retry-sleep 1.0 --failure-retry-timeout 30 --failure-retry-retries 3
./.venv/Scripts/python <skill-root>/scripts/xueqiu/fetch_xueqiu_hot_rank.py --date <YYYY-MM-DD> --data-root data --table-name xueqiu_hot_rank --timeout 20 --retries 1 --hot-rank-types 12 22
./.venv/Scripts/python <skill-root>/scripts/xueqiu/fetch_xueqiu_status.py --date <YYYY-MM-DD> --data-root data --table-name xueqiu_status --workers 1 --batch-size 10 --sleep 2 --timeout 30 --retries 2 --candidate-limit 500
./.venv/Scripts/python <skill-root>/scripts/xueqiu/fetch_xueqiu_cube_rebalance.py --date <YYYY-MM-DD> --data-root data --table-name xueqiu_cube_rebalance --workers 2 --sleep 0.2 --timeout 20 --retries 1 --failure-retry-rounds 1 --failure-retry-workers 1 --failure-retry-sleep 2.0 --failure-retry-timeout 30 --failure-retry-retries 5
```

状态采集默认使用候选股票范围，候选来源包括热榜、讨论数和组合调仓活跃度。全市场范围通过 `--full-universe` 显式开启，适合经过评估的专项任务。社交计数使用双 worker 首轮采集，失败证券再以单 worker 和较长间隔重试；成功记录先写入，仍有失败时脚本返回失败状态。组合调仓同样先快速采集，再用保守参数重试失败证券，成功记录先 upsert。

状态任务遇到已有日期分区时，通过 `--replace-existing` 显式执行有意重跑。统一调度器按日期调度任务，并保留脚本默认的分区保护行为。

## 统一调度

```powershell
./.venv/Scripts/python <skill-root>/scripts/scheduler.py --list-jobs
./.venv/Scripts/python <skill-root>/scripts/scheduler.py --run-once xueqiu_cube_rebalance xueqiu_social_count xueqiu_hot_rank xueqiu_status --date <YYYY-MM-DD> --dry-run
```

采集脚本输出任务级运行记录；统一调度器在任务结束后发送成功或失败通知，并将当前任务日志附在邮件正文中。客户端在检测到认证错误时发送 token 失效提醒。

## 故障处理

- HTTP 405 按可能的 WAF 限制排查；先降低并发、增加请求间隔，再确认 token 和 Clash 节点状态。
- 任务从最后完成批次恢复，重跑前先处理当前失败条件。
- 生产 token 保持失效提醒可用。
