---
name: factor-benchmark
description: 使用脚本比较因子计算批次的耗时与峰值内存，诊断并修复基准失败。
---

# Factor Benchmark

运行前按 [Factor Protocol 安装说明](../factor-protocol/SKILL.md#安装)检查项目已有环境和依赖。`<path-to-skill>` 指本 Skill 的安装目录。

`scripts/run_benchmark.py` 自动完成隔离计算、批次比较、单日追加测试、配置选择和 README 回写。Agent 按需运行脚本，在基准失败时诊断和修复。

## 脚本入口

Review、Validate 通过后，在项目根目录使用已有环境运行。参数与用法以脚本帮助为准：

~~~shell
uv run --locked python "<path-to-skill>/scripts/run_benchmark.py" --help
~~~

一次调用完成全部候选与单日追加测试。候选使用相同的输出区间、线程和内存限制；运行前确认 `.env` 的 `FACTOR_WORKFLOW_NAME` 与目标因子一致，目录配置见 [`.env.example`](.env.example)。

## 结果与验收

脚本回写 README 第 7 节，并维护第 4 节第 5 项。验收要求全部声明列的单日追加通过，候选均为 `pass` 或 `oom`，且至少一个批次成功；失败以非零状态退出。

`pass` 表示全部候选成功；`complete` 表示候选已测完，至少一个触及 OOM 边界且峰值 RSS 可测。缺失资源记录、清理失败或未完成测试不能验收。

表中记录日均耗时和计算进程树的峰值 RSS。单日追加只计最后一天的计算与写入，不含历史准备及 Update 的验证、评估刷新。

验收后，脚本将日均耗时最短的成功批次及对应的线程、内存限制回填 `.env`。Calculate 按该配置显式传参。临时因子数据运行后清理，结果保留在 README。

## 失败介入

先读终端和 README 第 7 节的状态、失败原因及单日追加结果。

- 日历或预热不足：核对结束日期、源表分区和因子所需历史。
- 计算、数据身份或输出检查失败：核对因子包与目录，沿失败批次检查源数据和 Calculate 的错误。
- `oom` 或 `timeout`：检查批次、线程、内存和超时设置；资源调整后重跑完整基准，候选仍使用一致的比较条件。
- `blocked`、峰值 RSS 缺失或清理失败：检查运行权限、资源采集和隔离目录占用，处理后重跑。
- README 或 `.env` 回写失败：核对模板结果标记、目标因子配置及写入权限。

定义变化返回 Design；实现修改后重新 Review 和 Validate。修复后重跑脚本，按完整结果更新验收状态。

## 参考

- [因子计算](../factor-calculate/SKILL.md)：使用选定配置物化完整历史。
- [因子工作流](../factor-workflow/SKILL.md)：阶段顺序与返工路径。
