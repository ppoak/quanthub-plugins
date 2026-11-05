# QuantHub

> You are a specialist in quant research.

## Project Introduction

这是一个致力于量化多因子挖掘、回测、检验、开发到生产、实盘的量化投资实验室。

## Environment

项目使用 `uv` 管理 Python 环境和依赖。可以通过 `uv sync` 同步环境，可以通过 `uv add` 添加必要的依赖包。虚拟环境在 `.venv`目录下。

插件在公开仓库 `https://github.com/ppoak/quanthub-plugins` 独立维护。本仓库不包含插件源码或子模块；使用安装后 Skill 的实际目录运行工具。研究运行所需的公共 Python 包通过固定 Git 提交安装。

- 运行任何 Python 脚本、模块或命令时，必须使用项目虚拟环境中的 Python：`.venv/Scripts/python`
- Windows 路径必须使用 `/` 正斜杠，避免反斜杠转义问题。
- 如果需要添加依赖，直接使用：`uv add <package>`
- 如果需要运行 Python 代码，使用：`.venv/Scripts/python -c "<python code>"`
- 如果需要运行 Python 脚本，使用：`.venv/Scripts/python path/to/script.py`

## Data Source

项目 `.env` 的 `DATA` 指定上游数据目录，使用 DuckDB 和 Parquet；数据可按 `date=YYYY-MM-DD` 做 Hive 分区，也可保存为非分区文件。开始研究时查看实际目录、字段和有界样本，按研究目标选择数据、关联方式和日期范围。方法见 [Factor Protocol 数据读取](https://github.com/ppoak/quanthub-plugins/blob/main/factor-research/skills/factor-protocol/references/data-access.md)，源码与产物目录见[项目路径配置](https://github.com/ppoak/quanthub-plugins/blob/main/factor-research/skills/factor-protocol/SKILL.md#项目路径配置)。

## Rules

### Core

Less is more，大道至简，在编码、写报告过程中请谨记，严格遵守如下规则：

1. 永远用最简单、高效的方式实现需求；
2. 永远不要自行创造没有要求的功能；
3. 永远不要为过于简单的功能做 abstraction；
4. 永远不要为不可能发生的场景写防御；
5. 永远保持语言的简洁高效，惜字如金，非必要不展开详细论述；

### Language

- Coilysis 的测试脚本和测试产物统一放在临时目录，测试完成后删除，不在课题内保留测试目录或测试代码。

- 与用户交流时使用中文。
- 构建脚本、编写代码、运行分析、生成代码注释、生成命令行输出说明时，全部使用英文。
- 代码、变量名、函数名、文件名、日志、异常信息、CLI 参数和 Markdown 技术文档中的代码相关内容，全部使用英文。
- 避免在代码文件、脚本输出和命令行内容中使用中文，以免出现 Windows 编码问题。
- Markdown 报告文档、技术文档、说明文档中统一使用中文，涉及到 GitHub 上的 Issue / Pull Request / Comment 等内容，统一使用中文。

### Git

#### Branch

- `main` 分支只保留人工确认后的稳定内容。
- Agent 永远不要直接合并到 `main`。
- 因子开发子分支 `<factor_name>` 上，Agent 可以不经用户同意直接创建分支、commit 和 push。
- 因子 Design 阶段完成设计 README 后，Agent 应主动 commit、push，并创建与因子分支同名、指向 `main` 的 Draft PR；description 直接采用分支最新提交的因子 README，后续阶段持续同步。
- 非因子开发 PR 的创建、所有 PR 的关闭，以及任何非 Draft PR 状态切换前，必须询问用户确认。
- 所有合并到 `main` 分支的操作，均由用户人工执行。

所有因子开发的分支均使用如因子名，例如 `market_cap`。不要使用无意义分支名，例如 `test`、`dev2`、`new-idea`、`tmp`。

#### Commit Message

Git commit message 统一使用如下格式：`<type>: <summary>`

`type` 仅使用以下类型：

- `feat`: 新增因子、脚本、模块或功能
- `exp`: 新实验、新假设、新参数组合
- `fix`: 修复 bug、数据泄露、未来函数、计算错误
- `refactor`: 重构代码，不改变整体逻辑
- `docs`: 文档、实验记录、结论说明
- `data`: 数据口径、样本区间、数据加载方式变化
- `test`: smoke test、单元测试、验证脚本
- `wip`: 临时保存，尚未整理完成

重要研究提交应在 commit body 中记录：

```text
Why:
- Factor motivation or hypothesis.

What:
- Key code, data, config, or documentation changes.

Result:
- Current conclusion.
```

不要使用无意义提交信息，`wip` 只用于临时保存，合并前应整理为有明确研究含义的 commit。

#### Commit Content

关于什么文件可以加入git管理，什么需要ignore，什么坚决不能进入git有以下标准：

1. 脚本、报告必须加入git管理；
2. 所有 **二进制** 文件一律不可加入 git 管理，不再设置图片例外；
3. `data/`、`artifacts/`、`output/`、`.venv/`、`factor_zoo/*/dist/`、CSV 输出、图片和临时二进制产物不要加入 git；
4. 因子精选图片只能作为 GitHub release asset 发布，不能提交到代码仓库。
5. 因子对应的研报或论文 PDF 在 Design 阶段上传同名 prerelease，并以稳定 ASCII 文件名（如 `<factor_name>_reference_01.pdf`）上传；Publish 验收完整评估资产后转为正式 Release。

#### Pull Request

1. 在github上发起的Pull Request，命名需要和分支名称一致。
2. 因子 Draft PR 在 Design 阶段提交推送设计 README 后，通过 factor-publish 的 `scripts/sync_pr.py --factor-name <factor_name> --create` 创建。description 直接使用 PR 分支最新提交的因子 README，后续各阶段推送由 GitHub Actions 自动同步；不另写提交摘要。
3. 不要在 PR 描述中罗列验证命令或脚本位置。PR 审核期间，由用户指定目标因子包，发布包内全部声明列及完整评估资产。因子包是最小发布和更新单位。图片挂在 tag 与开发因子分支一致的 `<factor_name>` Release，并在 PR 描述 中嵌入对应链接。
4. Publish 核验全部评估资产后移除 prerelease 标记，再由用户人工合并 PR 到 `main`。
5. PR 远程合并后，本地需同步远程，应将本地、远程的因子开发子分支删除，并核对 Design 阶段已上传的参考 PDF。
6. 因子包名由 README 英文标题转为全小写、空格改下划线生成；Release tag 使用包名，名称与 README 标题一致，说明直接采用 README 全文；Design 创建 prerelease 时同步设计版，Publish 转正时同步完成版，均由脚本执行。
7. 用户确认暂无可提供的研报或论文 PDF 时，由 Design 脚本在 README 第 5 节记录原因，随正文同步到 Release。

## Skill 工作流路由

全部 `factor-*` Skill 由 [Factor Research 插件](https://github.com/ppoak/quanthub-plugins/blob/main/factor-research/README.md) 提供，使用安装后的 Skill 入口。

`base-strategy`、`ashare-backtest-strategy` 与 `eastmoney-strategy` 由 [Strategy Lab 插件](https://github.com/ppoak/quanthub-plugins/blob/main/strategy-lab/README.md) 提供，使用安装后的 Skill 入口。

`database-maintenance` 与 `report-download` 由 [Quant Tools 插件](https://github.com/ppoak/quanthub-plugins/blob/main/quant-tools/README.md) 提供，使用安装后的 Skill 入口。使用安装后 Skill 的实际目录，通过目标项目虚拟环境运行脚本；`--project-root` 指定配置和数据所属项目。

- 因子全流程：`factor-workflow` 编排 `factor-design` → `factor-develop` → `factor-review` → 隔离计算/`factor-validate` → `factor-benchmark` → 完整 `factor-calculate` → `factor-validate` → `factor-evaluate`；用户同意发布后进入 `factor-publish`，需要改善效果时进入 `factor-improve` → `factor-design`。
- 生产因子增量刷新进入 factor-update，按所用数据的实际分区和日历确定新增区间；完整历史计算进入 factor-calculate。项目调度器按需调用更新脚本。源数据采集、表分区修复和备份进入 database-maintenance。
- 因子公共协议、FactorStorage、加载器、注册表和产物接口由 `factor-protocol` 所有。阶段引擎单向依赖它，不在协议包中实现评估、验证或更新流程。
- 因子合成进入 `factor-compose`，使用 `run_evaluate.py --factor-data <signals_directory> --factor-column score` 直接评估保存的评分，不要求因子名称、因子包或 Factor Validate。标准因子包使用互斥的 `--factor-package` 输入并核验已有 Validate 结果；计算、检查、绘图和 README 更新由 `core/evaluation.py` 按操作参数编排。策略公共模型、四层契约和成交评估进入 `base-strategy`；具体后端的开发与运行进入对应后端 skill，包括 `ashare-backtest-strategy` 和 `eastmoney-strategy`。这些后端不依赖因子代码。
- 雪球策略不在当前范围；雪球数据采集仍属于 `database-maintenance`。

## Skill 地图

| Skill | 职责 |
|---|---|
| `factor-protocol` | 因子稳定接口、DuckDB/Parquet 存储、包加载、active 注册表、指纹和公共产物 |
| `factor-design` / `factor-develop` / `factor-review` | 因子设计规范、SQL 生产代码与独立验证实现、定义及 PIT 审查 |
| `factor-calculate` / `factor-validate` / `factor-benchmark` / `factor-evaluate` | 全量物化、独立复算、资源配置选择和 SQL 评估 |
| `factor-improve` | 检视评估效果，提出因子构造改进并交回设计 |
| `factor-update` / `factor-publish` / `factor-workflow` | 增量更新、发布资产及生命周期编排 |
| `factor-compose` | 数据准备、模型合成、工作流与评分保存；评估交由 factor-evaluate |
| `base-strategy` / `ashare-backtest-strategy` / `eastmoney-strategy` | 通用策略契约、本地 A 股撮合、东财远端账户镜像 |
| `database-maintenance` | RiceQuant、雪球、EastMoney、Hugging Face 数据任务及调度 |
| `report-download` | 报告下载 |

需要复用或被其他包依赖的 Python 能力通过 skill 自身 `pyproject.toml` 和 `src/<package>/` 提供可编辑安装。Calculate、Validate、Evaluate、Benchmark 等终端流程 skill 将执行脚本放在各自 `scripts/` 下，不发布 Python 包。根 `pyproject.toml` 管理研究环境，并固定所需公共 Python 包的 Git 版本；不发布或导入 `quanthub` 总包。

测试与案例约定：Factor Protocol 的可执行契约案例统一放在 `examples/`，测试本身也是接口示例，不创建 `tests/`。其他可安装 Python 包可按需保留 `tests/`；非包的流程或脚本 skill 不创建 `tests/`，通过运行其脚本入口及 `--help` 做启动检查。
