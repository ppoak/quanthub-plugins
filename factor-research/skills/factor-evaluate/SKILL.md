---
name: factor-evaluate
description: 评估因子包或已有评分数据，计算 IC、组合收益和相关性，检查结果、绘图并更新研究文档。
---

# Factor Evaluate

运行前按[插件说明](../../README.md)准备项目环境。`<skill-root>` 指本 Skill 的安装目录，`<plugin-root>` 指插件根目录。

在项目根目录使用 `.venv/Scripts/python` 运行 `scripts/run_evaluate.py`。参数默认读取 `.env`，命令行参数优先。完整参数见 `--help`，环境配置见[评估配置](references/evaluation-settings.md)。

## 用法

选择一种输入：

- `--factor-package <name>`：评估因子包。运行前检查代码、数据和已有验证结果是否一致，验证范围须覆盖评估区间；检查失败时停止，不自动运行 Validate。
- `--factor-data <directory> --factor-column <column>`：直接读取评分目录中的日度 Parquet。数据须含 `date, code` 和指定的评分列，不需要因子名称、源码包或验证记录。
- `--all-alpha`：评估已注册的全部非 Barra alpha 因子包。

三种选择互斥。因子包默认评估全部声明列，可用 `--factor-column` 选择列；包名与数据目录名不同时，用 `--storage-factor-name` 指定目录名。直接评分需用 `--reference-factor` 指定参考因子，否则不计算参考相关性；因子包缺省使用项目的 `barra_cne5_styles` 和注册表 `core` 集合，也可用 `--reference-factor` 明确选择已有参考因子。

`--task` 选择计算内容，可重复传入，省略时计算全部三项：

| 参数 | 内容 | 文件 |
| --- | --- | --- |
| `ic` | 日度 Rank IC | `ic.csv` |
| `portfolio` | 分层及 Top 组合的收益、换手、成本和净值 | `returns.csv` |
| `correlation` | 与参考因子的相关性 | `correlation.csv` |

`--operation` 选择操作，可重复传入，按下表顺序执行。省略时只计算；单独检查、绘图或更新文档不会重新计算。

| 参数 | 操作 |
| --- | --- |
| `evaluate` | 计算所选任务 |
| `check` | 检查完整结果及其使用的数据 |
| `plot` | 检查完整结果并生成总览图 |
| `readme` | 更新因子包 README 第 8 节和评估验收项，需完整指标和总览图 |

```sh
# Evaluate a package, check results, plot and update its README.
.venv/Scripts/python "<skill-root>/scripts/run_evaluate.py" --factor-package market_cap --operation evaluate --operation check --operation plot --operation readme

# Calculate IC for an existing score column.
.venv/Scripts/python "<skill-root>/scripts/run_evaluate.py" --factor-data artifacts/compositions/study/run_example/signals --factor-column score --task ic

# Evaluate a score directory directly.
.venv/Scripts/python "<skill-root>/scripts/run_evaluate.py" --factor-data artifacts/compositions/study/run_example/signals --factor-column score --output-dir artifacts/evaluations/run_example

# Plot existing complete results.
.venv/Scripts/python "<skill-root>/scripts/run_evaluate.py" --factor-data artifacts/compositions/study/run_example/signals --factor-column score --output-dir artifacts/evaluations/run_example --operation plot
```

## 注意事项

- 日期须满足 `begin <= train_end < validation_end < end`。收益未能在对应区间内实现的记录不参与统计，不填零。
- 直接评分的结果保存到 `<output-dir>/<column>/`，默认目录为 `$ARTIFACTS/factor_evaluation`；不同实验分别指定 `--output-dir`。因子包默认保存到 `$ARTIFACTS/factor_zoo/<name>/<column>/`。文件说明见[评估产物](references/evaluation-artifacts.md)。单项计算只更新自己的 CSV；三项的日期、参数和数据一致才标为 `complete`，否则为 `partial`，不能完整验收或绘图。
- `--refresh` 仅用于单个因子包，沿用已有设置刷新全部声明列，不能与 `--task`、`--factor-column` 或 `--rebuild` 混用。需要按当前参数重新计算时用 `--rebuild`。
- 直接评分不能使用 `readme` 或 `--refresh`。是否采用或发布因子由用户判断。
- `--factor-data` 的相对路径以 `--project-root` 为基准。评估只读取评分目录；单独检查或绘图时沿用相同输入和输出目录。

## 问题处理

先读终端错误和对应列的 `config.json`。

- 代码、数据或验证记录不一致：核对包名与目录；按原因重新 Calculate 或 Validate 后再评估。
- 日期不足或缺少输入：检查行情、行业、参考因子及评估区间，行情需覆盖收益退出日。
- 结果为 `partial`、文件缺失或数据已变化：统一参数和区间，补算所缺任务，必要时用 `--rebuild` 重算。
- 内存或临时空间不足：降低 `--workers`、`--threads` 或批次大小，检查临时目录空间。
- 绘图或 README 更新失败：先用 `--operation check` 检查结果，再核对绘图依赖、总览图、README 模板标记和文件写入权限。
