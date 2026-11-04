# Factor Research

提供因子研究、计算、验证、评估、合成、发布和增量更新。读取目标项目中已有的数据。

## Skills

| Skill | 用途 |
| --- | --- |
| [factor-workflow](skills/factor-workflow/SKILL.md) | 研究流程与阶段交接 |
| [factor-protocol](skills/factor-protocol/SKILL.md) | 公共 Python 接口、数据读取和产物路径 |
| [factor-design](skills/factor-design/SKILL.md) | 因子定义与研究 README |
| [factor-develop](skills/factor-develop/SKILL.md) | SQL 计算与独立 Python 复算 |
| [factor-review](skills/factor-review/SKILL.md) | 实现、时间可见性和计算独立性审查 |
| [factor-validate](skills/factor-validate/SKILL.md) | 抽样独立验证 |
| [factor-benchmark](skills/factor-benchmark/SKILL.md) | 计算批次与资源比较 |
| [factor-calculate](skills/factor-calculate/SKILL.md) | 完整历史计算 |
| [factor-evaluate](skills/factor-evaluate/SKILL.md) | IC、组合收益与相关性 |
| [factor-improve](skills/factor-improve/SKILL.md) | 评估分析与构造改进 |
| [factor-compose](skills/factor-compose/SKILL.md) | 数据准备、模型训练和评分保存 |
| [factor-publish](skills/factor-publish/SKILL.md) | 评估资产发布和研究记录同步 |
| [factor-update](skills/factor-update/SKILL.md) | 按现有数据增量更新 active 因子 |

## 安装与环境

插件采用 Agent Plugins 1.0 格式，可单独分发本目录。以下命令使用 PowerShell，先取得最新 `main` 的完整提交号，再安装插件：

```powershell
$revision = (git ls-remote https://github.com/ppoak/quanthub-plugins.git refs/heads/main).Split()[0]
codex plugin marketplace add https://github.com/ppoak/quanthub-plugins.git --ref $revision
codex plugin add factor-research@quanthub
```

下文 `<plugin-root>` 为安装后的插件目录，`<project-root>` 为保存配置、数据、因子源码和产物的项目目录。Skill 文档中的 `<skill-root>` 指该 Skill 的实际安装目录。

使用 Python 3.12 或更新版本和 `uv`。在目标项目目录执行以下命令；新项目先运行 `uv init --bare --python 3.12`：

```powershell
uv add "factor-protocol @ git+https://github.com/ppoak/quanthub-plugins.git@${revision}#subdirectory=factor-research/skills/factor-protocol" "factor-compose @ git+https://github.com/ppoak/quanthub-plugins.git@${revision}#subdirectory=factor-research/skills/factor-compose" "matplotlib>=3.9.0"
```

`$revision` 沿用上方安装插件时取得的完整提交号。两个 Python 包从该提交构建并安装到项目 `.venv`，不使用插件缓存的 editable 安装。只需公共接口时，可仅安装 `factor-protocol`；无需安装 Agent 或插件，只需先运行上方取得 `$revision` 的命令。

提交目标项目的 `pyproject.toml` 和 `uv.lock`，其他环境运行 `uv sync --locked`。第三方依赖以目标项目的锁文件为准。更新时重新取得提交号，同时更新插件和 Python 包。

合成模型需要的额外库按模型实现安装。发布和同步 GitHub 记录还需 Git、GitHub CLI 及已授权账号。

## 项目配置与数据

将 [.env.example](.env.example) 中需要的设置写入目标项目 `.env`：

```dotenv
DATA=data
ARTIFACTS=artifacts
FACTORZOO=factor_zoo
FACTORZOO_SCHEMA=schema.yaml
```

相对路径以目标项目为基准。命令行路径参数优先于进程环境变量，进程环境变量优先于 `.env`。数据、因子源码和研究产物均保存在目标项目。

开始研究时，从 `DATA` 查看实际表目录、分区、字段和有界样本，再选择数据与计算区间。读取方式见[数据访问说明](skills/factor-protocol/references/data-access.md)。具体因子的 README 记录所用字段、单位、关联方式和可见时间；字段含义不明确时先确认依据。

## 运行

从目标项目目录运行脚本，并显式传入 `--project-root`：

```sh
.venv/Scripts/python "<plugin-root>/skills/factor-evaluate/scripts/run_evaluate.py" --project-root "<project-root>" --factor-package <factor_name> --operation evaluate --operation check --operation plot --operation readme
.venv/Scripts/python "<plugin-root>/skills/factor-evaluate/scripts/run_evaluate.py" --project-root "<project-root>" --factor-data artifacts/compositions/study/run_example/signals --factor-column score
.venv/Scripts/python "<plugin-root>/skills/factor-update/scripts/run_update.py" --project-root "<project-root>" --all --dry-run
.venv/Scripts/python -m factor_compose --config config/workflow.yaml
```

标准因子包按 Design → Develop → Review → Validate → Benchmark → Calculate → Evaluate 执行；发布由用户决定。合成评分直接交给 Evaluate，使用 `--factor-data` 指定评分目录。

Update 以因子实际使用的数据分区和日历确定新增日期，由项目调度器按需调用。评估还需要持有期结束后的行情，其可评估尾日可能早于因子尾日。

## 问题处理与维护

- 无法导入公共包：检查当前 Python 环境和项目中记录的 Git 提交，运行 `uv sync --locked`。
- 表或字段不存在：查看实际目录、字段及样本，按因子定义调整实现。
- 没有新增输出：检查因子使用的数据范围、预热期和现有输出日期。
- 验证、评估或历史一致性检查失败：按对应 Skill 查看终端和产物中的失败记录。

修改依赖后更新插件 `uv.lock`；插件版本记录在 `plugin.json` 和 `pyproject.toml`。发布前检查脚本启动、公共接口案例，以及独立项目中的小样本计算、验证、评估和更新。
