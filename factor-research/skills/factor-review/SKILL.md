---
name: factor-review
description: 全面审查因子定义与实现，核对数据时点、频率、公式、单位、边界和计算独立性。
---

# Factor Review

运行前按 [Factor Protocol 安装说明](../factor-protocol/SKILL.md#安装)检查项目已有环境和依赖。`<path-to-skill>` 指本 Skill 的安装目录，`<path-to-plugin>` 指插件根目录。

## 目标

按照下列要求完成因子审核，确认实现符合 README 第 1–5 节的研究规格，并核对公式、频率、单位、时间对齐、边界处理、计算独立性和运行假设；重点排查未来信息，给出是否可以进入 `factor-validate` 的结论。

## 输入与输出

输入为已完成设计和开发的因子包及其研究依据：

- `$FACTORZOO/<factor_name>/README.md`；
- `$FACTORZOO/<factor_name>/scripts/main.py` 及其按职责拆分的模块；
- 实际输入的字段、有界样本、因子 README 中的数据用法和必要的参考文献；读取方法见[因子数据读取](../factor-protocol/references/data-access.md)。

Review 不修改因子实现、定义或包内配置。结论、证据和交接建议在回复中汇报；生产与验证实现符合定义、实现相互独立、未来数据和时点可见性审查均通过后，在 README 第 4 节勾选第 1、2、3 项并提交同步。失败或结论失效时取消对应勾选。

## 审查清单

### 1. 定义、频率与接口

- 实际计算公式及其变量、符号、过滤条件和参数与 README 一致，因子值的经济含义和方向清楚；
- 输入与输出频率、交易日口径、时间戳含义、单位、窗口、预热期、输出日和边界规则与 README 一致；
- `FACTOR_NAME`、`FACTOR_COLUMNS`、`VALIDATION_INPUT_COLUMNS` 与 README 第 2–3 节一致，输出列稳定、唯一；
- 每个用于对比的显式参数均映射到对应输出列，列值与参数含义相符。

### 2. 未来信息

- 逐字段核对数据可见时间与事件、公告或生效时间，并检查因子日对齐、关联条件、窗口方向、排序和滞后逻辑；审计依据是代码中的时序逻辑及来源可见时间证据。
- 特别检查 `Back Fill`：它可能无意中把后续时点才可见的值填入更早时点，造成未来信息泄漏。严禁将未来值向更早时点回填；历史修订值只能从其实际可见时点起参与计算。
- 财务类数据按 README 定义的 ASOF 与科目优先级处理，只采用输出时点已可见的非 NULL、有限值；较新的空记录不得覆盖此前仍有效的值。

### 3. 两条计算路径

- `calculate_factor` 使用 DuckDB SQL 完成关联、窗口、聚合、排序、回归和因子值计算；Python 负责参数处理、有界调度和 SQL 组装。生产计算不得使用 Pandas、NumPy、SciPy、DataFrame、`.df()` 或 `.fetchdf()` 计算因子值；
- `compute_validation_audit` 使用独立 Python/Pandas/NumPy，从最小原始输入重新计算期望值；不得调用生产 SQL、生产计算 helper、`calculate_factor` 或已物化因子列；
- 两条路径的字段、可见时点、窗口、过滤、缺失值、零分母、非有限值和输出日期语义一致，计算实现相互独立。

### 4. 键、边界与状态

- 输出 `(date, code)` 唯一；NULL、零分母、`inf`、常数输入、缺失分区和空窗口的结果符合 README；
- `source_begin`、`output_begin`、`output_end` 含义明确且范围有界，预热数据只参与计算；读取列和日期范围经过裁剪；分钟滚动一般不超过 20 个交易日，日频滚动一般不超过 240 个交易日；
- 仅在存在跨日状态依赖时使用持久缓存，路径为 `$ARTIFACTS/cache/<factor_name>/`，并记录输入区间、参数、schema、代码指纹与输入数据摘要及唯一键。

### 5. 协议与资源

- 因子包符合[因子包接口说明](../factor-protocol/references/factor-package-protocol.md)，模块职责清楚，每个 Python 模块不超过 600 行；
- 核对扫描范围、列裁剪和中间数据规模，并记录内存工作集、线程、临时目录及 `compute_batch_days` 假设，供后续基准测试使用。

## 判定与交接

全部阻断项通过才判定 `pass`。汇报审核结论、关键证据及未解决的阻断项。

- `pass`：完成验收勾选和提交同步，将后续执行交给用户；
- `fail`：列出最小修复项并退回 `factor-develop`；
- 定义或数据口径不清时，暂停审核并请求决策。

交接前核对并补齐项目 `.env` 的目录、阶段日期和默认参数，确保 `FACTOR_WORKFLOW_NAME` 与目标因子一致；配置见[示例](../factor-protocol/.env.example)。回复中将实际因子名称和日期填入下列命令，Calculate 的资源参数在 Benchmark 通过后按结果填写。后续阶段由用户运行，Agent 在用户请求诊断或修复时再介入。

## 后续执行

在项目根目录激活项目虚拟环境，以下 `python` 指向该环境。按 Validate → Benchmark → Calculate → Evaluate 顺序运行，逐项通过后再进入下一阶段。Evaluate 完成后，用户同意发布才运行 Publish；需要改善效果时进入 [Factor Improve](../factor-improve/SKILL.md)。执行前替换占位值。

~~~shell
uv run --locked python "<path-to-plugin>/skills/factor-validate/scripts/run_validate.py" --factor-name <factor_name> --sample --begin <validate_begin> --end <validate_end>
uv run --locked python "<path-to-plugin>/skills/factor-benchmark/scripts/run_benchmark.py" --factor-name <factor_name> --end <benchmark_end> --threads <threads> --memory-limit <memory_limit>
uv run --locked python "<path-to-plugin>/skills/factor-calculate/scripts/run_calculate.py" --factor-name <factor_name> --begin <calculate_begin> --end <calculate_end> --compute-batch-days <batch_days> --threads <threads> --memory-limit <memory_limit>
uv run --locked python "<path-to-plugin>/skills/factor-evaluate/scripts/run_evaluate.py" --factor-package <factor_name> --end <evaluate_end> --operation evaluate --operation check --operation plot --operation readme
uv run --locked python "<path-to-plugin>/skills/factor-publish/scripts/run_publish.py" --factor-name <factor_name>
~~~

Calculate 包含收尾验证，批次、线程和内存按 Benchmark 结果显式传入。其余参数与用法见各脚本 `--help`。

## 参考链接

- [factor-design](../factor-design/SKILL.md)
- [factor-develop](../factor-develop/SKILL.md)
- [factor-package-protocol](../factor-protocol/references/factor-package-protocol.md)
- [high-frequency-development](../factor-develop/references/high-frequency-development.md)
- [factor-validate](../factor-validate/SKILL.md)
- [factor-benchmark](../factor-benchmark/SKILL.md)
