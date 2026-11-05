
# A 股回测流程

分别创建 `AShareAccount`、`AShareClient`、`AShareExchange` 和 `AShareLedger`，将账户与账本注册到交易所，再连接客户端。订单在后续 `advance()` 的日线行情中撮合；客户端连接后可直接提交订单。

同一证券在每根 bar 中共享成交量上限，按订单队列顺序分配给所有账户；只有实际成交扣减额度，下一根 bar 重新计量。

每日事件顺序应保持明确：先撮合上一交易日提交的订单，再计算持仓和目标权重，依次提交减仓、加仓，最后更新持仓估值。先减仓可以将卖出所得用于后续买入。

~~~python
from ashare_backtest_strategy import (
    AShareAccount,
    AShareClient,
    AShareExchange,
    AShareLedger,
    FixedRateCommission,
)
from base_strategy import evaluate_executions

account = AShareAccount(account_id="strategy", init_cash=1_000_000)
ledger = AShareLedger()
client = AShareClient(client_id="strategy")
exchange = AShareExchange(
    commission=FixedRateCommission(
        rate=0.0003,
        min_commission=5.0,
        stamp_duty=0.001,
    ),
)
exchange.register(account, ledger=ledger)
client.connect(exchange, account_id=account.account_id)

for timestamp, bar in bars:
    # bar is indexed by instrument code and has OHLCV columns.
    exchange.advance(bar, timestamp=timestamp)

    # delta_weight and current_weight are strategy-specific functions.
    deltas = delta_weight(timestamp)
    current_weights = current_weight(account, bar)
    target_weights = current_weights.add(deltas, fill_value=0.0).clip(lower=0.0)
    sell_signals = deltas[deltas < 0]
    buy_signals = deltas[deltas > 0]
    close_prices = bar["close"]

    for code in sell_signals.index:
        position = account.positions.get(code)
        if code in bar.index and position is not None and position.shares_held > 0:
            client.order_target_percent(
                code,
                percent=float(target_weights.loc[code]),
                price=float(close_prices.at[code]),
            )

    for code in buy_signals.index:
        if code in bar.index:
            client.order_target_percent(
                code,
                percent=float(target_weights.loc[code]),
                price=float(close_prices.at[code]),
            )

    account.update_prices(close_prices)

orders = ledger.orders_frame()
executions = ledger.executions_frame()
final_snapshot = ledger.snapshot(timestamp=timestamp, prices=close_prices.to_dict())
metrics = evaluate_executions(
    ledger.executions,
    prices=price_matrix,
    initial_cash=account.init_cash,
)
~~~

行情表以证券代码为索引，包含 `open`、`high`、`low`、`close` 和 `volume`。输出写入 `.tmp/` 或 `output/` 等已忽略目录，不要硬编码机器专属的数据路径。

订单在撮合前按当前 bar 时间检查有效期，`timestamp > valid_until` 时记为 `EXPIRED`。`STOPLIMIT` 触发后按限价条件继续等待成交；`TARGETLIMIT` 在触发后的下一根 bar 开始检查限价条件。部分成交的剩余数量沿用同一有效期。

默认下单数量按 `lot_size=100` 向下取整，并执行 T+1 可卖数量限制。交易所使用下一根 `advance()` 日线撮合队列中的订单。策略通过 `AShareClient` 提交交易意图，不直接修改订单、持仓、成交或账本事实。

只更新已有持仓的估值，除非策略明确需要全市场估值。读取 `bar["close"]` 后，用 `account.update_prices(close_prices)` 批量更新持仓估值；信号查询使用 `Series.at`。不要每根 bar 都重放 `ledger.snapshot()`；仅在需要查看某时点或生成最终快照时调用。通过 `orders_frame()` 和 `executions_frame()` 导出订单与成交，并使用相同价格矩阵调用 `base_strategy.evaluate_executions` 进行比较。

可运行的策略循环和生命周期行为检查见 [examples/](../examples/)；运行方式见本 skill 的 [SKILL.md](../SKILL.md)。检查订单与成交非空、每笔成交关联订单、手续费合计一致，以及相同价格口径下账户最终资产与评估结果相符。

`STOP` 与 `TARGET` 在开盘已满足触发条件时使用开盘价，盘中触发时使用触发价，并应用滑点。明确提供的 `volume` 须为有限正数才可成交；省略该字段的简化行情示例按订单剩余数量撮合。
