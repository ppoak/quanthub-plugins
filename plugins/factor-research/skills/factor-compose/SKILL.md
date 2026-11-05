---
name: factor-compose
description: 准备因子数据、训练合成模型并保存评分，使用 Factor Evaluate 评估评分目录。
---

# Factor Compose

优先使用项目已有的 Python 3.12+ 环境，按下文安装缺少的依赖。

通过 YAML 运行数据准备和模型训练，使用 Store 保存模型、评分和运行记录。配置方法见[使用说明](references/usage.md)，编写模型和训练流程见[开发说明](references/development.md)。

## 安装

先检查项目已有的 Python 包。缺少或需更新时，从 [Releases](https://github.com/ppoak/quanthub-plugins/releases) 选择 `factor-compose@版本号`，并按其说明选择兼容的 `factor-protocol` 版本，在已有 uv 项目目录一起安装：

```sh
uv add "factor-protocol @ https://github.com/ppoak/quanthub-plugins/releases/download/factor-protocol@<factor-protocol-version>/factor_protocol-<factor-protocol-version>-py3-none-any.whl" "factor-compose @ https://github.com/ppoak/quanthub-plugins/releases/download/factor-compose@<factor-compose-version>/factor_compose-<factor-compose-version>-py3-none-any.whl"
```

将命令中的版本占位符替换为实际发布版本，不带 `v`；文件名以 Release 附件为准。两个包的版本分别选择，无需相同。仅使用 Python 包无需安装插件。保留项目的 `pyproject.toml` 和 `uv.lock`，其他环境运行 `uv sync --locked`。复用已有环境和配置。

额外的模型库按所用模型安装。项目路径见 [Factor Protocol](../factor-protocol/SKILL.md#项目路径配置)，仅补充所需配置。

## 用法

在目标项目 `.env` 设置 `DATA` 和 `ARTIFACTS`，然后从该项目目录运行：

```sh
uv run --locked python -m factor_compose --config config/workflow.yaml
```

`--config` 可以指向数据准备或完整训练流程。终端进度写入标准错误流，标准输出为 JSON 结果，失败返回非零状态。数据准备返回 dataset ID，训练返回运行信息，完整 Workflow 的 `results` 保存各步骤结果。

训练完成后，将返回结果中的 `signals` 目录传给 [Factor Evaluate](../factor-evaluate/SKILL.md)。下方 `<path-to-skill>` 指 Factor Evaluate 的实际安装目录：

```python
import subprocess
import sys

subprocess.run([
    sys.executable, "<path-to-skill>/scripts/run_evaluate.py",
    "--factor-data", prediction["signals"],
    "--factor-column", "score",
    "--output-dir", "artifacts/evaluations/" + prediction["run_id"],
], check=True)
```

`prediction` 是已完成训练的返回结果。评分按日保存 `date, code, score`，评估直接读取原目录，默认计算 IC、组合收益和相关性。`--output-dir` 指定结果目录；参考因子、日期及绘图操作按 [Factor Evaluate 用法](../factor-evaluate/SKILL.md)设置。

## 注意事项

- 每个交易日、证券代码只能有一条评分。`code` 保持字符串，`score` 可为空，不能包含无穷值。
- 训练和选参只使用预测时已经可知的特征与已兑现收益；保存训练截止日、特征顺序和数据来源。
- 数据准备和训练须使用相同的项目目录与 `ARTIFACTS`。评估前确认训练成功结束，不同实验使用不同的评估结果目录。
- 评估日期须满足 `begin <= train_end < validation_end < end`。训练使用的收益标签与正式评估参数分别设置，不能直接当作同一结果。
- [合成示例](examples/workflow.py)在临时目录展示训练和评分保存，结束后清理示例产物，不运行历史评估。

## 问题处理

先查看终端错误和对应运行目录的 `run.json`。

- 配置类无法导入：检查 `class` 中的模块名、类名和当前项目目录。
- 数据列、日期或评分重复：检查数据准备结果，按 `date, code` 定位重复或缺失记录。
- 运行未完成或文件检查失败：核对 run ID 和项目目录，从有效断点恢复训练，或重新生成结果。
- 模型加载失败：使用训练时的依赖版本，并确认模型类仍可导入。
- 评估失败：检查评估日期、行情、行业和参考因子，按 [Factor Evaluate 问题处理](../factor-evaluate/SKILL.md#问题处理)排查。
