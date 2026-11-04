---
name: factor-calculate
description: 使用脚本物化因子历史数据，诊断并修复计算失败。
---

# Factor Calculate

运行前按 [Factor Protocol 安装说明](../factor-protocol/SKILL.md#安装)检查项目已有环境和依赖。`<path-to-skill>` 指本 Skill 的安装目录。

`scripts/run_calculate.py` 自动完成分批计算、输出检查、数据身份提交和收尾独立复算。Agent 按需运行脚本，在计算失败时诊断和修复。

## 脚本入口

Review、Validate、Benchmark 通过后，在项目根目录使用项目虚拟环境运行。参数与用法以脚本帮助为准：

~~~shell
uv run --locked python "<path-to-skill>/scripts/run_calculate.py" --help
~~~

用 `--begin/--end` 指定输出区间，批次、线程和内存显式使用 Benchmark 选定配置。目录与完整历史范围配置见 [`.env.example`](.env.example)。

## 结果与验收

脚本检查日期边界、声明列、唯一 `(date, code)` 键和有限值，通过后提交 Parquet 数据与 `identity.json`。物化检查失败保留原数据；符合因子定义的零输出日无需补零。

正式验收覆盖包内全部声明列，以及 `FACTOR_CALCULATE_BEGIN/END` 指定的完整历史；未配置时使用源日历全范围。脚本自动调用 Validate 做收尾独立复算，回写 README 第 6 节并保留 `audit.csv`、`config.json`。完整历史检查与收尾复算均通过才完成验收，维护 README 第 4 节第 4、6 项；失败以非零状态退出。

指定列、使用独立存储名或 `--no-readme-update` 会跳过收尾复算。

## 失败介入

先读终端的失败阶段和原因；收尾复算失败时，再读 README 第 6 节及对应列的审计产物。

- 已有数据身份缺失、定义或历史摘要变化：核对因子包与数据目录，重算完整已存区间，不改写身份来接受变更。
- 日历、输出列、键或数值异常：沿 `(date, code)` 检查源分区、预热、过滤条件和 SQL 中间结果。
- 资源不足：检查批次、线程、内存限制及临时目录空间；计算资源调整后重新 Benchmark，再重跑计算。
- 权限、目录占用或计算中断：核对写入权限与同因子任务；脚本重跑时恢复目录，也可用 `--recover-only` 只恢复中断计算。
- 收尾复算失败：数据已提交但验收未通过，按 [Factor Validate](../factor-validate/SKILL.md) 排查，修复后重跑；通过前不进入 Evaluate。

定义变化返回 Design；实现修改后重新 Review、Validate 和 Benchmark，再执行完整计算。

## 参考

- [Factor Protocol](../factor-protocol/SKILL.md)：运行依赖、因子接口与存储。
- [因子数据身份](../factor-protocol/references/data-identity.md)：历史摘要与重算要求。
- [因子工作流](../factor-workflow/SKILL.md)：阶段顺序与返工路径。
