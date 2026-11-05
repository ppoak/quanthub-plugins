# 开发说明

通过以下接口编写自己的数据准备、模型和训练流程。业务类放在模块顶层，以便 YAML 导入和模型恢复。

| 接口 | 使用方法 |
| --- | --- |
| Preprocessor | 实现 `read()` 读取数据，`transform(frame)` 处理特征和训练目标 |
| Composer | 实现 `fit(data, context=...)` 训练模型、`predict(data)` 返回评分 |
| Workflow | 按步骤运行配置，或覆盖 `run()` 实现滚动训练和选参 |
| Store | 保存数据、模型、评分和运行记录 |

## 准备数据

Preprocessor 默认依次读取、处理、保存数据，返回 dataset ID。数据以日度 `date` 和字符串 `code` 确定每条记录，特征顺序由 `features` 指定，训练目标及其可知日期默认使用 `ret`、`exit_date`。

需要分批或保存处理状态时，覆盖 `run()`，向 `Store.freeze` 传入数据批次、特征列、状态和诊断表。`read_dataset` 按日期和特征读取数据，`scan_dataset` 供 DuckDB 查询，`load_preprocessor` 恢复已保存的处理状态。滚动特征需读取足够的预热历史。

## 训练与预测

Context 提供训练截止时间、有序特征、目标列及可选验证数据、权重和随机种子。`fit` 只使用截止时已知的数据；`Preprocessor.labeled` 按收益可知日期筛选训练样本，`strict=True` 排除截止日当天才兑现的收益。

`predict` 返回输入的全部 `date, code` 及 `score`，保持键一致，缺失评分保留。多模型组合可在 Composer 中实现；训练 Workflow 负责窗口、选参及重新训练。

## 保存与恢复

运行开始时调用 `Store.start`。通过 `save_model` 保存模型及数据集、特征顺序、训练截止日，用 `write_scores` 保存预测，成功后调用 `complete` 并返回 `result(run_id)`；异常时调用 `fail` 记录错误。

`load_model` 恢复模型，模型声明的依赖版本需与保存时一致。`workspace` 保留中断的工作目录，成功后清理；`freeze` 可复用检查通过的分区，`resume=False` 重建未完成的数据缓存。训练恢复位置由研究 Workflow 根据模型、评分和设置确认。

## 评估评分

将已完成运行返回的 `signals` 目录传给 Factor Evaluate 的 `--factor-data`，评分列使用 `--factor-column score`，用 `--output-dir` 指定评估结果目录。

按[使用说明](usage.md#运行评估)设置评估日期和参考因子。训练选参使用当时已知的收益，正式评估使用 Factor Evaluate 指定的价格和持有期。

完整实现见[扩展示例](../examples/extensions.py)，可运行[合成示例](../examples/workflow.py)查看训练和评分保存的调用顺序。
