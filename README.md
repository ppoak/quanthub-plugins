# QuantHub Plugins

用于量化研究的三个插件，可按需安装，也可单独使用其中的 Python 包。

| 插件 | 用途 | 使用入口 |
| --- | --- | --- |
| Quant Tools | 采集、更新和备份数据，下载研究报告 | [数据维护](quant-tools/skills/database-maintenance/SKILL.md) · [报告下载](quant-tools/skills/report-download/SKILL.md) |
| Factor Research | 开发、检验、评估和合成因子 | [因子研究](factor-research/skills/factor-workflow/SKILL.md) · [因子合成](factor-research/skills/factor-compose/SKILL.md) |
| Strategy Lab | 编写策略、回测 A 股，连接东财模拟组合 | [策略基础](strategy-lab/skills/base-strategy/SKILL.md) · [A 股回测](strategy-lab/skills/ashare-backtest-strategy/SKILL.md) · [东财模拟组合](strategy-lab/skills/eastmoney-strategy/SKILL.md) |

## 安装插件

准备 Git 和 Codex CLI，在[版本列表](https://github.com/ppoak/quanthub-plugins/tags)中选择版本，将 `<tag>` 替换为实际版本名称。以下命令可用于 Windows PowerShell、Linux 和 macOS：

```sh
codex plugin marketplace add https://github.com/ppoak/quanthub-plugins.git --ref "<tag>"
codex plugin add quant-tools@quanthub
codex plugin add factor-research@quanthub
codex plugin add strategy-lab@quanthub
```

只需执行所需插件的安装命令，随后新建会话使用。其他支持 Agent Plugins 的工具使用各自的安装入口。

## 使用与更新

按上表进入所需 Skill。安装说明、配置示例和运行方法均在 Skill 内，优先使用项目已有的 Python 环境，缺少依赖时再安装。仅使用 Python 包时，无需安装 Agent 或插件。

把所需配置补充到自己项目的 `.env`，保留已有设置。数据、研究代码和运行结果也保存在自己的项目中。

升级时选择新版本，同时更新插件和所用 Skill 的 Python 依赖。插件与 Python 包使用同一版本标签。

修改代码或依赖时，在对应 Skill 中维护和检查。发布时为合并后的版本创建新标签，已有标签保持不变。

## 许可证

采用 [MIT License](LICENSE)。数据源和第三方服务遵循各自的使用条件。
