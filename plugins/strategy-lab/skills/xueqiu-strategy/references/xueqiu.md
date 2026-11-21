# 雪球账本使用说明

## 找到要用的账本

按[配置示例](../assets/.env.example)填写项目的 `.env`。`XUEQIU_TOKEN` 填雪球登录凭据（浏览器中的 `xq_a_token`），`XUEQIU_GID` 填模拟记账账本的编号。

用 `uv run --locked --env-file .env python` 启动 Python。从 `os.environ` 读取登录凭据，传给 `XueqiuClient(token=...)`。调用 `client.list_investment_groups()` 后，在 `result_data.trans_groups` 里按账本名称查找 `gid`。

## 读取雪球上的记录

下列方法都通过 `client` 查询指定账本。查询成功时，返回的 `result_code` 是 `60000`。表中字段都在 `result_data` 下面。

| 想查什么 | 调用方法 | 数据的位置 |
| --- | --- | --- |
| 现金和持仓 | `portfolio_performance(gid)` | `performances` 中，`ALL` 行的 `cash` 是总现金，各市场行的 `list` 是持仓明细。 |
| 成交记录 | `transaction_records(gid, row=50)` | `transactions` |
| 入金和出金记录 | `bank_transfer_records(gid, row=50)` | `bank_transfers` |

`ledger.order_history()` 和 `ledger.transfer_history()` 读取远端原始成交和划转记录。`ledger.execution_history(record=True)` 将可转换的远端成交按成交编号去重写入 `ledger.executions`。远端成交时间使用北京时间。历史方法默认读取一页，可传入响应里的 `pos` 从下一页开始，或设置 `all_pages=True` 读取全部。

## 把成交写到雪球

确认要写入的账本后，在自己的脚本里把登录凭据传给 Client，把账本编号传给 Ledger 和 Exchange。创建 Exchange 时设为 `sync_transactions=True`。开始前按雪球上的实际现金和持仓设置本地账户，已有持仓可用 `account.replace(cash=..., positions=...)` 一起载入。

开启同步后，雪球确认成交或资金划转后，才更新本地账户并写入评估用的账本。`exchange.transfer("2026-01-05", 500)` 表示入金 500 元，金额为负数表示出金。成交备注会附上一段编号，方便断网后核对以及导入历史时去重。

## 收盘保存和继续运行

创建 Exchange 时传入 `store=XueqiuStore("output/xueqiu-run.sqlite3")`。在 `XueqiuStore` 里设置目录和文件名。相对路径从启动命令时所在的目录算起，缺少的目录会自动创建。每次回测用自己的文件。

日行情调用一次 `advance()`。用日内行情时，盘中传 `is_close=False`，收盘这一轮传 `is_close=True`。当天撮合结束，用收盘价算出净值后，SQLite 一次保存账户、未成交订单和新增历史。盘中断开时，文件里保留的是上次成功收盘的状态。

重启时使用原来的路径、账户和账本，从 `last_completed_bar` 之后用相同的行情和策略信号重新运行。同步雪球时，让该策略独占账本。脚本会用成交备注核对已写入雪球的成交，能够唯一对应的划转也会核对后复用。如果程序在雪球回应前退出，而且账本上暂时查不到记录，先核对这笔写入再重跑。程序还开着时，可以先调用 `recover_pending()` 查账。

## 下单和价格

- 价格表每行对应一只证券，行名用 `000001.SZ` 这样的代码。`open` 是开盘价，`close` 是收盘价。限价单用 `high` 和 `low` 判断本时段内是否到过限价。提供的 `volume` 为零或缺失时，订单继续等待。收盘价缺失时，持仓当期市值也保持缺失。
- `buy()` 和 `sell()` 默认用下一个可成交时段的开盘价。指定 `exectype="LIMIT"` 和 `price=...` 可以设限价。股数填正整数，未完成的订单可以调用 `client.cancel(order)` 撤销。
- 每笔成交后立刻更新现金和股数，下一笔订单用更新后的余额判断能否成交。余额或持有股数不足时，订单状态为 `MARGIN`。买入后可立即卖出，股数可以是任意正整数。
- 佣金和税默认是零。用 `commission` 和 `tax` 回调返回每笔实际金额（元），可以设置最低收费。只传 `commission_rate` 和 `tax_rate` 时，按千分比算出金额。同步时，按实际费用倒算雪球的四位小数费率。费率无法还原金额，或雪球确认的金额与指定费用不符时，停止同步并报错。

## 导出本次运行

需要 Excel 时安装 `base-strategy[excel]`，再调用 `xueqiu_strategy.stores.to_excel(account, ledger, "output/xueqiu.xlsx")`。表格包含账户、持仓、订单和成交。

从已有账本开始回测时，先用 `account.replace()` 载入同一天的现金和持仓。开始交易前保存这份现金和持仓股数，分别传给 `evaluate_executions()` 的 `initial_cash` 和 `initial_positions`。行情从同一起点开始，成交只传入快照之后的新记录。
