# 生产增量更新

## 目标与资源

唯一入口为 `<path-to-skill>/scripts/run_update.py`。目标使用 --target，任务使用可重复的 --task update/readme；--dry-run 独立控制只读预览。

```shell
uv run --locked python "<path-to-skill>/scripts/run_update.py" --target active --dry-run
uv run --locked python "<path-to-skill>/scripts/run_update.py" --target market_cap --task update --task readme
```

update 任务为每个包解析 README 的标准 Benchmark 候选及单日追加表，核验 Benchmark 验收并选择最快成功配置。没有固定线程、内存或批次默认值；显式参数也须匹配已通过的组合。缺失、未完成、资源记录不明确或旧格式均要求返回 Factor Benchmark。

计算、验证和评估共用选定线程和内存；计算 Benchmark 不保证下游阶段一定成功。首次物化和历史补算仍属于 Calculate。全部参数及任务语义见 [Skill 入口](../SKILL.md)。

## 更新门禁

- 配置块中的指纹与源码一致，引用列必须存在。active 的列清单必须与包的全部声明列一致。
- `identity.json` 与当前定义指纹、声明列、整个已有历史摘要一致。身份缺失或变化返回 `rebuild_required`，不自动接受新身份。
- 现有数据抽样 schema 与声明列不符时返回 `schema_mismatch`。
- 验证产物检查要求每列 config、非空 audit、pass 状态、正确的包身份、覆盖输出尾日的验证尾日及匹配的数据快照。缺失或失败为 `validation_failed`，日期或快照过期为 `validation_stale`。没有新增日期时也会按需重试验证。

尚无物化数据的包返回 `uninitialized`，先使用 Factor Calculate 完成物化。`--dry-run` 不调用计算、验证、评估或报告写回。

## 日期与计算

根据实际因子尾日确定新增源日期。`UPDATE_CALENDAR` 指定源日历；`quotes_common` 对 `UPDATE_CALENDAR_TABLES` 的日期取交集。`UPDATE_SOURCE_FACTORS` 声明的上游因子也参与日历交集。

声明 `UPDATE_SOURCE_TABLES`、共同日历或上游因子依赖时，检查最近 400 个源日期的源分区修改时间是否晚于同日因子输出。检测到改写返回 `rebuild_required`，不在 Update 内重建历史。该检查是现有修改时间检查，不等同完整上游内容快照。

Calculate 子进程负责暂存、输出列、日期边界、唯一键、有限值检查和原子提交，使用 `--require-values --no-readme-update`。失败保留原数据；真实运行前通过 Calculate 恢复中断目录。Update 的验证阶段另行执行，不依赖 Calculate 的 README 收尾模式。

因子按定义决定是否输出；末尾零输出日期可能在后续更新中再次计算。返回的实际输出尾日取物化分区，评估目标尾日仍取上游日历尾日。

## 验证与评估

Calculate 成功后，Validate 复算最近 5 个有输出日期，保存 audit/config，不修改 README 第 6 节。验证成功后 Evaluate 刷新全部声明列，执行 evaluate、check、plot；readme 任务单独调用评估文档写回，update 本身不修改 README。

没有新增日期时也检查验证状态并刷新评估。后续阶段失败保存 `.tmp/factor_update/<factor>/refresh_failed.json`，命令非零退出，下次运行重试；此前已经提交的数据不回滚。

## 报告与调度

成功更新后仅保存测量报告，不修改 README。dry-run、up_to_date 和失败不生成新实测记录；readme 任务使用已有报告写回文档。

```shell
uv run --locked python "<path-to-skill>/scripts/run_update.py" --target active --task update --task readme
uv run --locked python "<path-to-skill>/scripts/run_update.py" --target market_cap --task readme
```

`--task readme` 使用完整已有评估产物和 `readme_result.json` 写回文档，不运行数据更新。项目调度器继续调用唯一的 run_update 入口。
