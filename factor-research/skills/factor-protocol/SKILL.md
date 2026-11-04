---
name: factor-protocol
description: 提供因子共用接口、DuckDB Hive 存储、因子包加载和注册表。
---

# Factor Protocol

运行前按[插件说明](../../README.md)准备项目环境。`<skill-root>` 指本 Skill 的安装目录，`<plugin-root>` 指插件根目录。

本技能提供因子包接口、路径解析、注册表读取、定义指纹、数据身份、DuckDB/Hive 数据访问和公共产物读写接口。Python 包随插件依赖安装；在 QuantHub 仓库中使用 `uv sync`。

## 项目路径配置

在项目根目录的 `.env` 中设置因子源码、数据和产物根目录：

```dotenv
FACTORZOO=factor_zoo
FACTORZOO_SCHEMA=schema.yaml
DATA=data
ARTIFACTS=artifacts
```

相对路径以项目根目录为基准，绝对路径可用于共享存储。显式路径参数优先于进程环境变量，进程环境变量优先于 `.env`；未配置时使用默认值。仓库提供 `.env.example` 作为配置模板；项目 `.env` 保存实际路径。

`FACTORZOO_SCHEMA` 相对于 `FACTORZOO` 解析。数据与产物按以下结构存储：

| 内容 | 路径 |
| --- | --- |
| 因子源码与 README | `$FACTORZOO/<factor>/` |
| 上游数据 | `$DATA/<table>/date=YYYY-MM-DD/*.parquet` |
| 因子数据 | `$ARTIFACTS/factor_data/<factor>/date=YYYY-MM-DD/*.parquet` |
| 列级验证与评估产物 | `$ARTIFACTS/factor_zoo/<factor>/<column>/` |

## 因子包接口

因子包以 `$FACTORZOO/<factor_name>/README.md` 保存研究定义，以 `$FACTORZOO/<factor_name>/scripts/main.py` 作为入口。包目录、元数据、计算与独立验证回调的契约见[因子包接口说明](references/factor-package-protocol.md)。

因子入口声明 `FACTOR_NAME`、非空的 `FACTOR_COLUMNS` 和 `calculate_factor`。因子包通过 `VALIDATION_INPUT_COLUMNS`、`compute_validation_audit` 和 `define_factor_validator` 描述独立复算接口；特殊结构可提供自有 `validate_factor` 回调。验证定义记录输入、输出和回调。

按[数据访问说明](references/data-access.md)查看 `DATA` 下的实际目录、字段和样本，再确定所用数据、关联方式和日期范围。具体用法记录在对应因子 README 中。

## Python 接口

通过 `factor_protocol` 使用 `FactorStorage`、`FactorPath`、`FactorPackage`、`FactorCalculateContext`、`FactorValidateContext`、`FactorValidationDefinition`、`define_factor_validator`、`resolve_factor_columns`、因子包加载、注册表查询和公共产物读写函数。

`FactorStorage` 提供 DuckDB 查询执行、通用 `SELECT` SQL 组装、Hive Parquet 分区扫描和 Parquet 写入。调用方可通过 `select_sql(table, columns=..., where=..., order_by=...)` 生成数据读取 SQL，再用 `df(sql)` 获取结果；复杂查询可直接组合 DuckDB SQL。`table_root(table)` 返回上游表或因子表的目录。因子值按 Hive `date=YYYY-MM-DD` 分区写入，键为 `(date, code)`。

构造时可用 `factor_root=<directory>` 指定当前因子的数据目录；其他因子和上游表仍按项目路径读取。

数据身份与运行配置的字段和校验方式见[因子数据身份](references/data-identity.md)。使用方通过协议接口声明因子包、读取配置、查询数据并写入标准化产物。

`factor_protocol.readme` 提供第 4 节验收项的读取与原子勾选接口；通过条件由各阶段脚本判断。

协议使用示例和可执行契约案例统一放在 `examples/`；每个案例既演示接口，也用断言核对行为。运行存储接口与数据身份案例：

```powershell
./.venv/Scripts/python "<skill-root>/examples/protocol_cases.py"
./.venv/Scripts/python "<skill-root>/examples/identity_cases.py"
```
