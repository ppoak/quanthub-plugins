# 生产增量更新

本说明介绍 active 因子的更新条件、失败处理和调度配置。数据目录由项目 `.env` 的 `DATA` 指定，读取方法见[因子数据读取](../../factor-protocol/references/data-access.md)。`<skill-root>` 为 Factor Update 的安装目录。

## 执行入口

在项目根目录激活项目虚拟环境后运行：

```shell
.venv/Scripts/python "<skill-root>/scripts/run_update.py" --project-root . --all --dry-run
.venv/Scripts/python "<skill-root>/scripts/run_update.py" --project-root . --all --threads 1 --memory-limit 1GB --validation-memory-limit 1GB --temp-directory .tmp/factor_update
```

`--all` 更新 `FACTORZOO_SCHEMA` 的全部 active 包；`--factor-name` 更新其中指定的包。两种方式均核对包的源码指纹与完整声明列，并以包为单位执行。

## 更新条件

已有因子数据须满足以下条件：

- `identity.json` 与当前计算指纹、列定义及整个历史摘要一致。相对路径、修改时间或文件内容变化，返回 `rebuild_required`，通过 Calculate 重算完整已存区间。
- Parquet 输出列符合 `FACTOR_COLUMNS`；不符时返回 `schema_mismatch`，先重建数据。
- 每列的 `config.json` 和 `audit.csv` 齐全，验证状态为 `pass`，验证尾日覆盖因子输出尾日。缺失或失败返回 `validation_failed`，覆盖不足返回 `validation_stale`。

尚未物化的包返回 `uninitialized`；先完成历史计算，或用 `--bootstrap-begin` 指定初始化起点。dry-run 只报告计划和阻断原因。

## 日期与计算

按实际因子尾日计算新增源日期。依赖多表共同日历的包声明 `UPDATE_CALENDAR = "quotes_common"` 和 `UPDATE_CALENDAR_TABLES`。因子按定义决定是否输出；末尾零输出日期可在后续更新中再次计算。

声明 `UPDATE_SOURCE_TABLES` 的包会检查源分区的晚到改写；源数据改写影响已存结果时返回 `rebuild_required`，通过 Calculate 重算。

Calculate 在临时目录核对输出列、日期边界、唯一键和有限值，成功后统一提交 Parquet 与历史摘要。失败保留原数据；目录切换中断时，下次真实运行先恢复目录。

## 验证与评估

计算提交后，独立验证最近 5 个有输出日期，并按本次上游结束日期刷新全部声明列的评估。Evaluate 对齐行情交易日，检查输入快照及指标摘要；有效结果复用，变化或损坏的结果重新计算。因子已到最新日期时也执行此检查。

验证或评估失败时非零退出，并写入 `.tmp/factor_update/<factor>/refresh_failed.json`；下次运行重试。全部后续检查通过才算更新完成。

## 调度与 README

项目调度器按需调用更新脚本：

```shell
.venv/Scripts/python "<skill-root>/scripts/run_update.py" --project-root . --all
```

脚本直接读取因子使用的数据分区和交易日历，确定可更新日期；没有新增日期时检查现有验证和评估状态。多表因子按包内声明的日历与时间关联方式确定范围，低频数据按因子定义使用。

成功更新后回写 README 第 7 节统一性能块中的正式增量运行记录，包含日期、资源、耗时、峰值 RSS、行数及状态。dry-run、`up_to_date` 和失败结果不生成实测记录。需要重试回写时运行：

```shell
.venv/Scripts/python "<skill-root>/scripts/readme.py" --project-root . --factor-name <factor_name>
```

`--no-readme-update` 跳过本阶段及内部 Evaluate 对 README 的回写。
