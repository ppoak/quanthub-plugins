---
name: factor-workflow
description: 编排因子从设计、开发、验证到评估、效果改进、发布和生产更新的全流程。
---

# 因子工作流

运行前按 [Factor Protocol 安装说明](../factor-protocol/SKILL.md#安装)检查项目已有环境和依赖。`<path-to-skill>` 指本 Skill 的安装目录。

本技能统筹因子从研究来源、定义设计、实现、审查、计算、验证、评估、效果改进、发布到生产更新的流程，明确阶段顺序、进入条件、交付物和返工路径。各阶段按对应技能执行。因子包是最小发布和更新单位，包内声明列共同计算、共同更新，并整体发布。

## 项目配置与参数

在目标项目准备 Python 环境和 `.env`，配置数据、因子源码及产物目录，配置项见 [`.env.example`](../factor-protocol/assets/.env.example)。阶段设置见对应 Skill 的配置示例，当前因子名称见[工作流配置](assets/.env.example)。使用独立工作树时，共享数据目录可配置为绝对路径。

按[数据读取说明](../factor-protocol/references/data-access.md)查看实际表目录、字段、分区和样本。根据研究目标选择数据，并在因子 README 确定字段、时间对齐、预热和输出范围。阶段脚本用 `--project-root` 指定项目，`--factor-name` 指定因子包；Evaluate 使用 `--factor-package`。`.env` 仅保存在本地。

日期按所用数据与依赖因子的日历设置，截至可用的最近完整交易日 `T`。各阶段参数如下：

| 参数 | 设置方式 |
|---|---|
| `FACTOR_VALIDATE_BEGIN/END` | 可取截至 `T` 最近 5 个有效交易日；独立复算预热由验证脚本按定义加载 |
| `FACTOR_BENCHMARK_END` | 固定为 `T`；标准基准脚本使用此前 3 个月的实际交易日，比较 1、5、10、20、30 日批次 |
| `FACTOR_CALCULATE_BEGIN/END` | 截止日为 `T`；起点按用户指定范围、因子定义与数据可用性确定 |
| `FACTOR_EVALUATE_END` | 固定为 `T`；收益可观察截止日由评估脚本按执行滞后和持有期确定 |
| `FACTOR_CALCULATE_THREADS/MEMORY_LIMIT/COMPUTE_BATCH_DAYS` | 先设置候选资源配置，Benchmark 验收通过后由脚本回填日均耗时最短的通过配置 |
| `FACTOR_BENCHMARK_MEMORY_LIMIT` | 根据可用资源设置，并在候选批次间保持一致 |

## 生命周期

~~~mermaid
flowchart LR
    S[来源] --> D[设计]
    D --> DEV[开发]
    DEV --> R[审查]
    R -- 通过 --> SV[验证]
    SV -- 通过 --> B[批次基准]
    B -- 完成 --> C[完整历史计算]
    C -- 收尾验证通过 --> E[评估]
    E -- 用户同意发布 --> P[发布与 Release 转正]
    E -- 需要改进 --> I[效果改进]
    I -- 构造方案 --> D
    P --> M[用户合并]
    M --> U[生产更新]
    R -- 返工 --> DEV
    SV -- 需修改 --> DEV
    B -- 需调整 --> DEV
    C -- 收尾验证失败 --> DEV
~~~

## 阶段与交接

| 阶段 | 进入条件与产物 | Skill |
|---|---|---|
| 来源 | 研究问题、可追溯依据和数据可用性明确 | — |
| 设计 | 完成 README 第 1–5 节，确定公式、时点可见性、边界、参数、输出列和文献来源；创建同名 prerelease 并上传参考 PDF；创建与因子分支同名、指向 `main` 的 Draft PR | [factor-design](../factor-design/SKILL.md) |
| 开发 | 完成 DuckDB SQL 生产实现和独立 Python 验证实现 | [factor-develop](../factor-develop/SKILL.md) |
| 审查 | 核对 README 与实现、未来数据风险、键、状态、独立性和资源边界 | [factor-review](../factor-review/SKILL.md) |
| 验证 | 在 Validate 内先用正式计算引擎和临时存储名物化当前代码的隔离样本，再独立复算；横截面保留完整股票池，回写 README 第 6 节。 | [factor-validate](../factor-validate/SKILL.md) |
| 基准 | 验证通过后固定输出区间，比较 1、5、10、20、30 个交易日批次；脚本回写 README 第 7 节结果表 | [factor-benchmark](../factor-benchmark/SKILL.md) |
| 完整计算 | 使用选定配置物化正式历史范围，收尾检查联合唯一键并调用 factor-validate 抽样独立复算，回写 README 和审计资产；通过后进入评估 | [factor-calculate](../factor-calculate/SKILL.md) |
| 评估 | 对全部声明列和变体生成 SQL 指标、增量状态和图表，并回写 README 第 8 节摘要 | [factor-evaluate](../factor-evaluate/SKILL.md) |
| 效果改进 | 按研究需要检视全部评估结果，诊断 IC、分层和多头收益，提出具体构造方案并交回设计 | [factor-improve](../factor-improve/SKILL.md) |
| 发布 | 用户同意后，脚本核验并上传完整资产，同步 README，转正 Release 并设为 latest；提交推送后通过 `sync_pr.py` 同步 Design 已创建的 Draft PR 描述 | [factor-publish](../factor-publish/SKILL.md) |
| 合并后 | 用户人工合并 PR；核验 Design 已上传的参考 PDF；同步并清理因子分支 | [factor-publish](../factor-publish/SKILL.md) |
| 生产更新 | 上游数据就绪后，更新 active 且指纹匹配的已物化因子；脚本回写 README 第 7 节，启用评估刷新时同步更新第 8 节。定义变化或历史源分区改写进入显式重建流程 | [factor-update](../factor-update/SKILL.md) |

## 验收勾选

README 第 4 节固定使用八项清单，阶段对应关系如下。按实际通过证据勾选，阶段结束时随 README 提交同步。

| 完成阶段 | 第 4 节对应条目 |
|---|---|
| Review | 第 1、2、3 项：SQL 生产实现、独立 Python 验证实现、未来数据与时点可见性 |
| Validate | 第 4 项：独立复算与物化结果一致 |
| Benchmark | 第 5 项：全部声明列的单日增量与可用批次测试通过 |
| 完整 Calculate | 第 6 项：正式历史的联合唯一键检查通过；收尾复算重新验收第 4 项，失败取消第 6 项 |
| Evaluate | 第 7 项：全部声明列的评估与图表完整，并提交用户判断 |
| Publish | 第 8 项：overview 与全部 CSV/PNG 资产齐全，Release 已转正；发布仍由用户决定 |

## 返工路径

- IC 不稳定、分层不佳或多头收益不足：进入 Improve 形成构造改进方案，再返回设计。
- 公式、字段口径、参数或输出列变化：返回设计，更新 README 第 1–5 节后重新开发和审查。
- 实现偏离定义，或检查发现未来数据、独立性风险：返回开发并重新审查。
- 基准结果异常：复核日期区间、交易日批次和计算实现，再运行标准基准。
- 源水位、指纹或历史分区异常：交由 factor-update 诊断；定义变化按完整计算及其收尾验证、评估流程重建。

## 配套技能

- [factor-protocol](../factor-protocol/SKILL.md)：提供因子包接口、加载器、DuckDB/Hive 存储和产物路径。
- [factor-compose](../factor-compose/SKILL.md)：独立合成路径，在训练窗口和标签可见性约束下构建组合，由 Store 输出评分，使用统一评估入口的 `--factor-data` 输入，不进入标准因子包的 Calculate/Validate 流程。
