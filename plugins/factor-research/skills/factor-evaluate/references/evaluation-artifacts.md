# 评估产物

直接评分的结果保存在 `<output-dir>/<column>/`，默认使用 `$ARTIFACTS/factor_evaluation/<column>/`；因子包默认保存在 `$ARTIFACTS/factor_zoo/<name>/<column>/`。计算、绘图和文档操作见 [Factor Evaluate](../SKILL.md)，参数见[评估配置](evaluation-settings.md)。

三类指标都按 `variant` 区分：`raw` 为原始因子值，`neutralized` 为去除行业和市值影响后的值，`rolling_mean` 为中性化值的滚动均值。窗口和行业层级由评估参数指定。

## ic.csv：因子与未来收益的关系

| 字段 | 含义 |
| --- | --- |
| `date`、`variant` | 信号日、因子处理方式；两者共同确定一条记录 |
| `ic` | 当日因子排名与未来收益排名的相关系数，即 Rank IC |
| `observations` | 参与计算的股票数 |

股票数至少为 3，结果须为有限数值。只有入场时非 ST、未停牌、价格严格处于涨跌停价之间，且入场、退出价格有效的股票参与计算。信号日与退出日须处于同一训练、验证或测试区间。

## returns.csv：组合收益和换手

每条记录由 `date, variant, portfolio_id, layer` 确定。

| 字段 | 含义 |
| --- | --- |
| `date`、`variant` | 信号日和因子处理方式 |
| `portfolio_id` | 持有期不同起点对应的组合编号 |
| `layer` | 从低分到高分的分层编号 |
| `gross_return`、`net_return_5bps` | 分层组合的毛收益和扣除交易成本后的收益 |
| `gross_return_nav`、`net_return_5bps_nav` | 分层组合的毛、净累计净值 |
| `turnover`、`holdings`、`observations` | 分层组合换手率、持股数和有效收益样本数 |
| `top_gross_return`、`top_net_return_5bps` | Top 组合的毛、净收益 |
| `top_gross_return_nav`、`top_net_return_5bps_nav` | Top 组合的毛、净累计净值 |
| `top_turnover`、`top_holdings`、`top_observations` | Top 组合换手率、持股数和有效收益样本数 |

组合等权持有。Top 选择高分端还是低分端，由训练期原始因子的平均 IC 决定，测试期不参与选择。Top 字段在同日、同种处理方式、同一组合的各层重复，统计前按 `(date, variant, portfolio_id)` 去重。

首次建仓换手率为 1，之后比较上一期持仓经过收益变化后的权重与本期目标权重，范围为 `[0, 2]`。净收益计算为 `gross_return - turnover × 0.0005`。换手率保存在本文件，不另设 CSV。

持有期为 N 个交易日时，按入场日在交易日历中的位置分成 N 条错开的组合序列，各自累计净值。收益是持有期收益，不直接当作单日收益使用；缺少已实现收益的日期不填零。

## correlation.csv：与参考因子的关系

| 字段 | 含义 |
| --- | --- |
| `date`、`variant`、`reference_factor` | 信号日、因子处理方式和参考因子；三者共同确定一条记录 |
| `correlation` | 当日因子排名与参考因子排名的相关系数 |
| `observations` | 同时有有效数据的股票数，至少为 3 |

沿用 IC 和组合评估的股票范围。未指定参考因子时保留表头，不生成数值记录。

## config.json：本次计算的设置和状态

记录输入路径或因子包名称、因子列、日期、收益周期、价格列、分层设置、线程和内存上限，以及输入文件和结果文件的摘要，用于检查结果是否仍对应当前数据。直接评分的 `factor` 保存 `path` 和 `column`。

`evaluation.tasks` 分别保存三项任务的设置和记录。`complete` 表示三项结果齐全且匹配；`partial` 表示尚未齐全或设置、数据不一致；已有结果被标为 `stale` 时需要重新检查。文件齐全不等于三项来自同一次设置。

因子包还保存代码和已有 Validate 记录，直接评分不要求这些内容。实际日期范围和指标数值从 CSV 查看。

## overview.png：评估总览图

展示 IC、分层与 Top 净值、换手率及参考相关性。仅在完整结果检查通过后绘制。

图中展示方式根据验证期 IC 均值与波动之比的绝对值选择，优先保持训练期确定的方向，测试期不参与选择。多条持有期组合曲线按各自的观测序号对齐后取均值，不把不同起点的收益拼成一条复利曲线。

## audit.csv：已有独立验证结果

由 Factor Validate 生成，Evaluate 不执行独立复算。常见字段为 `date, code, expected_value, stored_value, abs_diff`，另有验证诊断列。因子包评估需要检查此文件及对应的验证记录；直接评分不需要它。

## 更新已有结果

单项任务只替换自己的 CSV，其余文件保留。完整评估在历史数据和设置不变时追加新日期；只有参考数据变化时，可只重算相关性。历史数据、设置或指标文件变化时，按检查结果重算；最终仍需三项记录匹配才能完整验收。
