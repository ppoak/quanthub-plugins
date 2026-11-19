# 雪球数据采集

## 脚本分工

| 文件 | 用途 |
| --- | --- |
| `scripts/xueqiu/client.py` | 读取凭据、规范证券代码、绑定代理节点与请求身份、调用雪球 API，并处理 token 失效通知。 |
| `scripts/xueqiu/fetch_xueqiu_social_count.py` | 采集股票关注数与讨论数并写入日分区。 |
| `scripts/xueqiu/fetch_xueqiu_hot_rank.py` | 采集热榜结果并写入日分区。 |
| `scripts/xueqiu/fetch_xueqiu_status.py` | 采集雪球状态评论并写入日分区。 |
| `scripts/xueqiu/fetch_xueqiu_cube_rebalance.py` | 采集组合调仓买卖次数并写入日分区。 |
| `scripts/xueqiu/email_relogin.py` | 保存认证状态，并按 ID token 临期状态执行邮件二维码重登录；同时提供可被外部 scheduler 调用的函数和 CLI。 |
| `scripts/scheduler.py` | 统一登记雪球采集与认证任务，按设定日期和交易日运行。 |

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
XUEQIU_LOGIN_PHONES=<phone1>,<phone2>
XUEQIU_LOGIN_EMAILS=<login-mailbox1>,<login-mailbox2>
XUEQIU_RELOGIN_SCHEDULE_TIME=22:00
XUEQIU_RELOGIN_SCHEDULE_DAYS=all
XUEQIU_RELOGIN_THRESHOLD_DAYS=5
XUEQIU_RELOGIN_TIMEOUT=600
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

```sh
uv run --locked python "<path-to-skill>/scripts/xueqiu/fetch_xueqiu_social_count.py" --date 2026-06-02 --codes 002384.SZ --data-root data --table-name xueqiu_social_count_test --workers 1 --sleep 0 --timeout 20 --retries 0
uv run --locked python "<path-to-skill>/scripts/xueqiu/fetch_xueqiu_hot_rank.py" --date 2026-06-02 --data-root data --table-name xueqiu_hot_rank_test --timeout 20 --retries 0 --hot-size 5 --hot-rank-types 12
uv run --locked python "<path-to-skill>/scripts/xueqiu/fetch_xueqiu_status.py" --date 2026-06-01 --codes 688008.SH --data-root data --table-name xueqiu_status_test --workers 1 --batch-size 1 --count 20 --max-pages 1 --sleep 0 --timeout 20 --retries 0
uv run --locked python "<path-to-skill>/scripts/xueqiu/fetch_xueqiu_cube_rebalance.py" --date 2026-06-02 --codes 002384.SZ --data-root data --table-name xueqiu_cube_rebalance_test --workers 1 --sleep 0 --timeout 20 --retries 0
```

验证结束后，清理 `_test` 数据表。

## 正式运行参数

```sh
uv run --locked python "<path-to-skill>/scripts/xueqiu/fetch_xueqiu_social_count.py" --date <YYYY-MM-DD> --data-root data --table-name xueqiu_social_count --workers 2 --sleep 0.2 --timeout 30 --retries 2 --failure-retry-rounds 1 --failure-retry-workers 1 --failure-retry-sleep 1.0 --failure-retry-timeout 30 --failure-retry-retries 3
uv run --locked python "<path-to-skill>/scripts/xueqiu/fetch_xueqiu_hot_rank.py" --date <YYYY-MM-DD> --data-root data --table-name xueqiu_hot_rank --timeout 20 --retries 1 --hot-rank-types 12 22
uv run --locked python "<path-to-skill>/scripts/xueqiu/fetch_xueqiu_status.py" --date <YYYY-MM-DD> --data-root data --table-name xueqiu_status --workers 1 --batch-size 10 --sleep 2 --timeout 30 --retries 2 --candidate-limit 500
uv run --locked python "<path-to-skill>/scripts/xueqiu/fetch_xueqiu_cube_rebalance.py" --date <YYYY-MM-DD> --data-root data --table-name xueqiu_cube_rebalance --workers 2 --sleep 0.2 --timeout 20 --retries 1 --failure-retry-rounds 1 --failure-retry-workers 1 --failure-retry-sleep 2.0 --failure-retry-timeout 30 --failure-retry-retries 5
```

状态采集默认使用候选股票范围，候选来源包括热榜、讨论数和组合调仓活跃度。全市场范围通过 `--full-universe` 显式开启，适合经过评估的专项任务。社交计数使用双 worker 首轮采集，失败证券再以单 worker 和较长间隔重试；成功记录先写入，仍有失败时脚本返回失败状态。组合调仓同样先快速采集，再用保守参数重试失败证券，成功记录先 upsert。

状态任务遇到已有日期分区时，通过 `--replace-existing` 显式执行有意重跑。统一调度器按日期调度任务，并保留脚本默认的分区保护行为。

## 雪球重登录

### 它做什么

重登录会打开 Chrome，把二维码发到邮箱，等用户扫码，然后把新的 token 写回 `.env`。

二维码过期后不会自己发送新码。请回复对应的登录邮件，正文单独写 `刷新`，程序才会发送新二维码。每次登录最多等十分钟，十分钟没有完成就结束本次登录。

### 需要哪些文件

| 文件 | 做什么 |
| --- | --- |
| `scripts/xueqiu/email_relogin.py` | 登录一个账号，发送二维码并保存 token。 |
| `scripts/scheduler.py` | 按时间启动重登录。 |
| `.env` | 保存邮箱、手机号和 token。不要提交到 Git。 |
| `.tmp/database-maintenance/xueqiu-auth/browser` | 保存每个账号的 Chrome 登录状态。不要提交到 Git。 |

每个手机号都有自己的 profile。下次登录时，程序会先试着使用这个账号以前的登录状态。

### 配置

在项目根目录的 `.env` 中填写：

```dotenv
SENDER=<发送邮件的邮箱>
EMAIL_PASSWORD=<邮箱授权码>
SMTP_SERVER=smtp.qq.com

XUEQIU_LOGIN_PHONES=<手机号1>,<手机号2>
XUEQIU_LOGIN_EMAILS=<邮箱1>,<邮箱2>
XUEQIU_TOKENS=<token1>,<token2>

XUEQIU_RELOGIN_SCHEDULE_TIME=22:00
XUEQIU_RELOGIN_SCHEDULE_DAYS=all
XUEQIU_RELOGIN_THRESHOLD_DAYS=5
XUEQIU_RELOGIN_TIMEOUT=600
```

相同位置的内容属于同一个账号：第一个手机号、第一封邮箱、第一枚 token 是一组。三行的数量必须一样。`EMAIL_PASSWORD` 填邮箱授权码，不是邮箱登录密码。

### 手动登录

在项目根目录运行：

```sh
uv run --locked python "<path-to-skill>/scripts/xueqiu/email_relogin.py" "<完整手机号>" --env-path .env --timeout 600
```

然后：

1. 等待二维码邮件。
2. 用雪球 App 扫码并确认。
3. 如果二维码过期，回复原邮件，正文只写 `刷新`。
4. 收到新邮件后再扫码。

程序会检查登录后的手机号。手机号不对时，不会覆盖原来的 token。

### 按时间自动登录

查看任务：

```sh
uv run --locked python "<path-to-skill>/scripts/scheduler.py" --list-jobs
```

手动运行一次重登录：

```sh
uv run --locked python "<path-to-skill>/scripts/scheduler.py" --run-once xueqiu_relogin --date <YYYY-MM-DD>
```

调度时间由 `.env` 中的 `XUEQIU_RELOGIN_SCHEDULE_TIME` 和
`XUEQIU_RELOGIN_SCHEDULE_DAYS` 决定。

## 故障处理

- HTTP 405 按可能的 WAF 限制排查；先降低并发、增加请求间隔，再确认 token 和 Clash 节点状态。
- 任务从最后完成批次恢复，重跑前先处理当前失败条件。
- 生产 token 保持失效提醒可用。
