# 使用说明

## 配置与运行

业务类放在项目可导入的 Python 模块中。YAML 用 `class: module:Class` 指定类，`params` 传入构造参数，`inputs` 传入 `run()` 参数。`$config` 读取其他配置，`$ref` 读取前面步骤的返回值。

```yaml
class: factor_compose:Workflow
params:
  name: example
  steps:
    - id: prepared
      stage: config/preprocessor.yaml
    - id: composed
      stage: config/training.yaml
      params:
        dataset: {$ref: prepared}
```

`preprocessor.yaml` 选择数据准备类，`training.yaml` 选择训练 Workflow 和模型。步骤按顺序执行，步骤内的 `params` 覆盖阶段配置的同名参数。`$ref` 可读取结果字段，例如 `{$ref: composed.run_id}`。

```sh
.venv/Scripts/python -m factor_compose --config config/workflow.yaml
```

Python 使用相同配置：

```python
from factor_compose import run

result = run("config/workflow.yaml")
prediction = result["results"]["composed"]
```

## 保存评分

训练流程使用 Store 保存结果：

```python
store.write_scores(run_id, predictions)
store.complete(run_id)
prediction = store.result(run_id)
```

评分须包含唯一、非空的日度 `date, code` 和数值 `score`，允许缺失评分。`prediction["signals"]` 是保存后的评分目录。也可通过 `store.write_prediction(name, scores)` 单独保存预测，其返回值的 `prediction_path` 指向评分目录。

| 产物 | 位置与内容 |
| --- | --- |
| 冻结数据 | `$ARTIFACTS/compositions/datasets/<dataset_id>/`，含日分区和数据说明 |
| 模型与评分 | `$ARTIFACTS/compositions/<name>/<run_id>/` 下的 `models/`、`signals/` |
| 运行记录 | 同一运行目录下的 `run.json`，记录设置、进度和错误 |
| 单独预测 | `$ARTIFACTS/compositions/predictions/<name>/` 下的日分区 |

`read_scores(prediction)` 读取评分；训练结果输入会检查运行完成状态及文件是否变化。

## 运行评估

将评分目录传给 Factor Evaluate 的 `--factor-data`，用 `--output-dir` 指定结果目录：

```python
import subprocess

subprocess.run(
    [".venv/Scripts/python", "<plugin-root>/skills/factor-evaluate/scripts/run_evaluate.py",
     "--factor-data", prediction["signals"],
     "--factor-column", "score",
     "--output-dir", "artifacts/evaluations/" + prediction["run_id"],
     "--operation", "evaluate", "--operation", "check", "--operation", "plot"],
    check=True,
)
```

该命令计算全部三项指标并检查、绘图。只计算一项时追加 `--task ic`、`--task portfolio` 或 `--task correlation`；参考因子用 `--reference-factor` 指定。

评估日期默认读取项目配置，可用 `--begin`、`--train-end`、`--validation-end`、`--end` 覆盖。训练期和验证期截止日须位于评估区间内，行情须覆盖持有期退出日。评估前确认训练完成及评分范围，不要求因子包或 Validate 记录。

评估结果保存到指定目录下的 `score/`。文件含义见[评估产物](../../factor-evaluate/references/evaluation-artifacts.md)，单独检查、绘图及重新计算见 [Factor Evaluate](../../factor-evaluate/SKILL.md)。
