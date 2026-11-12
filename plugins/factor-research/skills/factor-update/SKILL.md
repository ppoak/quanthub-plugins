---
name: factor-update
description: 补齐已有因子的最新数据，刷新验证、评估和总览图，并按需更新 README。
---

# Factor Update

运行前检查[项目环境](../factor-protocol/SKILL.md#安装)和[评估依赖](../factor-evaluate/SKILL.md)。首次计算、补算历史或重建数据，使用 Factor Calculate。

## 使用

从项目根目录运行。`<path-to-skill>` 替换为本技能的安装目录。

`--target` 可以填写因子包名，也可以填写项目 schema 中的组名，例如 active、core。同名时优先按组处理；每个包的全部列一起更新。

先预览更新计划，不修改数据和文档：

```sh
.venv/Scripts/python "<path-to-skill>/scripts/run_update.py" --target active --dry-run
```

更新数据、验证、评估和总览图，保存运行记录：

```sh
.venv/Scripts/python "<path-to-skill>/scripts/run_update.py" --target active
```

需要同时更新 README 时：

```sh
.venv/Scripts/python "<path-to-skill>/scripts/run_update.py" --target market_cap --task update --task readme
```

只用已有结果更新 README，不重新计算：

```sh
.venv/Scripts/python "<path-to-skill>/scripts/run_update.py" --target market_cap --task readme
```

## 参数

| 参数 | 用途 |
| --- | --- |
| `--target` | 必填，指定因子包或组名 |
| `--task` | 默认 update；可重复填写 update、readme，固定先更新再写文档 |
| `--dry-run` | 只预览，可用于任意任务组合 |
| `--project-root` | 项目目录，默认当前目录 |
| `--threads` | 选择 Benchmark 已通过的线程数 |
| `--memory-limit` | 选择 Benchmark 已通过的内存限制 |
| `--compute-batch-days` | 选择 Benchmark 已通过的每批交易日数 |
| `--temp-directory` | 临时文件目录，默认由脚本安排 |

通常不需要指定资源参数。脚本从各因子 README 的 Benchmark 表中选择最快的成功配置；手动指定时也必须匹配已通过的组合。记录缺失、不完整或格式无法识别时，先完成 Factor Benchmark，不自行猜测配置。计算、验证和评估使用所选线程与内存，后续阶段若资源不足，先处理失败再重试。

数据、因子包、schema 和结果目录读取项目 `.env`，完整用法见 `--help`。

## 更新规则

脚本从已有因子最后一个输出日之后，按源数据的可用交易日期补算；验证本次新增输出中最新至多 5 日，不足 5 日就验证全部新增日期，然后刷新评估和总览图。没有新增计算时，仍会检查已有验证和评估，必要时重试；重试验证使用已有输出的最新至多 5 日。

默认不改 README，只有选择 readme 任务才写入评估结果和运行记录。`--dry-run` 下只预览，不执行写操作。

## 失败处理

- Benchmark 记录不完整：先运行 Factor Benchmark。
- 因子定义、输出列或已有历史数据发生变化：按报错要求使用 Factor Calculate 重建，再继续更新。
- 计算、验证或评估失败：查看报错，修复原因后重试，不跳过失败步骤。已提交的数据不会因后续失败自动撤销。
- 仅文档写回失败：使用 `--task readme` 重试，无需重新更新数据。

检查所有目标的执行结果，全部成功后才算本次更新完成。
