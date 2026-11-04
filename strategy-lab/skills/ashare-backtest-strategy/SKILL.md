---
name: ashare-backtest-strategy
description: Build and validate deterministic local A-share backtests using next-bar matching, T+1, board lots, commission, slippage, and ledger-based evaluation.
---

# A-share Backtest Strategy

复用项目已有的 Python 3.12+ 环境，按下文安装缺少的依赖。`<skill-root>` 为本 Skill 的安装目录。

## 安装

先检查项目是否已有所需包，缺少或需更新版本时，在已有 uv 项目目录运行。将 `<tag>` 替换为所用插件的版本标签：

```sh
uv add "base-strategy @ git+https://github.com/ppoak/quanthub-plugins.git#subdirectory=strategy-lab/skills/base-strategy" "ashare-backtest-strategy @ git+https://github.com/ppoak/quanthub-plugins.git#subdirectory=strategy-lab/skills/ashare-backtest-strategy" --tag "<tag>"
```

仅使用 Python 包无需安装插件。保留项目的 `pyproject.toml` 和 `uv.lock`，其他环境运行 `uv sync --locked`。不要重建已有环境或覆盖项目配置。

Excel 导出所需依赖见 [Base Strategy 安装说明](../base-strategy/SKILL.md#安装)。

本 skill 提供本地 A 股历史回测后端，负责订单提交、下一 bar 撮合、T+1 与整手约束、费用和滑点、持仓估值、账本事实记录及回测评估。基于 `base-strategy` 的通用接口，使用 `AShareClient` 构建策略循环，由 `AShareExchange` 驱动撮合，并通过 `AShareLedger` 查询订单、成交和账户快照。

```python
from ashare_backtest_strategy import AShareAccount, AShareClient, AShareExchange, AShareLedger, FixedRateCommission

account = AShareAccount(account_id="strategy", init_cash=1_000_000)
ledger = AShareLedger()
client = AShareClient(client_id="strategy")
exchange = AShareExchange(commission=FixedRateCommission(rate=0.0003, min_commission=5.0, stamp_duty=0.001))
exchange.register(account, ledger=ledger)
client.connect(exchange, account_id=account.account_id)

for timestamp, bar in bars:
    exchange.advance(bar, timestamp=timestamp)  # Match orders already queued.
    account.update_prices(bar["close"])  # Mark open positions in one pass.
    # Submit today's strategy intent.

executions = ledger.executions
```

日线 bar 的事件顺序为：先用当前 bar 撮合此前排队的订单，再更新持仓估值，随后提交新的卖出与买入意图。新订单在下一次 `advance()` 撮合；数量默认向下取整到 100 股，并执行 T+1 可卖约束。通过 `AShareClient` 下达意图，从 `AShareLedger` 读取确定的成交事实，再用 `base_strategy.evaluate_executions` 评估。

先在短区间核对订单与成交的关联键、费用、账户净值和账本回放。完整生命周期及 A 股规则见 [回测参考](references/backtest.md)。

## 编写策略循环

将每个交易日的全市场行情整理成以证券代码为索引、包含 OHLCV 列的 DataFrame。循环内先调用 `exchange.advance(bar, timestamp=...)` 撮合上一轮提交的订单，再用 `account.update_prices(bar["close"])` 更新估值，然后只使用当前及更早数据计算信号并通过 Client 提交目标仓位。新订单在下一根 bar 撮合；循环末尾导出订单、成交，并可用相同价格矩阵做独立评估。

[均线策略循环示例](examples/example_strategy_loop.py) 用确定性日线展示信号时点、下一 bar 成交、目标仓位和账本评估；[生命周期示例](examples/example_lifecycle.py) 与 [导出契约示例](examples/example_export_contracts.py) 检查 T+1、部分成交、自定义滑点、字段别名和成交回放。[条件单示例](examples/example_conditional_orders.py) 展示触发价格、成交量约束、止损限价后续成交、部分成交和订单有效期。示例均可运行：

```sh
uv run --locked python "<skill-root>/examples/example_strategy_loop.py"
uv run --locked python -m unittest discover -s "<skill-root>/examples" -p "example_*.py"
```
