---
name: xueqiu-strategy
description: 基于 Base Strategy 编写雪球模拟投资组合策略，在本地撮合订单并将成交和资金划转同步到雪球记账账户。
---

# Xueqiu Strategy

使用 Python 3.12+，复用项目已有的 uv 环境。四个组件分别继承 `BaseAccount`、`BaseClient`、`BaseExchange` 和 `BaseLedger`；事实存储与评估复用 `base-strategy`。

## 安装

从 [Releases](https://github.com/ppoak/quanthub-plugins/releases) 选择已发布的版本，在用户项目中安装对应 wheel：

```sh
uv add "base-strategy @ https://github.com/ppoak/quanthub-plugins/releases/download/base-strategy@<base-version>/base_strategy-<base-version>-py3-none-any.whl" "xueqiu-strategy @ https://github.com/ppoak/quanthub-plugins/releases/download/xueqiu-strategy@<xueqiu-version>/xueqiu_strategy-<xueqiu-version>-py3-none-any.whl"
```

文件名以 Release 附件为准。未发布时，可将两个 wheel 地址换成源码检出目录中的 `base-strategy`、`xueqiu-strategy` Skill 路径。保留项目的 `pyproject.toml` 和 `uv.lock`，其他环境运行 `uv sync --locked`。Excel 导出需安装 `base-strategy[excel]`。

## 运行

```python
from xueqiu_strategy import XueqiuAccount, XueqiuClient, XueqiuExchange, XueqiuLedger

account = XueqiuAccount(account_id="strategy", init_cash=100_000)
client = XueqiuClient(token="offline")
ledger = XueqiuLedger(client=client, gid="offline")
exchange = XueqiuExchange(client, gid="offline", sync_transactions=False)
exchange.register(account, ledger=ledger)
client.connect(exchange, account_id=account.account_id)

# bars yields (timestamp, DataFrame indexed by code).
for timestamp, bar in bars:
    exchange.advance(bar, timestamp=timestamp)
    # Generate signals from current data after matching earlier orders.
    # client.buy("000001.SZ", 100, created_at=timestamp)
    # client.sell("000001.SZ", 100, created_at=timestamp)
```

- `Client` 提交买卖和撤单；`Exchange.advance()` 撮合此前提交的订单，自动更新 `Account` 和 `Ledger`，再按收盘价更新持仓市值。
- bar 以 `000001.SZ`、`600000.SH` 等代码为索引，提供 `open/high/low/close/volume`；行情和信号由用户项目提供。
- 默认只在本地运行。需要远端同步时，读取用户项目 `.env` 中的 token 和 `gid`，明确目标账户及写入意图，再设置 `sync_transactions=True`。`offline` 仅用于本地示例。
- 净值评估使用 `base_strategy.evaluate_executions(ledger.executions, prices=prices, initial_cash=account.init_cash)`；`Store` 可汇集账本中的终态订单和成交。输出保存在用户项目中。

[运行示例](examples/example_strategy_loop.py) 默认不联网，也不包含测试代码：

```sh
uv run --locked python "<path-to-skill>/examples/example_strategy_loop.py"
```

远端认证、资金划转、历史查询、同步失败处理及原始实现出处见[雪球接入参考](references/xueqiu.md)。
