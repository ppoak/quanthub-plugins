---
name: factor-develop
description: 用 DuckDB SQL 实现因子生产计算，并用独立 Python 实现复算。
---

# 因子开发

运行前按[插件说明](../../README.md)准备项目环境。`<skill-root>` 指本 Skill 的安装目录，`<plugin-root>` 指插件根目录。

项目根目录 `.env` 中的 `FACTORZOO` 指定因子源码根目录。以 `$FACTORZOO/<factor_name>/README.md` 第 1–3 节为实现规格，完成因子代码包并交由 Factor Review 审查。目录、元数据和入口遵循[因子包接口说明](../factor-protocol/references/factor-package-protocol.md)。

## 输入与模板

实现前阅读因子 README、研究 Issue 和[因子数据读取](../factor-protocol/references/data-access.md)。从项目 `DATA` 查看实际字段、分区和有界样本，按研究定义确定来源、记录粒度、关联键和可见时间。

新因子包可从[标准因子脚本模板](assets/factor_script.py)开始。模板中的来源关系和输入字段按 README 映射到实际数据；复杂实现按职责拆分。

因子名使用有意义的 `snake_case`，开发分支与因子名一致。按 README 第 1–3 节实现公式、来源字段、可见时间、因子日与输出日、预热、窗口、单位、过滤及特殊值规则；每个差异参数对应稳定、唯一的输出列。因子包 Python 文件原则上不超过 600 行，可按数据读取、SQL 构造、状态逻辑和独立验证拆分。

## 生产计算

`calculate_factor` 使用 DuckDB SQL 计算生产因子值。Join、窗口、聚合、排序、回归和因子值均由 SQL 完成，列裁剪及日期过滤下推到 Parquet 扫描。Python 负责参数处理、SQL 组装和有界计算调度；生产计算不得使用 Pandas、NumPy、SciPy、DataFrame、`.df()` 或 `.fetchdf()` 计算因子值。

所有原始读取声明 `source_begin`、`output_begin` 和 `output_end`。预热输入参与计算，输出只覆盖目标区间。`FactorCalculateContext.compute_batch_days` 表示单批输出的交易日数；窗口长度遵循设计约定：分钟频率一般不超过 20 个交易日，日频一般不超过 240 个交易日。

## 独立验证

`compute_validation_audit` 从有界原始输入独立计算期望值，使用 Python/Pandas/NumPy 公式，覆盖窗口边界、缺失值、零分母、非有限值和常数输入，并返回以 `(date, code)` 为键的 `expected_<factor_column>`。Factor Validate 使用这些期望值检查覆盖率、键唯一性、expected/stored 空值位置及误差范围。

验证实现与生产公式独立：按验证所需的最小原始列读取数据，独立编写复算逻辑；验证器不调用 `calculate_factor`，也不导入生产计算 helper、复用生产 SQL 或把物化因子列作为期望值。两条路径使用相同的源字段、时点可见性、输出日期、窗口、过滤和特殊值规则。Factor Review 阶段对照 README 审查时点可见性与未来数据。

## 数据处理

财务类时点因子按 README 已确认的财务口径和有序科目列表实现。对每个证券、指标和科目，仅使用可见时间不晚于输出日且值非 NULL、有限的记录，并按可见时间进行 ASOF，取最近有效值；再按 README 的科目优先级选择首个有效结果。所有候选科目都没有有效值时输出 NULL。较新的空值或非有限值保留该科目此前最近的有效值；多个指标分别解析。生产 SQL 和独立验证分别实现同一时点与科目回退语义。

分钟频率因子按[高频因子开发规范](references/high-frequency-development.md)设置时间范围、批次、预热和资源。只有计算依赖前一交易日状态时才使用状态缓存；缓存放在 `$ARTIFACTS/cache/<factor_name>/`，记录输入区间、参数、schema、代码指纹与输入数据摘要，并校验元数据和 `(date, code)` 唯一性。滚动窗口通过有界原始输入覆盖预热期；缺失分钟对应的 NULL 语义在两条计算路径中保持一致。
