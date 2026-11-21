---
name: xueqiu-strategy
description: 用 Python 在本地模拟买卖，查看现金和持仓，也可把成交和资金划转写入雪球模拟记账账本。
---

# 雪球策略

先用内置示例看一遍流程。示例用三天的价格模拟买入和卖出，最后显示剩余现金和总资产。默认在本地运行。

## 跑一次示例

在自己的 uv 项目中使用 Python 3.12 或更高版本。把下列路径换成仓库中 `base-strategy` 和 `xueqiu-strategy` 两个技能目录的实际路径。

```sh
uv add "<path-to-base-strategy>" "<path-to-skill>"
uv run --locked python "<path-to-skill>/examples/example_strategy_loop.py"
```

也可以从 [Releases](https://github.com/ppoak/quanthub-plugins/releases) 选已发布的版本，把安装命令中的目录换成对应的 `.whl` 文件路径。在另一台机器上使用现有项目时，运行 `uv sync --locked` 安装锁定版本的依赖。

## 换成自己的策略

打开[示例脚本](examples/example_strategy_loop.py)，把价格和买卖条件换成自己的。每次先调用 `exchange.advance()`，用这一时段的价格处理之前提交的订单，再用 `client.buy()` 或 `client.sell()` 提交新订单。现金和持仓从 `account` 查看，本次运行的订单和成交从 `ledger` 查看。

要查雪球上的账本、把成交写回雪球，或导出 Excel，请看[雪球账本使用说明](references/xueqiu.md)。
