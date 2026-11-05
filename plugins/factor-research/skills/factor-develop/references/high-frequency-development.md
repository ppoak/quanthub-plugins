# 高频因子开发规范

## 单次计算范围

`FactorCalculateContext.compute_batch_days` 表示单批输出的交易日数；高频因子的单次输出跨度一般不超过 12 个自然月。`source_begin` 包含所需预热输入，`output_begin` 和 `output_end` 界定本次写入范围。分钟级行情读取使用明确的时间范围或等价分区谓词，列裁剪和时间过滤下推到 Parquet 扫描。

每次计算按选定的 `compute_batch_days` 一次性生成该输出区间。性能记录覆盖这次计算的完整耗时和峰值 RSS；输出按 `(date, code)` 保持唯一。

## 状态依赖与缓存

根据因子的跨日状态依赖选择缓存方式：

1. 日级聚合仅依赖当天分钟输入时，在有界 `source_begin` 至 `output_end` 内读取原始分区；滚动窗口所需历史由预热输入覆盖；
2. 后续交易日依赖前一日计算状态时，使用每日状态缓存。缓存位于 `$ARTIFACTS/cache/<factor_name>/`，记录输入日期、代码范围、参数、依赖 schema、代码指纹与输入数据摘要和生成时间；恢复前核验元数据，并检查 `(date, code)` 唯一性；
3. 缓存读取保留缺失分钟形成的 NULL；零填充、前值填充和跨缺失日滚动均遵循因子 README 定义。

多参数或多窗口计算可复用同一次计算内存中的日级结果。状态字段、初始化、更新顺序、失效和重算策略遵循因子 README 的已确认定义。

## 性能和资源

DuckDB 执行分钟读取、聚合和窗口计算；独立验证可用 Python/Pandas/NumPy 复算有界原始样本，生产因子值由 DuckDB SQL 生成。运行时设置合适的 `--threads`、`--memory-limit` 和 `--temp-directory`，并监控临时空间。每次计算记录完整输出区间的耗时和峰值 RSS。
