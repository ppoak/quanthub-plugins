# 雪球数据采集

## 脚本分工

| 文件 | 用途 |
| --- | --- |
| `scripts/xueqiu/client.py` | 读取凭据、规范证券代码、绑定代理节点与请求身份、调用雪球 API，并处理 token 失效通知。 |
| `scripts/xueqiu/fetch_xueqiu_social_count.py` | 采集股票关注数与讨论数并写入日分区。 |
| `scripts/xueqiu/fetch_xueqiu_hot_rank.py` | 采集热榜结果并写入日分区。 |
| `scripts/xueqiu/fetch_xueqiu_status.py` | 采集雪球状态评论并写入日分区。 |
| `scripts/xueqiu/fetch_xueqiu_cube_rebalance.py` | 采集组合调仓买卖次数并写入日分区。 |
| `scripts/xueqiu/email_relogin.py` | 实时核对账号身份，按需通过邮件二维码重登录，并同步更新配置文件和当前进程中的 token。 |
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
XUEQIU_RELOGIN_TIMEOUT=600
XUEQIU_RELOGIN_VERIFY_TIMEOUT=20
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

调度器逐账号调用雪球账号绑定和用户信息接口，核对手机号并获取雪球用户名。token 缺失、被雪球拒绝或手机号不匹配时，可通过 Chrome 二维码重登录。接口暂时不可用或掩码手机号匹配多个账号时，任务会记录该账号的失败原因并继续检查其他账号。重登录成功后，新 token 会写入 `.env` 并同步到当前进程。常驻调度器启动时保留环境变量的覆盖值，此后每个雪球任务启动前读取 `.env`，只用文件中发生变化的槽位更新内存凭据，手动续登在下一次雪球任务启动时生效。

调度器邮件会用独立表格行展示每个账号的手机号尾号和雪球用户名。验证或重登录失败时，用户名位置显示失败原因。

二维码是否过期以雪球页面为准。需要换码时，回复最近一封二维码邮件并在本次回复正文中只写 `刷新`。等待时间从第一封邮件发出后开始计算，默认十分钟。刷新沿用同一个截止时间，超时后结束本次登录并保留原 token。

### 需要哪些文件

| 文件 | 做什么 |
| --- | --- |
| `scripts/xueqiu/email_relogin.py` | 登录一个账号，发送二维码并保存 token。 |
| `scripts/scheduler.py` | 按时间启动重登录。 |
| `.env` | 保存邮箱、手机号和 token。不要提交到 Git。 |
| `.tmp/database-maintenance/xueqiu-auth/browser` | 保存每个账号的 Chrome 登录状态。不要提交到 Git。 |

每个手机号都有自己的登录状态目录（profile）。下次运行时，如果仍然登录着且手机号正确，就直接保存 token，不再发二维码邮件。

### 配置

运行脚本的电脑需要先安装 Google Chrome，Mac 和 Windows 都可以。发件邮箱需要开启 SMTP 发信和 IMAP 收信服务，程序通过收信来读取“刷新”回复。

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
XUEQIU_RELOGIN_TIMEOUT=600
```

相同位置的手机号、邮箱和 token 属于同一账号。某项暂缺时保留其空位，避免后续账号错配。调度器会报告缺失的邮箱，缺失的 token 可通过二维码重登录补齐。`EMAIL_PASSWORD` 填邮箱授权码。

上面的示例使用 QQ 邮箱发信。其他邮箱还需设置 `XUEQIU_IMAP_SERVER` 为该邮箱的收信服务器。收信账号和授权码默认与发信相同。分开配置时，将 `XUEQIU_IMAP_USERNAME` 设为轮询邮箱的地址，二维码邮件会将该地址用作回复地址。

### 手动登录

在项目根目录运行：

```sh
uv run --locked python "<path-to-skill>/scripts/xueqiu/email_relogin.py" "<完整手机号>" --env-path .env --timeout 600
```

然后：

1. 等待二维码邮件。
2. 用雪球 App 扫码并确认；同一部手机上可以先保存图片，再从相册识别。
3. 如果二维码过期，回复原邮件，正文只写 `刷新`。
4. 收到新邮件后再扫码。

程序会检查登录后的手机号。手机号不对时，不会覆盖原来的 token。

### 按时间自动登录

定时任务每次实时检查所有已配置的手机号，有 token 但未设置对应手机号时报告配置失败。`--check-accounts` 使用各账号配置中的二维码收件人。`--recipient` 仅用于手动登录单个账号。未登记的号码必须指定空闲槽位和收件人，账号迁移需先更新配置中的手机号和收件人。

查看任务：

```sh
uv run --locked python "<path-to-skill>/scripts/scheduler.py" --list-jobs
```

手动运行一次重登录：

```sh
uv run --locked python "<path-to-skill>/scripts/scheduler.py" --run-once xueqiu_relogin --date <YYYY-MM-DD>
```

调度时间由 `.env` 中的 `XUEQIU_RELOGIN_SCHEDULE_TIME` 和
`XUEQIU_RELOGIN_SCHEDULE_DAYS` 决定；示例表示每天 22:00，按项目设置的时区运行。

查看清单和运行一次都不会开启定时等待。要持续按时间运行，启动调度器并保持它运行：

```sh
uv run --locked python "<path-to-skill>/scripts/scheduler.py"
```

调度器会运行项目中已启用的任务；如果只需要雪球重登录，在 `.env` 中设置 `DATABASE_MAINTENANCE_ENABLED_JOBS=xueqiu_relogin`。

## 故障处理

- HTTP 405 按可能的 WAF 限制排查；先降低并发、增加请求间隔，再确认 token 和 Clash 节点状态。
- 任务从最后完成批次恢复，重跑前先处理当前失败条件。
- 生产 token 保持失效提醒可用。
