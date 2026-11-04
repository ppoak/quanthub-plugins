# Factor Research

从项目已有数据出发，完成因子设计、开发、检验、评估和合成。

## 工具

| Skill | 用途 |
| --- | --- |
| [factor-workflow](skills/factor-workflow/SKILL.md) | 安排研究步骤 |
| [factor-protocol](skills/factor-protocol/SKILL.md) | 共用接口和数据读写 |
| [factor-design](skills/factor-design/SKILL.md) | 定义因子，编写研究说明 |
| [factor-develop](skills/factor-develop/SKILL.md) | 编写计算代码和独立复算代码 |
| [factor-review](skills/factor-review/SKILL.md) | 审查计算逻辑和数据使用时间 |
| [factor-validate](skills/factor-validate/SKILL.md) | 抽样复算，核对结果 |
| [factor-benchmark](skills/factor-benchmark/SKILL.md) | 比较计算速度和内存用量 |
| [factor-calculate](skills/factor-calculate/SKILL.md) | 计算历史因子数据 |
| [factor-evaluate](skills/factor-evaluate/SKILL.md) | 评估预测效果、组合收益和相关性 |
| [factor-improve](skills/factor-improve/SKILL.md) | 分析结果，提出改进方向 |
| [factor-compose](skills/factor-compose/SKILL.md) | 训练合成模型，保存评分 |
| [factor-publish](skills/factor-publish/SKILL.md) | 发布研究说明和评估结果 |
| [factor-update](skills/factor-update/SKILL.md) | 随数据更新因子 |

## 安装

准备 Git、Codex CLI、Python 3.12+ 和 uv。在[版本列表](https://github.com/ppoak/quanthub-plugins/tags)中选择版本，将所有 `<tag>` 替换为同一版本名称。以下命令可用于 Windows PowerShell、Linux 和 macOS：

```sh
codex plugin marketplace add https://github.com/ppoak/quanthub-plugins.git --ref "<tag>"
codex plugin add factor-research@quanthub
```

在自己的项目目录安装 Python 包。新项目先运行 `uv init --bare --python 3.12`：

```sh
uv add "factor-protocol @ git+https://github.com/ppoak/quanthub-plugins.git#subdirectory=factor-research/skills/factor-protocol" "factor-compose @ git+https://github.com/ppoak/quanthub-plugins.git#subdirectory=factor-research/skills/factor-compose" --tag "<tag>"
uv add "matplotlib>=3.9.0"
```

仅使用 Python 包可跳过插件安装；只需共用接口时，可仅安装 `factor-protocol`。合成模型所需的额外库按模型安装。发布到 GitHub 还需 GitHub CLI 和已授权账号。

保留项目的 `pyproject.toml` 和 `uv.lock`，在其他机器运行 `uv sync --locked`。升级时，使用新版本重新执行插件和 Python 包的安装命令。

## 配置

将 [.env.example](.env.example) 中需要的设置写入项目 `.env`：

```dotenv
DATA=data
ARTIFACTS=artifacts
FACTORZOO=factor_zoo
FACTORZOO_SCHEMA=schema.yaml
```

以上分别指定原始数据、运行结果、因子代码和因子清单的位置。相对路径从项目目录计算；同名设置优先使用命令行，其次是环境变量，最后是 `.env`。

研究前先查看实际数据和少量样本，确认字段、日期范围及数据何时可用。方法见[数据读取说明](skills/factor-protocol/references/data-access.md)。

## 使用

下文 `<plugin-root>` 是插件安装目录，`<project-root>` 是自己的项目目录，`<factor_name>` 是因子包名；均替换为实际值。各 Skill 中的 `<skill-root>` 是该 Skill 的安装目录。

在自己的项目目录运行。评估一个因子包：

```sh
uv run --locked python "<plugin-root>/skills/factor-evaluate/scripts/run_evaluate.py" --project-root "<project-root>" --factor-package "<factor_name>" --operation evaluate --operation check --operation plot --operation readme
```

评估合成评分：

```sh
uv run --locked python "<plugin-root>/skills/factor-evaluate/scripts/run_evaluate.py" --project-root "<project-root>" --factor-data artifacts/compositions/study/run_example/signals --factor-column score
```

查看因子更新计划或运行合成模型：

```sh
uv run --locked python "<plugin-root>/skills/factor-update/scripts/run_update.py" --project-root "<project-root>" --all --dry-run
uv run --locked python -m factor_compose --config config/workflow.yaml
```

因子研究依次完成设计、开发、审查、验证、性能检查、历史计算和评估；发布由用户决定。合成评分可直接评估。因子更新取决于已有数据；评估收益还需等待持有期结束后的行情。

## 常见问题

- Python 包无法导入：在项目目录运行 `uv sync --locked`。
- 缺少表或字段：检查实际数据，并核对因子说明。
- 没有新增结果：检查数据日期、计算所需历史长度和已有结果。
- 验证或评估失败：查看终端报错，并按对应 Skill 排查。
