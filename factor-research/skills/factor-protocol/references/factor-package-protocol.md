# 因子包接口说明

本文档定义因子包的目录、稳定元数据和运行入口。因子包通过 Factor Protocol 声明共享契约。

## 目录结构

```text
$FACTORZOO/<factor_name>/
├── README.md
└── scripts/
    ├── main.py
    ├── calculation.py
    ├── validation.py
    └── optional_modules.py
```

`README.md` 保存因子研究规格；默认运行入口为 `scripts/main.py`。复杂实现可按职责拆分为同一因子包内的模块，并从 `main.py` 导出稳定接口。运行产物通过 Factor Protocol 的产物接口写入 `$ARTIFACTS/factor_zoo/<factor>/<column>/`。

## 元数据和函数

`scripts/main.py` 按因子包契约声明以下元数据：

- `FACTOR_NAME`：因子包的稳定名称，与 `$FACTORZOO/<factor_name>/` 中的目录名一致。
- `FACTOR_COLUMNS`：非空、有序的因子输出列名。列集合应稳定；因子包是最小发布和更新单位，active schema 的 `columns` 记录全部声明列。
- `UPDATE_CALENDAR`：定义输出交易日的上游分区表名；多表共同日期使用 `quotes_common`，并在 `UPDATE_CALENDAR_TABLES` 中列出表名。
- `UPDATE_SOURCE_TABLES`：参与计算的上游表，用于识别已物化历史的源数据改写。
- `VALIDATION_SOURCE_TABLE`：标准独立复算读取的上游表名，与 `VALIDATION_INPUT_COLUMNS` 配合使用。
- `VALIDATION_INPUT_COLUMNS`：标准验证所需的最小原始输入列，至少包含 `code` 和 `date`，并包含独立复算实际使用的输入列。

计算入口 `calculate_factor` 由使用方加载并调用。推荐的接口接收 `FactorCalculateContext`：

```python
def calculate_factor(*, context: FactorCalculateContext) -> None:
    ...
```

`FactorCalculateContext` 提供共享的 `FactorStorage`、因子名、计算起止日期、数据根目录、输出目录、线程数、当前选择的因子列和批次大小 `compute_batch_days`。该值表示每批输出的交易日数。入口也可声明具体命名参数，由调用方按函数签名传入。因子包使用包内模块引用和显式路径配置解析源码。

因子包通过 `compute_validation_audit(source, cfg)` 基于有界原始数据计算期望值；多列因子可提供 `compute_validation_audits(source, cfg)` 一次生成全部期望列。`define_factor_validator(...)` 创建 `FactorValidationDefinition`，记录因子名、输出列、最小输入列和复算回调。调用方执行采样和比较，并写出审计结果。审计表以 `code`、`date` 为键，每个输出列对应 `expected_<factor_column>`。特殊验证结构可由因子包的 `validate_factor` 回调表达。

独立复算函数以原始输入独立计算期望值，并返回 `expected_<factor_column>` 列。加载器读取因子入口及其定义；调用方在验证时调用回调并生成审计产物。

## 公共接口和数据访问

从 `factor_protocol` 导入稳定接口：

- `FactorStorage`：DuckDB 连接、参数化查询、Hive Parquet 读写、分区扫描、按分区日期分组和通用 `select_sql` 组装。
- `FactorPath`：解析 `table.column` 形式的因子引用。
- `FactorPackage`、`resolve_factor_package`、`load_module_from_path`：按项目根目录解析因子包位置并加载入口。
- `FactorCalculateContext`、`FactorValidateContext`、`resolve_factor_columns`：计算上下文、验证上下文和列解析接口。
- active 注册表、定义指纹和公共产物读写函数：用于查询或维护稳定元数据。

跨因子引用使用 `FactorPath` 表达；加载器依据项目根目录和 `FACTORZOO` 定位因子包。定义指纹由因子包内相对路径和源码内容构成。

## 运行依赖与职责边界

Factor Protocol 随[插件依赖](../../../README.md#安装与环境)安装；在 QuantHub 仓库开发时使用 `uv sync`。

因子包从 `factor_protocol` 导入共享契约、路径、加载、注册、指纹、存储和公共产物接口。
