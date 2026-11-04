---
name: factor-update
description: 按项目现有数据增量更新 active 因子，并刷新验证和评估结果。
---

# Factor Update

运行前按 [Factor Protocol 安装说明](../factor-protocol/SKILL.md#安装)检查项目已有环境和依赖。`<path-to-skill>` 指本 Skill 的安装目录。

本技能以因子包为最小更新单位，增量计算 `.env` 所配置注册表文件的 `active` 项中各包的全部声明列。定义变化、历史源分区改写或 schema 不安全时返回 `rebuild_required`，并交由 `factor-calculate` 显式重建。

更新会刷新评估结果，运行前同时检查 [Factor Evaluate 的依赖](../factor-evaluate/SKILL.md)。

入口与实现脚本平铺在 `scripts/` 下；运行时使用插件内的 Factor Protocol 和项目环境。

正式库的更新门禁、失败状态和可选调度配置见[生产增量更新说明](references/production-update.md)。

从项目根目录运行：

```sh
uv run --locked python "<path-to-skill>/scripts/run_update.py" --project-root . --all --threads 1 --memory-limit 1GB --validation-memory-limit 1GB --temp-directory .tmp/factor_update
uv run --locked python "<path-to-skill>/scripts/run_update.py" --project-root . --factor-name <factor_name> --dry-run
```

在其他目录运行时，`--project-root` 指定数据项目；相对数据、因子库、临时和输出路径均从该根目录解析，正式验证和评估产物位于 `$ARTIFACTS/factor_zoo/`，`ARTIFACTS` 从项目 `.env` 读取。更新流程通过子进程调用插件内的 Calculate、Validate、Evaluate 脚本，检查 active 包级指纹、完整输出列清单和文件身份，采用 DuckDB 资源限制、逐因子释放内存及失败非零退出。

`--all` 与 `--factor-name` 都只更新 `FACTORZOO_SCHEMA` 指定文件中 `active` 项列出的因子；每个条目的 `columns` 必须与该包的 `FACTOR_COLUMNS` 完整一致。

调度中的验证调用使用 `--no-readme-update`，刷新 audit/config；需要更新 README 第 6 节时，单独执行 factor-validate 的标准脚本并检查差异。

项目调度器按需调用本 Skill。更新范围由实际数据分区和因子声明的日历确定；数据查看方法见[因子数据读取](../factor-protocol/references/data-access.md)，调度方式见[生产增量更新说明](references/production-update.md)。

真实更新成功后，`run_update.py` 调用同级 `readme.py`，回写因子 README 第 7 节 `factor-benchmark:results` 统一性能块内的最新增量更新记录，包含测试日期、输出日期、源起始日、线程、内存上限、每批交易日数、端到端墙钟耗时、更新进程树峰值 RSS、输出行数、状态和备注。记录覆盖本次更新的全部日期，成功的零输出更新记录行数为 0。源起始日取因子计算日志中的 `source_begin`；因子未输出该字段时显示 `—`。墙钟耗时和 RSS 覆盖计算、验证与评估刷新。`--dry-run`、`up_to_date` 和阻断/失败结果不生成新的实测记录；README 写回失败时命令返回非零，并保留最新测量。可单独重试 README 写回，不必重新运行更新：

```sh
uv run --locked python "<path-to-skill>/scripts/readme.py" --project-root . --factor-name <factor_name>
```

该脚本只读取 `.tmp/factor_update/<factor_name>/readme_result.json` 并替换对应标记区域；没有成功更新记录时会明确失败。`--no-readme-update` 同时禁用 Update 第 7 节和内部 Evaluate 刷新对 README 的修改。Update 记录说明本次增量更新的资源消耗；Factor Benchmark 用于比较固定区间内的不同批次规模。Update 仅替换统一性能块内的正式增量运行记录，保留批次与单日增量基准。

启动检查使用 `uv run --locked python "<path-to-skill>/scripts/run_update.py" --help` 和 `uv run --locked python "<path-to-skill>/scripts/readme.py" --help`。
