# 东财模拟组合接入参考

## 组件职责

分别创建 `EMAccount`、`EMClient`、`EMExchange` 和 `EMLedger`，注册账户与账本，再连接客户端。`EMAccount` 保存服务端返回的现金和持仓镜像，`EMClient` 提交策略委托并查询可买卖数量，`EMExchange` 刷新账户并查询当日订单与成交，`EMLedger` 保存已确认的历史交易事实。

通过 `exchange.account(account_id)` 刷新账户镜像。下单前使用明确报价计算目标仓位；订单状态和成交结果以服务端查询结果为准。只有完整的账户响应成功后才替换本地账户镜像。开发验证使用固定响应或 mock。

## 环境配置

在项目根目录 `.env` 中维护以下参数。`from_env()` 和二维码登录会读取这些值；进程环境变量优先于 `.env`。省略或留空的运行参数使用表中默认值。

| 参数 | 默认值 | 用途 |
|---|---:|---|
| `EASTMONEY_MONI_USER_ID` | 空 | 登录用户 ID；未填写时可从 `PI` 推导 |
| `EASTMONEY_MONI_UT_TOKEN` | 空 | 登录会话令牌 |
| `EASTMONEY_MONI_CT_TOKEN` | 空 | 请求校验令牌 |
| `EASTMONEY_MONI_PI` | 空 | 登录身份 Cookie |
| `EASTMONEY_MONI_ACCOUNT_ID` | 空 | 目标模拟组合 ID，供 `EMAccount.from_env()` 使用 |
| `EASTMONEY_MONI_USER_AGENT` | 空 | HTTP 请求的 User-Agent |
| `EASTMONEY_MONI_APP_VERSION` | 空 | 客户端应用版本请求头 |
| `EASTMONEY_MONI_PLATFORM` | 空 | 客户端平台请求头 |
| `EASTMONEY_MONI_PROXY` | 空 | HTTP/HTTPS 代理地址 |
| `EASTMONEY_MONI_HTTP_TIMEOUT_SECONDS` | `20` | 单次 HTTP 请求超时秒数 |
| `EASTMONEY_MONI_HTTP_RETRIES` | `1` | 请求失败后的重试次数 |
| `EASTMONEY_MONI_HTTP_RETRY_SLEEP_SECONDS` | `1` | 重试间隔秒数 |
| `EASTMONEY_MONI_PAGE_SIZE` | `20` | 订单、成交查询的单页记录数 |
| `EASTMONEY_MONI_PENDING_ORDER_PAGE_SIZE` | `200` | 当日未完成订单查询的单页记录数 |
| `EASTMONEY_MONI_HOLDINGS_PAGE_SIZE` | `200` | 账户持仓查询的单页记录数 |
| `EASTMONEY_MONI_LOGIN_TIMEOUT_SECONDS` | `180` | 扫码登录等待秒数 |
| `EASTMONEY_MONI_LOGIN_POLL_INTERVAL_SECONDS` | `1` | 扫码状态轮询间隔秒数 |
| `EASTMONEY_MONI_QR_PATH` | `.tmp/eastmoney_login_qr.png` | 二维码图片保存路径 |

认证值可通过 `EMClient.login()` 扫码写入 `.env`。目标组合 ID 由用户从登录账号的组合列表中确认后填写。
