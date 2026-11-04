---
name: eastmoney-strategy
description: Build an EastMoney remote simulated-portfolio mirror using provider-confirmed accounts, orders, and executions.
---

# EastMoney Strategy

运行前按[插件说明](../../README.md)准备目标项目环境。`<skill-root>` 为本 Skill 的安装目录。

本 skill 提供东财模拟组合接入后端，负责刷新账户镜像、提交策略订单意图、同步平台订单与成交，并将已确认的交易事实记录到账本。账户、订单和成交状态以东财服务端返回为准。运行参数统一从项目根目录的 `.env` 读取；可配置项和默认值见[东财接入参考](references/eastmoney.md)。二维码登录使用 Python 标准库 HTTP；Excel 导出使用插件的 `excel` 依赖组。

东财服务端负责接单和撮合。本地分别创建 `EMAccount`、`EMClient`、`EMExchange`、`EMLedger`，再注册并连接。通过 `exchange.account(account_id)` 刷新账户镜像；通过 Client 操作订单；Exchange 查询当日平台订单与成交，Ledger 保存终态订单和已确认成交。

```python
from eastmoney_strategy import EMAccount, EMClient, EMExchange, EMLedger

dotenv = ".env"
account = EMAccount.from_env(dotenv_path=dotenv)
ledger = EMLedger.from_env(dotenv_path=dotenv)
client = EMClient.from_env(dotenv_path=dotenv, live=False)
exchange = EMExchange.from_env(dotenv_path=dotenv)
exchange.register(account, ledger=ledger)
client.connect(exchange, account_id=account.account_id)
```

## 接入东财模拟组合

1. 用已登录账号读取组合列表，确认目标组合 ID；将认证值和 `EASTMONEY_MONI_ACCOUNT_ID` 写入本地 `.env`。
2. 用同一组凭据创建 Account、Client、Exchange 和 Ledger；注册后连接 Client，再用 `exchange.account(account_id)` 刷新现金和持仓镜像。
3. 策略使用刷新后的账户状态和明确的报价计算目标仓位，通过 `EMClient.order_target_value` 或 `order_target_percent` 提交意图。传入已确定的价格可避免函数内部额外取价；不传价格时 Client 会请求实时报价。
4. 用 `exchange.fetch_orders` 和 `fetch_executions` 查询平台返回的当日状态，只把终态订单和已确认成交记录到 Ledger。请求成功或 dry-run 预览不代表订单被接受或成交。

`fetch_orders` 与 `fetch_executions` 会自动读取后续页面直到返回不足一页；`rec_cnt` 表示每页条数，`rec_idx` 表示起始记录偏移。

准备好模拟盘凭据、目标组合和明确下单意图后，可按下面方式连接模拟组合。`live=True` 会发送模拟盘委托或撤单；开发验证使用 [固定响应示例](examples/example_mock_portfolio_loop.py)，由本地固定响应覆盖接口交互。

```python
from eastmoney_strategy import EMAccount, EMClient, EMExchange, EMLedger

dotenv = ".env"
account = EMAccount.from_env(dotenv_path=dotenv)
account_id = account.account_id
ledger = EMLedger.from_env(dotenv_path=dotenv)
exchange = EMExchange.from_env(dotenv_path=dotenv)
client = EMClient.from_env(dotenv_path=dotenv, live=True)

exchange.register(account, ledger=ledger)
client.connect(exchange, account_id=account_id)
snapshot = exchange.account(account_id)  # Refresh provider state.
quote = client.quote("000001")
# Compute the signal, then submit only the user-authorized target order.
client.order_target_percent("000001", 0.10, price=quote.latest)

for order in exchange.fetch_orders(account_id=account_id):
    exchange.record_order(order)
for execution in exchange.fetch_executions(account_id=account_id):
    ledger.record_execution(execution)
```

运行固定响应示例或检查示例行为：

```powershell
.venv/Scripts/python "<skill-root>/examples/example_mock_portfolio_loop.py"
.venv/Scripts/python -m unittest discover -s "<skill-root>/examples" -p "example_*.py"
```

认证、刷新与异常处理见 [东财后端参考](references/eastmoney.md)。
