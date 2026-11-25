# 雪球数据采集

## 脚本分工

| 文件 | 用途 |
| --- | --- |
| `scripts/xueqiu/client.py` | 读取凭据、规范证券代码、绑定代理节点与请求身份、调用雪球 API，并处理 token 失效通知。 |
| `scripts/xueqiu/fetch_xueqiu_social_count.py` | 采集股票关注数与讨论数并写入日分区。 |
| `scripts/xueqiu/fetch_xueqiu_hot_rank.py` | 采集热榜结果并写入日分区。 |
| `scripts/xueqiu/fetch_xueqiu_status.py` | 采集雪球状态评论并写入日分区。 |
| `scripts/xueqiu/fetch_xueqiu_cube_rebalance.py` | 采集组合调仓买卖次数并写入日分区。 |
| `scripts/xueqiu/email_relogin.py` | 实时核对账号身份，按需通过邮件二维码重登录，并按账号键更新项目配置文件中的 Token。 |
| `scripts/scheduler.py` | 统一登记雪球采集与认证任务，按设定日期和交易日运行。 |

## 访问配置

在项目根目录 `.env` 中配置：

```dotenv
# 手机号,二维码收件邮箱,Clash 节点编号,Token
XUEQIU_ACCOUNT_0650='13800000650,a@example.com,2,token_a'
XUEQIU_ACCOUNT_B='13800000002,b@example.com,,'
XUEQIU_PROXY=http://127.0.0.1:7890
XUEQIU_CLASH_CONTROLLER=http://127.0.0.1:<controller-port>
XUEQIU_CLASH_SECRET=<controller-secret>
XUEQIU_CLASH_GROUP=GLOBAL
```

ID 使用大写字母、数字或下划线。手机号和邮箱必填，节点是从零开始的编号，留空时走默认代理路由。Token 留空时，该账号可用二维码续登，取得 Token 后才参与采集。只按前三个英文逗号拆分账号记录，Token 中其余逗号原样保留。状态和评论分页始终使用同一账号。

### 从旧配置迁移

1. 核对每个手机号实际对应的邮箱、节点和 Token，再按上述格式把这些字段写入同一条账号记录。
2. 从项目 `.env` 移除旧的手机号、邮箱、Token、节点及请求身份并行列表。保留共享代理、Clash 控制器和邮件服务配置。只存在旧配置时，脚本会提示迁移。
3. 使用新版脚本时，采集、验号和续登每次都从项目 `.env` 读取账号记录。账号记录以文件为准，常驻调度在下一次雪球任务开始时读到独立进程写回的新值。单账号采集使用 `--account <ID>`。

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

调度器逐账号调用雪球账号绑定和用户信息接口，核对手机号并获取雪球用户名。token 缺失、被雪球拒绝或手机号不匹配时，可通过 Chrome 二维码重登录。接口暂时不可用或掩码手机号匹配多个账号时，任务会记录该账号的失败原因并继续检查其他账号。重登录成功后，新 Token 按账号键写回项目配置。下一次雪球任务会读取最新文件。

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

XUEQIU_ACCOUNT_0650='13800000650,a@example.com,,'

XUEQIU_RELOGIN_SCHEDULE_TIME=22:00
XUEQIU_RELOGIN_SCHEDULE_DAYS=all
XUEQIU_RELOGIN_TIMEOUT=600
```

二维码发往该账号记录中的收件地址。`EMAIL_PASSWORD` 填邮箱授权码。

上面的示例使用 QQ 邮箱发信。其他邮箱还需设置 `XUEQIU_IMAP_SERVER` 为该邮箱的收信服务器。收信账号和授权码默认与发信相同。分开配置时，将 `XUEQIU_IMAP_USERNAME` 设为轮询邮箱的地址，二维码邮件会将该地址用作回复地址。

### 手动登录

在项目根目录运行：

```sh
uv run --locked python "<path-to-skill>/scripts/xueqiu/email_relogin.py" --account 0650 --env-path .env --timeout 600
```

然后：

1. 等待二维码邮件。
2. 用雪球 App 扫码并确认；同一部手机上可以先保存图片，再从相册识别。
3. 如果二维码过期，回复最近一封二维码邮件，正文只写 `刷新`。
4. 收到新邮件后再扫码。

程序核对登录后的手机号，再在写入锁内复核账号记录。通过后只更新该账号行的 Token，并保留文件权限。

### 按时间自动登录

定时任务逐个检查已登记账号。也可以使用 `--check-accounts` 运行同一流程。手动续登可按账号 ID 或已登记的完整手机号选择账号。

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
