# QuantHub Plugins

用于量化研究的三个插件，可按需安装，也可单独使用其中的 Python 包。

| 插件 | 用途 | 使用入口 |
| --- | --- | --- |
| Quant Tools | 采集、更新和备份数据，下载研究报告 | [数据维护](plugins/quant-tools/skills/database-maintenance/SKILL.md) · [报告下载](plugins/quant-tools/skills/report-download/SKILL.md) |
| Factor Research | 开发、检验、评估和合成因子 | [因子研究](plugins/factor-research/skills/factor-workflow/SKILL.md) · [因子合成](plugins/factor-research/skills/factor-compose/SKILL.md) |
| Strategy Lab | 编写策略、回测 A 股，连接东财模拟组合 | [策略基础](strategy-lab/skills/base-strategy/SKILL.md) · [A 股回测](strategy-lab/skills/ashare-backtest-strategy/SKILL.md) · [东财模拟组合](strategy-lab/skills/eastmoney-strategy/SKILL.md) |

## 安装插件

准备 Git 和 Codex CLI，在[发布列表](https://github.com/ppoak/quanthub-plugins/releases)中选择插件版本。标签统一使用 `对象名@版本号`，例如 `factor-research@0.1.0`，不加 `v`。示例名称不代表该版本已经发布。

`<plugin-name>` 可填 `quant-tools`、`factor-research` 或 `strategy-lab`，`<plugin-version>` 填它的发布版本。以下命令可用于 Windows PowerShell、Linux 和 macOS：

```sh
codex plugin marketplace add https://github.com/ppoak/quanthub-plugins.git --ref "<plugin-name>@<plugin-version>"
codex plugin add "<plugin-name>@quanthub"
```

安装其他插件时，分别选择版本并重复这两步；每次只安装所选插件。这里 `@quanthub` 表示插件来源，不是版本号。安装后新建会话使用。其他支持 Agent Plugins 的工具使用各自的安装入口。

## 使用与更新

按上表进入所需 Skill。安装说明、配置示例和运行方法均在 Skill 内，优先使用项目已有的 Python 环境，缺少依赖时再安装。仅使用 Python 包时，无需安装 Agent 或插件。

把所需配置补充到自己项目的 `.env`，保留已有设置。数据、研究代码和运行结果也保存在自己的项目中。

插件与 Python 包分别维护版本。Skill 文档或脚本变化时更新所属插件；Python 包内容变化时，更新该包和所属插件。未变化的包继续使用原版本。

Python 包从各自 Release 的 wheel 附件安装，例如 `factor-protocol@0.1.0`。各 Skill 提供安装命令，包版本按对应发布说明选择，不必与插件版本相同。安装前确认所选版本已有对应标签和 wheel 附件。

修改代码或依赖时，在对应 Skill 中维护和检查。发布时为合并后的版本创建新标签，已有标签保持不变。

## 许可证

采用 [MIT License](LICENSE)。数据源和第三方服务遵循各自的使用条件。
