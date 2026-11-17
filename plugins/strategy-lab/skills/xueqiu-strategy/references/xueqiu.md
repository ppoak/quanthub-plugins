# 雪球接入参考

## 来源与行为

移植自 QuantHub 最后保留完整雪球后端的提交 [`1d0a477`](https://github.com/ppoak/quanthub/commit/1d0a477c5edd5b327c6bdbcaed680dbf99954d5e)，原路径为 `quanthub/trader/xueqiu/`。原实现属于统一的 `trader-strategy` Skill，本插件将其拆为独立包 `xueqiu_strategy` 和 Skill。

接入的是 `tc.xueqiu.com/tc/snowx/MONI/` 模拟投资记账接口。订单在本地撮合，再按成交价格、日期和股数向雪球添加交易记录。这里的 `gid` 是投资记账账户标识；雪球 Cube 组合的数据采集仍属于 Quant Tools。

## 配置与远端同步

将 [配置示例](../assets/.env.example) 中需要的字段补充到用户项目 `.env`，保留已有配置。`XUEQIU_TOKEN` 为登录雪球后的 `xq_a_token` Cookie 值；`XUEQIU_GID` 为目标投资记账账户 ID。HTTP 使用 Python 标准库 `urllib`，默认超时 10 秒，不自动重试写入。

可通过 `client.ensure_success(client.list_investment_groups(), "list groups")` 查询原始账户列表，再由用户选择目标 `gid`。创建、删除账户的方法是 `add_investment_group(name)`、`delete_investment_group(gid)`，只在明确要求时调用。

用户已明确要求远端写入且已确认目标账户后，可运行：

```sh
uv run --locked --env-file .env python "<path-to-skill>/examples/example_strategy_loop.py" --sync-transactions
```

该示例会将示例成交写入所选雪球记账账户，运行前应将示例行情、信号和初始资金替换为用户策略。实际策略也可直接构建四层组件：

```python
import os
from xueqiu_strategy import XueqiuAccount, XueqiuClient, XueqiuExchange, XueqiuLedger

client = XueqiuClient(token=os.environ["XUEQIU_TOKEN"])
gid = os.environ["XUEQIU_GID"]
account = XueqiuAccount(account_id=gid, init_cash=100_000)
ledger = XueqiuLedger(client=client, gid=gid)
exchange = XueqiuExchange(client, gid=gid, sync_transactions=True)
exchange.register(account, ledger=ledger)
client.connect(exchange, account_id=account.account_id)
```

本地初始资金与持仓需与使用的策略起点一致；构建组件不会自动读取或覆盖本地账户。

## 撮合与结算

- `client.buy(code, quantity)`、`sell()` 默认市价单；限价单设置 `exectype="LIMIT", price=...`。数量为正整数。`cancel(order_or_id)` 只撤销本地未完成订单。
- `advance(bar, timestamp=...)` 使用开盘价撮合市价单；限价单在当根 bar 可触及时成交，并保留开盘价优于限价的价格改善。缺失证券或成交量为零时继续等待。
- 按提交顺序逐笔更新余额和持仓。现金或持仓不足的订单进入 `MARGIN` 终态。此后端保留原实现的整数股数及即时可卖规则，不施加 A 股 100 股整手或 T+1 限制；需要这些规则时使用 A 股回测 Skill。
- 在 `advance()` 之后根据当前信息提交新信号，避免把当前 bar 的收盘信息用于当根 bar 撮合。
- 本地手续费默认零，可向构造器传入 `commission(order, price, quantity)`。远端记账的 `commission_rate`、`tax_rate` 通过 `advance()` 或手动成交接口传入，应与本地费用口径一致。
- `record_execution(order, trade_time=..., price=..., commission=...)` 可显式成交一个本地挂单。成交立即更新账户并记账；`sync()` 可重复调用，不会重复扣款或记账。

## 资金与历史

```python
# Positive amounts deposit, negative amounts withdraw.
exchange.transfer("2026-01-05", 500)
exchange.transfer("2026-01-06", -200)

orders = ledger.order_history(row=50)
executions = ledger.execution_history(row=50)
transfers = ledger.transfer_history(row=50)
```

启用远端同步时，`transfer()` 同时写入雪球模拟资金划转记录。历史接口默认只查询；`execution_history(record=True)` 可按远端成交标识去重入账，但不改变账户余额和持仓。本地成交和远端查询的标识来源不同，合并前应核对重复记录，避免将同一成交计入两次。

`client.get_market_price(symbols)` 查询雪球行情；`to_xueqiu_symbol` 和 `from_xueqiu_symbol` 转换证券代码。远端历史成交统一转换为 `.SH/.SZ` 代码。

## 同步失败

本地成交或资金划转先完成结算和入账，再调用远端接口。远端返回失败会抛出 `XueqiuAPIError`，本地事实保留；批次中尚未处理的订单继续留在队列。收到的远端响应保存在成交的 `meta["xueqiu_response"]` 中。

发生失败后暂停策略，先查询雪球历史并核对本地账本，再决定如何补记。不要重跑整批交易或自动重试写入；网络错误可能发生在服务端已经写入之后。关闭远端同步的模式仍可显式调用查询接口，`sync_transactions` 只控制撮合与划转的远端写入。

## 导出

```python
from xueqiu_strategy.stores import to_excel

to_excel(account, ledger, "output/xueqiu.xlsx")
```

需安装 `base-strategy[excel]`，输出包含 `Account`、`Position`、`Order`、`Execution` 工作表。通用订单、成交导出和评估直接复用 Base Strategy。
