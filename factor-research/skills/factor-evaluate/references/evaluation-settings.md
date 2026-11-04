# Factor Evaluate 配置

评估脚本从项目根目录的 `.env` 读取默认值，读取顺序为命令行参数、进程环境变量、`.env`、代码默认值。路径可使用绝对路径或相对于项目根目录的路径。下表中的默认值与[配置示例](../.env.example)一致，代码在变量缺省时使用相同默认值。

## 项目路径

项目路径使用 [Factor Protocol 的项目路径配置](../../factor-protocol/SKILL.md#项目路径配置)。

## 评估口径

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `FACTOR_EVALUATE_BEGIN` | `2014-01-01` | 评估起始日期。 |
| `FACTOR_EVALUATE_TRAIN_END` | `2022-12-31` | 训练期结束日期。 |
| `FACTOR_EVALUATE_VALIDATION_END` | `2024-12-31` | 验证期结束日期；测试期从下一交易日起至评估结束日期。评估结束日期默认取因子最新物化日期，可由 `--end` 指定。 |
| `FACTOR_EVALUATE_HORIZON_DAYS` | `5` | 未来收益周期，单位为交易日。 |
| `FACTOR_EVALUATE_EXECUTION_LAG_DAYS` | `1` | 信号到执行的滞后交易日数。 |
| `FACTOR_EVALUATE_PRICE_COL` | `close_post` | 收益计算使用的价格列。 |
| `FACTOR_EVALUATE_QUANTILES` | `10` | 分层组合数量，至少为 2。 |
| `FACTOR_EVALUATE_TOP_PERCENTILE` | `1.0` | Top/Bottom 组合的百分位范围，取值为 `(0, 50]`。 |
| `FACTOR_EVALUATE_ROLLING_MEAN_DAYS` | `20` | `rolling_mean` 使用的滚动交易日窗口，包含当日。 |
| `FACTOR_EVALUATE_NEUTRALIZATION_INDUSTRY_LEVEL` | `first_industry_code` | 中性化使用的行业层级，可选 `first_industry_code`、`second_industry_code` 或 `third_industry_code`。 |

股票筛选、方向选择和收益计算说明见[评估产物](evaluation-artifacts.md)。

## 资源与批次

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `FACTOR_EVALUATE_THREADS` | `2` | 每个评估进程使用的 DuckDB 线程数。 |
| `FACTOR_EVALUATE_MEMORY_LIMIT` | `4GB` | 每个评估进程的 DuckDB 内存上限。 |
| `FACTOR_EVALUATE_TEMP_DIRECTORY` | `.tmp/factor_evaluation` | DuckDB 临时溢写及评估中间文件根目录。 |
| `FACTOR_EVALUATE_WORKERS` | `1` | 并行处理的进程数；每个进程使用独立临时目录。 |
| `FACTOR_EVALUATE_WORKER_COLUMN_GROUP_SIZE` | `1` | 分配给单个进程任务的因子列数。 |
| `FACTOR_EVALUATE_INTRA_COLUMN_WORKERS` | `1` | 单列完整重建时用于拆分指标和参考因子相关性任务的进程数。 |
| `FACTOR_EVALUATE_COLUMN_BATCH_SIZE` | 空 | 显式指定每个 SQL 批次处理的因子列数；留空时自动估算。 |
| `FACTOR_EVALUATE_DATE_BATCH_DAYS` | 空 | 显式指定每个 SQL 批次处理的交易日数；留空时自动估算。 |
| `FACTOR_EVALUATE_DEFAULT_COLUMN_BATCH_SIZE` | `8` | 多列自动批次的列数上限。 |
| `FACTOR_EVALUATE_DEFAULT_DATE_BATCH_DAYS` | `252` | 多列自动批次的交易日数上限。 |
| `FACTOR_EVALUATE_SINGLE_COLUMN_DATE_BATCH_DAYS` | `126` | 单列自动计算的默认交易日批次上限。 |
| `FACTOR_EVALUATE_REFRESH_COLUMN_BATCH_SIZE` | `1` | 增量刷新使用的列批次大小。 |
| `FACTOR_EVALUATE_REFRESH_DATE_BATCH_DAYS` | `31` | 增量刷新使用的交易日批次大小。 |
| `FACTOR_EVALUATE_REFERENCE_REFRESH_DATE_BATCH_DAYS` | `31` | 仅刷新参考因子相关性时使用的交易日批次大小。 |
| `FACTOR_EVALUATE_REFERENCE_CORRELATION_BATCH_SIZE` | `4` | 每个相关性批次处理的参考因子数。 |
| `FACTOR_EVALUATE_PROBE_DATE_DAYS` | `20` | 自动估算内存批次时抽取的样本交易日数。 |

`DATE_BATCH_DAYS` 与相关批次变量都按 `quotes_day` 中的交易日计数。显式设置 `COLUMN_BATCH_SIZE` 或 `DATE_BATCH_DAYS` 会覆盖自动批次选择；对应命令行参数也可逐次覆盖 `.env` 配置。

日期区间、收益周期、价格列、分层和中性化设置会写入每列 `config.json`，以便追溯评估口径；实际批次、线程和内存配置也会记录在资源信息中。
