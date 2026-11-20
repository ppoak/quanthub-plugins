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

当前版本的 `ledger.order_history()`、`execution_history()` 和 `transfer_history()` 可能在雪球有记录时仍返回空列表。查远端记录时，请用上表的 Client 方法。

## 把成交写到雪球

确认要写入的账本后，在自己的脚本里把登录凭据传给 Client，把账本编号传给 Ledger 和 Exchange。创建 Exchange 时设为 `sync_transactions=True`。开始前按雪球上的实际现金和持仓设置本地账户，已有持仓可用 `account.replace(cash=..., positions=...)` 一起载入。

每笔成交先更新本地现金和持仓，再写到雪球。`exchange.transfer("2026-01-05", 500)` 表示入金 500 元，金额为负数表示出金。开启同步后，划转也会写到雪球。收到写入错误时，先查雪球的成交或资金记录，确认缺哪一笔再补记。

## 下单和价格

- 价格表每行对应一只证券，行名用 `000001.SZ` 这样的代码。`open` 是开盘价，`close` 是收盘价。限价单用 `high` 和 `low` 判断本时段内是否到过限价。提供的 `volume` 为零时，订单继续等待。
- `buy()` 和 `sell()` 默认用下一个可成交时段的开盘价。指定 `exectype="LIMIT"` 和 `price=...` 可以设限价。股数填正整数，未完成的订单可以调用 `client.cancel(order)` 撤销。
- 每笔成交后立刻更新现金和股数，下一笔订单用更新后的余额判断能否成交。余额或持有股数不足时，订单状态为 `MARGIN`。买入后可立即卖出，股数可以是任意正整数。
- 本地手续费默认是零，可通过 `commission` 设置。同步到雪球时，可在成交入口传 `commission_rate` 和 `tax_rate`，让雪球按相同口径记账。

## 导出本次运行

需要 Excel 时安装 `base-strategy[excel]`，再调用 `xueqiu_strategy.stores.to_excel(account, ledger, "output/xueqiu.xlsx")`。表格包含账户、持仓、订单和成交。示例也演示了如何用成交记录计算资产变化。

原实现见 [QuantHub 提交 `1d0a477`](https://github.com/ppoak/quanthub/commit/1d0a477c5edd5b327c6bdbcaed680dbf99954d5e) 的 `quanthub/trader/xueqiu/`。
