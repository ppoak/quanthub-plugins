# Quant Tools

提供两个量化研究工具：

| Skill | 用途 |
| --- | --- |
| [database-maintenance](skills/database-maintenance/SKILL.md) | 行情、财务、行业和社交数据的采集、分区更新、调度与备份 |
| [report-download](skills/report-download/SKILL.md) | 按来源下载研究报告并校验 PDF |

## 安装

插件采用 [Agent Plugins 1.0](https://agent-plugins.org/) 格式，入口为根目录 `plugin.json`，两个 Skill 位于 `skills/`。插件包含全部脚本和参考文档，可单独分发 `quant-tools/` 目录。

在 ChatGPT／Codex 的仓库插件目录中选择 QuantHub 下的 `quant-tools`。仓库登记文件为 `.agents/plugins/marketplace.json`；单独分发本插件时，在所用客户端登记插件目录。其他实现该标准的客户端使用各自的插件安装入口。

以下命令使用 PowerShell，先取得最新 `main` 的完整提交号，再安装插件：

```powershell
$revision = (git ls-remote https://github.com/ppoak/quanthub-plugins.git refs/heads/main).Split()[0]
codex plugin marketplace add https://github.com/ppoak/quanthub-plugins.git --ref $revision
codex plugin add quant-tools@quanthub
```

安装插件后，使用目标项目的 Python 环境执行脚本。下文 `<plugin-root>` 为安装后的插件目录，`<project-root>` 为保存配置和数据的项目目录，两者可位于不同位置。

## Python 环境

使用 Python 3.12 或更新版本和 `uv`。仅下载报告无需额外 Python 依赖，在目标项目运行 `uv venv .venv --python 3.12` 即可。

数据库工具没有独立 Python 包，按安装插件时的 `$revision` 获取源码和锁文件。`<source-root>` 使用独立的绝对路径；已有克隆时先 `git -C "<source-root>" fetch origin`，再切换提交：

```powershell
git clone https://github.com/ppoak/quanthub-plugins.git "<source-root>"
git -C "<source-root>" checkout --detach $revision
uv export --project "<source-root>/quant-tools" --frozen --no-emit-project --no-hashes --output-file "<source-root>/quant-tools/requirements.txt"
```

在目标项目目录导入这些固定版本依赖；新项目先运行 `uv init --bare --python 3.12`：

```powershell
uv add -r "<source-root>/quant-tools/requirements.txt"
```

RiceQuant 与 Hugging Face 备份分别在 `uv export` 中增加 `--group ricequant`、`--group backup`，也可同时指定。提交目标项目的 `pyproject.toml` 和 `uv.lock`，其他环境运行 `uv sync --locked`。更新时重新取得提交号，同时更新插件、导出并导入依赖。

下文脚本使用项目 `.venv`；Linux／macOS 将 `.venv/Scripts/python` 替换为 `.venv/bin/python`。

RiceQuant 本地采集还需 Chrome、账号和服务器 notebook；发现报告签名需要 OpenSSL 或 Node.js，也可直接提供授权下载地址。来源要求见对应 Skill。

## 项目配置与运行

将 [.env.example](.env.example) 中需要的设置写入目标项目 `.env`，填写数据目录、账号和任务设置。命令行参数优先于进程环境变量，进程环境变量优先于项目 `.env`；相对路径以目标项目为基准。

查看调度任务：

```sh
.venv/Scripts/python "<plugin-root>/skills/database-maintenance/scripts/scheduler.py" --project-root "<project-root>" --list-jobs
```

下载报告：

```sh
.venv/Scripts/python "<plugin-root>/skills/report-download/scripts/bigquant_download.py" "<report-url>" --project-root "<project-root>"
```

数据、报告、日志、下载缓存和就绪标记保存在目标项目。长期调度使用同一脚本入口及 `--project-root`，由项目原有的服务或任务计划启动。

## 维护

修改对应 Skill 内的脚本和说明；更新依赖后，在插件目录运行 `uv lock` 并提交 `uv.lock`。插件版本记录在 `plugin.json` 与 `pyproject.toml` 中。

发布前检查两个 Skill 的脚本 `--help`、调度清单、dry-run，以及临时目录中的小范围数据写入和报告下载。真实采集、远端备份和通知需按所用来源另行验证。
