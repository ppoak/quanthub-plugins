# <Factor Name>

<!-- 将尖括号内容替换为正文；无内容的可选项删除。 -->

## 1. 因子动机

<!-- 说明待捕捉的信息、研究假设、经济原理和来源。 -->
<一段话>

## 2. 定义与数据

<!-- 给出公式与计算规则，明确口径和边界。 -->
<因子计算逻辑、规则、公式>

$$
F_{i,t} = <LaTeX formula>
$$

<频率、窗口、横截面或时序口径；高/低值的经济含义。>

<!-- 逐项填写数据表、字段、时间语义、预热期和边界。 -->
- 数据来源：<数据表、必要字段、交易日/公告日/可见日语义>
- 预热期：<无或 N 个交易日；预热数据取值范围 [t - N + 1, t]>
- 边界：<缺失、零分母、inf、常数、单位、过滤和 NULL 规则>

## 3. 参数与输出

<!-- 只列差异参数，不填无对比变化的参数；每个差异或对比参数映射到输出列。 -->
- 参数：<参数及默认值>
- 列名：<稳定列名或命名规则；默认总列数>
- 依赖：<无；或说明 t 如何依赖 t-1 与缓存内容>
- 数值：<NULL值、无限值或特殊值处理规则>

## 4. 验收标准

<!-- Review 维护第 1–3 项；Validate、Benchmark、Calculate、Evaluate、Publish 依次维护第 4–8 项。按实际通过证据勾选。 -->
- [ ] calculate 使用 DuckDB SQL，实现与 README 的定义、参数和输出一致。
- [ ] validate 使用独立 Python 实现，与 README 定义一致。
- [ ] 未来数据与时点可见性审查通过。
- [ ] 全部声明列的物化结果通过独立复算。
- [ ] Benchmark 完成批次与单日追加测试，耗时及峰值 RSS 满足要求。
- [ ] 完整历史的 `(date, code)` 唯一键检查通过。
- [ ] evaluate 完成全部声明列的评估与总览图，提交用户判断。
- [ ] Publish 完成全部 CSV/PNG 与 README 同步，Release 已转正并设为 latest。

## 5. 参考文献

<!-- 参考文献采用 APA 格式，链接指向可获取的正式来源。 -->
- [<APA 格式的作者、年份、题名和出版物/机构>](<可获取的正式来源链接>)
- <补充来源、实现决策或定义差异；无则删除>

## 6. 因子验证

<!-- factor-validate:results:start -->
| 测试日期 | 样本日期区间 / 数据覆盖截止日 | 因子列 | 交易日 / 代码数 / 审计行 | 覆盖率 | 空值不一致数 | 超差数 | 重复键 | 最大绝对误差 | 状态 | 备注 |
|---|---|---|---|---|---|---|---|---|---|---|
| YYYY-MM-DD | YYYY-MM-DD..YYYY-MM-DD / — | `<factor_column>` | — / — / — | — | — | — | — | — | pending | <备注> |
<!-- factor-validate:results:end -->

<!-- 本节结果由 factor-validate/scripts/run_validate.py 生成并回写；不手填数值。默认检查最近 5 个交易日。 -->

## 7. 性能测试

<!-- factor-benchmark:results:start -->
| 测试日期 | 输出日期 / 区间 | 输出交易日数 | 每批交易日 | 内部批次数 | 线程 | 内存限制 | 日均耗时（秒） | 峰值 RSS（MB） | 状态 | 原因 |
|---|---|---:|---:|---:|---:|---|---:|---:|---|---|
| YYYY-MM-DD | — | — | 1 | — | — | — | — | — | pending | — |
| YYYY-MM-DD | — | — | 5 | — | — | — | — | — | pending | — |
| YYYY-MM-DD | — | — | 10 | — | — | — | — | — | pending | — |
| YYYY-MM-DD | — | — | 20 | — | — | — | — | — | pending | — |
| YYYY-MM-DD | — | — | 30 | — | — | — | — | — | pending | — |
| YYYY-MM-DD | — | 1 | 1 | 1 | — | — | — | — | pending | 尚未执行 |
<!-- factor-benchmark:results:end -->


<!-- 批次与单日增量性能由 factor-benchmark 统一测试并回写。 -->

## 8. 因子评估

<!-- factor-evaluate:results:start -->
评估时间划分：训练期 <YYYY-MM-DD..YYYY-MM-DD>；验证期 <YYYY-MM-DD..YYYY-MM-DD>；测试期 <YYYY-MM-DD..YYYY-MM-DD>。

| 测试日期 | 评估区间 | 因子列 | 选定变体 | 方向 | 训练期平均 IC | 验证期 IC IR | 测试期 IC 天数 | 测试期 IC 均值 | 测试期 IC 标准差 | 测试期 IC IR | Top 净收益均值 | Top 期末净 NAV 均值 | Top 换手均值 | 参考因子平均绝对相关 | 评估水位 | 状态 | 备注 |
|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|---|
| YYYY-MM-DD | YYYY-MM-DD..YYYY-MM-DD | `<factor_column>` | `<raw / neutralized / rolling_mean>` | `<high / low>` | — | — | — | — | — | — | — | — | — | — | YYYY-MM-DD | pending | <备注> |
<!-- factor-evaluate:results:end -->

<!-- factor-evaluate/scripts/core/readme.py 根据已落盘产物生成并回写本节；评估结果覆盖全部因子列和变体，状态使用 complete、stale、pending、timeout、oom 或 blocked。 -->
<!-- 训练、验证和测试期指标从当前 ic.csv、returns.csv 和 correlation.csv 计算，时期边界与评估参数取 config.json。 -->
<!-- 测试期 IC IR 为均值除以样本标准差；Top NAV 先按 portfolio_id 取末值再求均值。 -->
<!-- 只总结已经落盘且可复核的结果；未运行、超时、OOM、权限阻塞和与当前定义不符的结果必须明确写出。 -->

## 9. 因子速览

<!-- factor-evaluate 生成图表内容；factor-publish/scripts/run_publish.py 绑定当前 Release 的 overview.png URL。不要引用 artifacts/ 或 .tmp/ 下的本地文件。 -->

### `<factor_column>`

![<factor_column> evaluation overview](<release-image-url>)
