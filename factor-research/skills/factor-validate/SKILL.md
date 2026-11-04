---
name: factor-validate
description: 使用脚本执行因子独立验证，诊断并修复验证失败。
---

# Factor Validate

运行前按[插件说明](../../README.md)准备项目环境。`<skill-root>` 指本 Skill 的安装目录，`<plugin-root>` 指插件根目录。

`scripts/run_validate.py` 自动完成独立复算、覆盖检查、误差审计和 README 回写。Agent 按需运行脚本，在验证失败时诊断和修复。

## 脚本入口

在项目根目录激活项目虚拟环境后运行，参数与用法以脚本帮助为准：

~~~shell
.venv/Scripts/python "<skill-root>/scripts/run_validate.py" --help
~~~

`--sample` 先隔离计算当前 SQL 结果，再独立复算；省略时检查已有数据，供 Calculate、Update 内部调用。默认配置见 [`.env.example`](../../.env.example)。

## 结果与验收

脚本回写 README 第 6 节，并维护第 4 节第 4 项；全部声明列通过才勾选，失败以非零状态退出。未来数据审查和完整历史唯一键检查分别由 Review、Calculate 负责。

临时计算文件在验证结束后清理，结果保留在 README；检查已有数据时保留 `audit.csv` 和 `config.json`，供评估使用。检查失败或历史因子数据变化时，现有评估标为 `stale`。产物字段与通过条件见[验证规范](references/validation-specification.md)。

## 失败介入

先读终端和 README 的失败原因；检查已有数据时，再核对对应列的 `audit.csv`、`config.json`。

- 配置、数据身份或指纹不符：核对因子包、日期和根目录；代码变化后重新计算，不改写身份或沿用旧数据。
- 覆盖、空值、重复键、非有限值或超差：沿 `(date, code)` 检查源数据、预热、缺分钟处理和 SQL/Python 中间值。
- `sample_insufficient`：补足有效样本与预热。
- 资源不足：调整线程、内存或审计规模，横截面计算仍保留全市场语义。

不得通过放宽容差、填零或删除失败行掩盖错误。定义变化返回 Design；实现修改后重新 Review 和 Validate。修复后重跑脚本并验证全部声明列，由真实结果恢复验收状态。

## 参考

- [因子数据身份](../factor-protocol/references/data-identity.md)：身份、指纹和数据快照。
- [因子工作流](../factor-workflow/SKILL.md)：阶段顺序与返工路径。
