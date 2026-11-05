# 本地数据表契约

本说明由 `database-maintenance` 维护，记录采集结果的表结构、分区和写入语义。下游流程读取数据前应以此契约确认字段、分区和来源口径；各数据源的认证、命令和调度参数见本 skill 的 RiceQuant、EastMoney、雪球及 Hugging Face 参考文档。

## 存储与检查

默认数据根目录是项目的 `data/`。分区表使用 `data/<table>/date=YYYY-MM-DD/*.parquet`，DuckDB 读取时启用 `hive_partitioning=true`；按表契约从 Hive 路径或 Parquet 列读取 `date`。`instruments_info` 和 `ex_factor` 使用各自目录下的 `data_0.parquet` 保存静态数据。采集任务可通过环境变量指定数据根目录。

历史覆盖记录用于追溯；当前覆盖率以运行前检查到的实际分区、字段与样本行为准：

```sql
DESCRIBE SELECT *
FROM parquet_scan('data/quotes_day/date=*/*.parquet', hive_partitioning=true, union_by_name=true);

SELECT date, COUNT(*) AS rows
FROM parquet_scan('data/quotes_day/date=*/*.parquet', hive_partitioning=true, union_by_name=true)
WHERE date >= DATE '2026-09-01'
GROUP BY date
ORDER BY date DESC;
```

旧分区可能经历字段演进。跨期扫描使用 `union_by_name=true` 并核对目标字段缺失率；大表检查限定日期、列和代码范围。

## 表、粒度和主要字段

下表的“写入键”用于采集端去重或 upsert；查询端还应按实际研究粒度检查唯一性。RiceQuant 分区表的 `date` 从源 `time` 生成，因此写入键包含 `time`。

| 表 | 来源与写入键 | 主要字段及语义 |
| --- | --- | --- |
| `quotes_day` | RiceQuant；`(time, code)`，按 `date` 分区 | `open`、`high`、`low`、`close`、`volume`、`amount`、`num_trades`、`prev_close`、`limit_up`、`limit_down`、`suspended`、`st`；股本为 `circulation_a`、`non_circulation_a`、`total_a`、`free_circulation`、`preferred_shares`、`total_shares`，换手率为 `turnover_today`、`turnover_week`、`turnover_month`、`turnover_year`、`turnover_current_year`；相应 `*_post` 为复权序列。日线查询通常按 `(date, code)` 组织。 |
| `quotes_min` | RiceQuant；`(time, code)`，按 `date` 分区 | 分钟 OHLCV、`amount`、`num_trades BIGINT` 和复权 OHLCV；同一股票同日有多条分钟记录，查询键包含 `time`，成交额采用原始口径。 |
| `industry_mapping` | RiceQuant；`(time, code)`，按 `date` 分区 | `first_industry_code`、`second_industry_code`、`third_industry_code` 及对应 `*_industry_name`；日线关联使用 `(date, code)`。 |
| `index_quotes_day` | RiceQuant；`(time, code)`，按 `date` 分区 | 指数 OHLCV、`amount`、`prev_close` 及对应复权列；部分综合记录的 `code` 可为 `NULL`。 |
| `index_weights` | RiceQuant；`(time, code, index_code)`，按 `date` 分区 | 成分证券 `code`、指数 `index_code`、`weight`。 |
| `financial_report` | RiceQuant；`(time, code, account_name)`，按 `date` 分区 | `time` 是来源发布/修订时间戳，`account_name` 是科目，`ttm`、`lyr`、`mrq` 是不同统计口径；同一科目可能有后续修订，因子读取须遵守可见时间。 |
| `instruments_info` | RiceQuant；`code`，静态快照 | `symbol`、`abbrev_symbol`、`instrument_type`、`listed_date`、`de_listed_date`；当前快照记录。 |
| `ex_factor` | RiceQuant；`(ex_date, code)`，单文件 | `announcement_date`、`ex_cum_factor`、`ex_factor`、`ex_end_date` 等除权信息；`rice_create_tm` 为米筐入库时间（Unix 秒整数）。 |
| `eastmoney_stock_sentiment` | EastMoney；`(code, date)` | `tape_z`、`tape_d`、`tape_type`、`hot_rank`、`new_uid_rate`、`new_uid_change_rate`、`old_uid_rate`、`old_uid_change_rate`、`ingested_at`。 |
| `eastmoney_intraday_changes` | EastMoney；`(code, date, event_time, change_type, source_rank)` | `event_time` 为 `HH:MM:SS` 字符串；`name`、`change_type_code`、`market_flag`、`related_info`、`ingested_at`。 |
| `xueqiu_social_count` | 雪球；`(code, date)` | 全市场股票的 `followers`、`discussions`、`ingested_at`。 |
| `xueqiu_hot_rank` | 雪球；`(code, date, rank_type, source_rank)` | 上榜股票的 `rank_type/label`、`source_rank`、`value`、`value_increment`、`rank_change`、`ingested_at`。 |
| `xueqiu_status` | 雪球；`status_id`，按目标 `date` 分区 | `symbol`、`code`、`created_at`、`created_at_ms`、`user_id`、`screen_name`、`text_plain`、`text_length`、`source`、`reply_count`、`retweet_count`、`like_count`、`ingested_at`。 |
| `xueqiu_cube_rebalance` | 雪球；`(code, date)` | `count_buy`、`count_sell`、`count_net`、`count_total`、`count_cube`、`pre_diff`、`update_time`、`ingested_at`。 |

所有 EastMoney 和雪球表也按 `date` 分区。`ingested_at` 记录采集时间；业务事件和信息披露时间按来源字段读取，PIT 计算依据来源可见时点筛选。雪球表的 `date` 由 Hive 路径读取。

`instruments_info.instrument_type` 包含 `CS`（A 股）、`INDX`、`ETF`、`FUND`、`LOF`、`Convertible`、`Future`、`FutureArbitrage`、`Option`、`Repo`、`Spot`、`REITs`。该表记录当前静态快照；历史名称和类型以对应历史数据源为准。

## 来源特有的写入语义

- RiceQuant 的 `quotes_day` 和 `quotes_min` 分别是日线与分钟线。使用 `num_trades` 等字段前，检查当前研究区间的非空覆盖率及缺失分布。采集映射和 upsert 键见 [RiceQuant 参考](ricequant.md)。
- EastMoney 情绪表合并 `stockCard` 多空看盘、`stockrank/getHisList` 热榜与 `stockrank/getHisProfileList` 用户画像。后两者每次返回滚动窗口，按 `(code, date)` upsert，保留最新非空字段。异动表汇集 22 种类型，保留来源内顺序 `source_rank`；两表字段分别聚焦情绪指标和异动事件。见 [EastMoney 参考](eastmoney.md)。
- 雪球社交计数覆盖股票池；热榜记录返回的榜单，默认 `rank_type=12`（一小时）和 `22`（24 小时）。`source_rank` 为榜内名次，`value_increment` 表示来源热度增量，`rank_change` 表示名次变化。评论表保存去 HTML 的 `text_plain`。组合调仓接口返回最近历史窗口，一次采集可能更新多个旧日期分区。见 [雪球参考](xueqiu.md)。
- 备份同步允许名单内的数据表；`quotes_min` 默认自 2015-01-01 起备份，备份范围聚焦数据库表。见 [Hugging Face 备份参考](huggingface.md)。

## 消费端需要遵守的数据语义

- `quotes_day` 和 `quotes_min` 分别是日线与分钟记录；分钟记录需要按来源时间区分。同一证券在同一天可以有多条分钟记录。
- 行情字段中的 `*_post` 表示复权序列；原始价格和复权价格按一致口径使用。停牌记录可能出现零成交或重复收盘价。
- 指数行情应按明确的指数代码筛选后再关联；部分综合记录的 `code` 为空。行业映射等日度来源应按其表契约的证券和日期粒度关联。
- `instruments_info` 提供当前静态快照；历史名称、分类和状态以对应历史数据源为准。
- `financial_report.date` 是分区日期，`time` 是来源发布时间或修订时间。历史输出日按来源可见时间筛选到输出日，并以来源时间排序同日修订。
- `financial_report` 中的 `ttm`、`lyr`、`mrq` 分别对应不同统计口径，按研究定义选择；`account_name` 表示来源科目。构造输出日 `t` 的历史特征时，先按 `COALESCE(CAST(time AS DATE), date) <= t` 筛选可见记录；同一可见日有多次修订时按 `time` 取最新有效值。对每个科目过滤空值与 `NaN` 后，选择截至 `t` 最新的有效值。
- 使用 `lyr` 时，按财年保留年度观察；同一财年多次快照只算一次。首选科目在该财年无有效记录时，再按消费端声明的顺序尝试备用科目；不同财务指标独立执行科目选择。全部候选科目无可见值或样本观察期不足时返回空值。
- `ingested_at` 记录采集时间，来源事件时间、行情日期和信息披露时间按各自字段读取。雪球热榜记录实际返回的上榜结果，缺席项按未上榜处理。

更改采集代码时，同时核对该来源参考文档和本表契约，尤其是字段、写入键、分区及滚动窗口覆盖规则。
