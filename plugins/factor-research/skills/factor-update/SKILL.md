---
name: factor-update
description: 按项目现有数据增量更新指定因子包或 schema 配置块，并按需写回 README。
---

# Factor Update

运行前按 [Factor Protocol 安装说明](../factor-protocol/SKILL.md#安装)检查项目环境；更新包含评估刷新，还需检查 [Factor Evaluate 的依赖](../factor-evaluate/SKILL.md)。`<path-to-skill>` 为安装目录。

## 唯一命令入口

从项目根目录使用项目虚拟环境运行 `scripts/run_update.py`，参数以 `--help` 为准。

`--target` 接受包名或 schema 配置块名，例如 market_cap、active、core。先匹配 `FACTORZOO_SCHEMA` 中的配置块，否则匹配因子包；空块或未知目标报错。配置块沿用 Factor Protocol 格式：active 使用包、指纹和完整列配置，core 等块使用列引用列表。引用按包去重，更新完整包。配置块指纹须与源码一致，列须存在；active 必须包含包内全部声明列。

## 任务与预览

`--task` 可以重复传入，去重后固定先 update、后 readme；省略时只执行 update。

| 任务 | 行为 |
| --- | --- |
| `update` | 增量计算、独立验证、评估检查与绘图、保存运行报告；不修改 README |
| `readme` | 使用已有完整评估结果及运行报告回写 README，不重新计算因子或评估指标 |

`--dry-run` 是独立预览开关，可与任何任务组合；只读取配置、产物和更新计划，不启动执行阶段、不恢复目录、不保存报告、不修改 README，也不创建临时目录。readme-only 预览要求已有报告；update + readme 预览会说明更新成功后将写回文档。

```sh
# Update without README changes.
uv run --locked python "<path-to-skill>/scripts/run_update.py" --target active

# Update, then write README.
uv run --locked python "<path-to-skill>/scripts/run_update.py" --target market_cap --task update --task readme

# Rewrite existing reports only.
uv run --locked python "<path-to-skill>/scripts/run_update.py" --target market_cap --task readme

# Preview both tasks without writes.
uv run --locked python "<path-to-skill>/scripts/run_update.py" --target core --task update --task readme --dry-run
```

## Benchmark 资源门禁

每个包单独读取 README 第 7 节的标准 `factor-benchmark:results` 表；要求第 4 节第 5 项 Benchmark 验收已勾选、候选均已完成为 pass 或 oom、至少一个候选成功、单日追加通过、资源测量齐全且候选比较条件一致。只识别当前 Benchmark 脚本输出的列格式，不从历史自然语言中猜测。

默认从成功候选按日均耗时、峰值 RSS、每批交易日数依次升序选择，与 Benchmark 的选择规则一致。选择依据为 README 中已保存的测量值，正式增量运行记录不参与候选选择。

线程、内存和批次没有固定默认值，也没有包名特例。显式传入资源参数时，只能选择表内已经通过的完整参数组合；没有匹配组合、验收未通过、旧格式或记录缺失时，要求先运行 Factor Benchmark，禁止回退到 1 线程、1GB 或固定批次。内存必须记录显式限制，`DuckDB default` 不作为可跨运行复用的配置。

同一配置块中的各包使用各自选出的配置；计算、验证和评估共用所选线程与内存设置。Benchmark 仅测量因子计算，不能据此保证独立验证和评估也一定满足资源要求；下游资源失败如实退出，不自动增加额度或跳过阶段。

| 参数 | 默认值 | 用途 |
| --- | --- | --- |
| `--target` | 必填 | 因子包或 schema 块 |
| `--task` | `update` | 可重复选择 update、readme |
| `--dry-run` | 关闭 | 只预览，不执行写操作 |
| `--project-root` | `.` | 项目根目录 |
| `--threads` | 当前包的 Benchmark 配置 | 筛选已通过的线程数 |
| `--memory-limit` | 当前包的 Benchmark 配置 | 筛选已通过的内存限制 |
| `--compute-batch-days` | 最快成功候选 | 筛选已通过的批次 |
| `--temp-directory` | 自动生成 | 临时计算目录，相对路径基于项目 |

数据、源码、注册表和产物路径读取项目 `.env` 的 DATA、FACTORZOO、FACTORZOO_SCHEMA、ARTIFACTS。没有 operation、no-readme-update、初始化或跳过验证/评估的用户参数。初始物化、补算及重建使用 Factor Calculate。

## 结果与失败处理

Update 校验数据身份、schema、源分区改写与实际日历，增量区间从已有输出尾日之后开始；定义或数据身份变化返回 rebuild_required。没有新增日期也检查验证与评估。内部 Calculate、Validate 都不修改 README；Evaluate 在 update 任务中只计算、检查和绘图。

更新提交后独立验证最近 5 个有输出日期，随后刷新全部声明列的评估。失败标记保存为 `.tmp/factor_update/<factor>/refresh_failed.json`；下次重试。只有更新成功才保存新的 `readme_result.json`，保存报告与写回 README 分离。文档失败保留报告，可用 `--task readme` 重试。

readme 任务调用 Evaluate 的文档操作，写回评估结果，再将运行报告写入第 7 节；不会重新跑计算。正式增量记录覆盖本次区间、耗时、进程树 RSS、资源和行数，保留原有 Benchmark 表。零输出记录为 0 行。详见[生产增量更新](references/production-update.md)。

## 实现结构

`scripts/` 仅有 run_update.py 入口，内部代码位于 core：selection 选择目标；resources 解析并检验 Benchmark；models 定义配置、计划和结果；sources、preflight、planning 处理日历、数据门禁和规划；tasks 下 calculate、validate、evaluate、readme 分别适配阶段；workflow、refresh 编排单包；update 调度任务；state 保存刷新状态；process、monitor、report 负责子进程、测量及报告。
