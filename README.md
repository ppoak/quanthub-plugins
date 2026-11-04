# QuantHub Plugins

面向 Coding Agent 的量化研究插件集，采用 Agent Plugins 格式，按需安装、独立使用。

| 插件 | 功能 |
| --- | --- |
| [Quant Tools](quant-tools/README.md) | 数据采集、维护、调度、备份与研究报告下载 |
| [Factor Research](factor-research/README.md) | 因子研究、计算、验证、评估、发布、更新与模型合成 |
| [Strategy Lab](strategy-lab/README.md) | 策略框架、本地 A 股回测、执行评估与东财模拟组合接入 |

## 安装

准备 Git 和支持插件安装的 Codex CLI。以下命令使用 PowerShell，先取得安装时最新的 `main` 完整提交号，再按该提交安装所需插件：

```powershell
$revision = (git ls-remote https://github.com/ppoak/quanthub-plugins.git refs/heads/main).Split()[0]
codex plugin marketplace add https://github.com/ppoak/quanthub-plugins.git --ref $revision
codex plugin add quant-tools@quanthub
codex plugin add factor-research@quanthub
codex plugin add strategy-lab@quanthub
```

新建会话后使用插件。其他支持 Agent Plugins 的客户端按各自方式安装。

插件安装不会自动安装 Python 依赖。按对应 README，在目标项目使用 Python 3.12+ 和 uv 安装同一 `$revision` 的 Python 包或锁定依赖；仅使用 Python 包无需安装 Agent。保存项目的 `pyproject.toml` 和 `uv.lock`，后续通过 `uv sync --locked` 复现环境。配置写入目标项目 `.env`。数据、账号、因子源码和运行结果由使用者管理；本仓库提供通用工具及示例。

## 目录与维护

三个插件目录分别包含 `plugin.json`、`skills/`、Logo、依赖文件和配置示例。`.agents/plugins/marketplace.json` 登记插件，marketplace 标识为 `quanthub`。

本地开发克隆本仓库，在对应插件中修改代码和文档。更新依赖时同步该插件的 `uv.lock`；验证脚本入口、接口示例及小样本流程后提交。插件可单独分发，引用路径均相对于插件目录。

更新时重新执行安装命令取得新的完整提交号，再按插件 README 更新 Python 依赖，保持插件与 Python 包来自同一提交。固定提交不会随远端更新自动变化。

本地开发可使用 editable 安装；各插件的 `pyproject.toml` 与 `uv.lock` 保留开发环境配置，用户安装以 README 为准。

## 许可证

采用 [MIT License](LICENSE)。数据源、第三方服务和依赖软件适用其各自的授权及使用条件。
