# 雪球数据采集

## 任务与共享模块

| 文件 | 用途 |
| --- | --- |
| `scripts/xueqiu/client.py` | 采集任务共用的账号选择、代理、请求客户端及 Token 失效通知。 |
| `scripts/xueqiu/accounts.py`、`scripts/xueqiu/login.py` | 账号读取和 Token 写回、账号身份验证与浏览器二维码操作。 |
| `scripts/fetch_xueqiu_social_count.py` | 采集股票关注数与讨论数并写入日分区。 |
| `scripts/fetch_xueqiu_hot_rank.py` | 采集热榜结果并写入日分区。 |
| `scripts/fetch_xueqiu_status.py` | 构建候选股票池，采集雪球状态评论并写入日分区。 |
| `scripts/fetch_xueqiu_cube_rebalance.py` | 采集组合调仓买卖次数并写入日分区。 |
| `scripts/xueqiu_relogin.py` | 执行验号、邮件二维码续登与结果汇报流程。 |


## 访问配置

在项目根目录 `.env` 中配置：

```dotenv
# 手机号,二维码收件邮箱,HTTP 代理 URL,Token
XUEQIU_ACCOUNT_0650='13800000650,a@example.com,http://127.0.0.1:18081,token_a'
XUEQIU_ACCOUNT_B='13800000002,b@example.com,,'
XUEQIU_PROXY=http://127.0.0.1:7890
```

ID 使用大写字母、数字或下划线。手机号和邮箱必填。第三字段为可选的 HTTP 代理 URL。账号代理优先，留空时使用采集命令的 `--proxy` 或全局 `XUEQIU_PROXY`，均未设定时直连。已配置的代理连接失败时报告错误。采集、验号和浏览器续登按同一账号选择代理。代理的监听端口和出站路由由代理服务管理。Token 留空时，该账号可用二维码续登，取得 Token 后才参与采集。只按前三个英文逗号拆分账号记录，Token 中其余逗号原样保留。状态和评论分页始终使用同一账号。

### 从旧配置迁移

1. 核对每个手机号实际对应的邮箱、代理 URL 和 Token，再按上述格式把这些字段写入同一条账号记录。
2. 从项目 `.env` 移除旧的手机号、邮箱、Token、节点及请求身份并行列表。保留共享代理和邮件服务配置。已有账号记录如果第三字段是节点编号，将其替换为对应的 HTTP 代理 URL，或清空以使用默认行为。旧的 Clash 控制器配置可删除。
3. 采集、验号和续登每次都从项目 `.env` 读取账号记录。账号记录以文件为准，下一次调用会读取续登写回的新值。单账号采集使用 `--account <ID>`。

可在 `.env` 设置采集和验号参数，例如：

```dotenv
XUEQIU_STATUS_WORKERS=2
XUEQIU_STATUS_BATCH_SIZE=20
XUEQIU_STATUS_SLEEP=1.0
XUEQIU_STATUS_RETRIES=1
XUEQIU_STATUS_MAX_PAGES=20
XUEQIU_STATUS_CANDIDATE_LIMIT=200
XUEQIU_RELOGIN_TIMEOUT=600
XUEQIU_RELOGIN_VERIFY_TIMEOUT=20
```

采集脚本默认使用业务时区的当天；热榜和社交计数读取当前快照，`--date` 不会把实时结果变成历史数据。

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
uv run --locked python "<path-to-skill>/scripts/fetch_xueqiu_social_count.py" --date 2026-06-02 --codes 002384.SZ --data-root data --table-name xueqiu_social_count_test --workers 1 --sleep 0 --timeout 20 --retries 0
uv run --locked python "<path-to-skill>/scripts/fetch_xueqiu_hot_rank.py" --date 2026-06-02 --data-root data --table-name xueqiu_hot_rank_test --timeout 20 --retries 0 --hot-size 5 --hot-rank-types 12
uv run --locked python "<path-to-skill>/scripts/fetch_xueqiu_status.py" --date 2026-06-01 --codes 688008.SH --data-root data --table-name xueqiu_status_test --workers 1 --batch-size 1 --count 20 --max-pages 1 --sleep 0 --timeout 20 --retries 0
uv run --locked python "<path-to-skill>/scripts/fetch_xueqiu_cube_rebalance.py" --date 2026-06-02 --codes 002384.SZ --data-root data --table-name xueqiu_cube_rebalance_test --workers 1 --sleep 0 --timeout 20 --retries 0
```

验证结束后，清理 `_test` 数据表。

## 正式运行参数

```sh
uv run --locked python "<path-to-skill>/scripts/fetch_xueqiu_social_count.py" --date <YYYY-MM-DD> --data-root data --table-name xueqiu_social_count --workers 2 --sleep 0.2 --timeout 30 --retries 2 --failure-retry-rounds 1 --failure-retry-workers 1 --failure-retry-sleep 1.0 --failure-retry-timeout 30 --failure-retry-retries 3
uv run --locked python "<path-to-skill>/scripts/fetch_xueqiu_hot_rank.py" --date <YYYY-MM-DD> --data-root data --table-name xueqiu_hot_rank --timeout 20 --retries 1 --hot-rank-types 12 22
uv run --locked python "<path-to-skill>/scripts/fetch_xueqiu_status.py" --date <YYYY-MM-DD> --data-root data --table-name xueqiu_status --workers 1 --batch-size 10 --sleep 2 --timeout 30 --retries 2 --candidate-limit 500
uv run --locked python "<path-to-skill>/scripts/fetch_xueqiu_cube_rebalance.py" --date <YYYY-MM-DD> --data-root data --table-name xueqiu_cube_rebalance --workers 2 --sleep 0.2 --timeout 20 --retries 1 --failure-retry-rounds 1 --failure-retry-workers 1 --failure-retry-sleep 2.0 --failure-retry-timeout 30 --failure-retry-retries 5
```

状态采集默认使用候选股票范围，候选来源包括热榜、讨论数和组合调仓活跃度。全市场范围通过 `--full-universe` 显式开启，适合经过评估的专项任务。社交计数使用双 worker 首轮采集，失败证券再以单 worker 和较长间隔重试；成功记录先写入，仍有失败时脚本返回失败状态。组合调仓同样先快速采集，再用保守参数重试失败证券，成功记录先 upsert。

状态任务遇到已有日期分区时，确认后用 `--replace-existing` 重跑。

## 雪球重登录

### 它做什么

`--check-accounts` 逐账号核对手机号并获取雪球用户名。Token 缺失、被雪球拒绝或手机号不匹配时，可以通过 Chrome 二维码重登录。接口暂时不可用或掩码手机号匹配多个账号时，脚本记录失败原因并继续处理其他账号。重登录成功后，新 Token 按账号键写回项目配置，供下次运行读取。

结果输出到终端；任一账号失败时，脚本以非零状态退出。

二维码是否过期以雪球页面为准。需要换码时，回复最近一封二维码邮件并在本次回复正文中只写 `刷新`。等待时间从第一封邮件发出后开始计算，默认十分钟。刷新沿用同一个截止时间，超时后结束本次登录并保留原 token。

### 需要哪些文件

| 文件 | 做什么 |
| --- | --- |
| `scripts/xueqiu_relogin.py` | 一次性检查全部账号或续登单个账号，按需发送二维码并保存 Token。 |
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

XUEQIU_RELOGIN_TIMEOUT=600
```

二维码发往该账号记录中的收件地址。`EMAIL_PASSWORD` 填邮箱授权码。

上面的示例使用 QQ 邮箱发信。其他邮箱还需设置 `XUEQIU_IMAP_SERVER` 为该邮箱的收信服务器。收信账号和授权码默认与发信相同。分开配置时，将 `XUEQIU_IMAP_USERNAME` 设为轮询邮箱的地址，二维码邮件会将该地址用作回复地址。

### 手动登录

在项目根目录运行：

```sh
uv run --locked python "<path-to-skill>/scripts/xueqiu_relogin.py" --account 0650 --env-path .env --timeout 600
```

然后：

1. 等待二维码邮件。
2. 用雪球 App 扫码并确认；同一部手机上可以先保存图片，再从相册识别。
3. 如果二维码过期，回复最近一封二维码邮件，正文只写 `刷新`。
4. 收到新邮件后再扫码。

程序核对登录后的手机号，再在写入锁内复核账号记录。通过后只更新该账号行的 Token，并保留文件权限。

### 一次性检查全部账号

```sh
uv run --locked python "<path-to-skill>/scripts/xueqiu_relogin.py" --check-accounts
```

单账号续登仍可使用 `--account <ID>` 或已登记的完整手机号。`--timeout` 覆盖 `.env` 中的 `XUEQIU_RELOGIN_TIMEOUT`。

## 故障处理

- HTTP 405 按可能的 WAF 限制排查；先降低并发、增加请求间隔，再确认 Token 和该账号使用的代理连通性。
- 任务从最后完成批次恢复，重跑前先处理当前失败条件。
- 生产 token 保持失效提醒可用。
