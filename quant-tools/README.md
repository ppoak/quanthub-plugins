# Quant Tools

采集和维护研究数据，下载研究报告。

| Skill | 用途 |
| --- | --- |
| [database-maintenance](skills/database-maintenance/SKILL.md) | 采集、更新、备份数据，定时运行任务 |
| [report-download](skills/report-download/SKILL.md) | 下载研究报告并检查 PDF |

## 安装

准备 Git、Codex CLI、Python 3.12+ 和 uv。在[版本列表](https://github.com/ppoak/quanthub-plugins/tags)中选择版本，将所有 `<tag>` 替换为同一版本名称。以下命令可用于 Windows PowerShell、Linux 和 macOS：

```sh
codex plugin marketplace add https://github.com/ppoak/quanthub-plugins.git --ref "<tag>"
codex plugin add quant-tools@quanthub
```

在自己的项目目录准备 Python 环境。新项目先运行 `uv init --bare --python 3.12`，再运行 `uv sync`。仅下载报告时，到此即可。

数据工具还需安装 Python 库。下载同一版本的代码，生成所需库的清单；将 `<source-root>` 替换为独立的绝对目录路径：

```sh
git clone https://github.com/ppoak/quanthub-plugins.git "<source-root>"
git -C "<source-root>" checkout --detach "refs/tags/<tag>"
uv export --project "<source-root>/quant-tools" --frozen --no-emit-project --no-hashes --output-file "<source-root>/quant-tools/requirements.txt"
```

已有下载目录时，跳过 clone，先运行 `git -C "<source-root>" fetch origin --tags`。然后在自己的项目目录安装：

```sh
uv add -r "<source-root>/quant-tools/requirements.txt"
```

使用 RiceQuant 时，在 `uv export` 命令中增加 `--group ricequant`；使用 Hugging Face 备份时增加 `--group backup`，也可同时指定。

保留项目的 `pyproject.toml` 和 `uv.lock`，在其他机器运行 `uv sync --locked`。升级时选择新版本，重新安装插件、生成清单并安装 Python 库。

## 配置与使用

将 [.env.example](.env.example) 中需要的设置写入项目 `.env`，填写数据目录、账号和任务设置。相对路径从项目目录计算；同名设置优先使用命令行，其次是环境变量，最后是 `.env`。

下文 `<plugin-root>` 是插件安装目录，`<project-root>` 是自己的项目目录，均替换为实际值。在自己的项目目录查看任务：

```sh
uv run --locked python "<plugin-root>/skills/database-maintenance/scripts/scheduler.py" --project-root "<project-root>" --list-jobs
```

下载报告：

```sh
uv run --locked python "<plugin-root>/skills/report-download/scripts/bigquant_download.py" "<report-url>" --project-root "<project-root>"
```

数据、报告和日志保存在自己的项目中。定时运行可使用项目已有的任务计划，调用相同脚本。

## 使用前注意

- RiceQuant 采集需要 Chrome、账号和服务器 notebook。
- 发现报告的下载授权需要 OpenSSL 或 Node.js，也可直接提供授权下载地址。
- 首次采集、备份或发送通知前，按对应 Skill 检查配置并先做小范围试运行。
