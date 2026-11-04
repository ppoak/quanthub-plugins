# QuantHub Plugins

用于量化研究的三个插件，可按需安装，也可单独使用其中的 Python 包。

| 插件 | 用途 |
| --- | --- |
| [Quant Tools](quant-tools/README.md) | 采集、更新和备份数据，下载研究报告 |
| [Factor Research](factor-research/README.md) | 开发、检验、评估和合成因子 |
| [Strategy Lab](strategy-lab/README.md) | 编写策略、回测 A 股，连接东财模拟组合 |

## 安装

准备 Git 和 Codex CLI，在[版本列表](https://github.com/ppoak/quanthub-plugins/tags)中选择版本，将 `<tag>` 替换为对应名称，例如 `v0.1.0`。示例名称不代表该版本已发布。以下命令可用于 Windows PowerShell、Linux 和 macOS：

```sh
codex plugin marketplace add https://github.com/ppoak/quanthub-plugins.git --ref "<tag>"
codex plugin add quant-tools@quanthub
codex plugin add factor-research@quanthub
codex plugin add strategy-lab@quanthub
```

只需执行所需插件的安装命令，随后新建会话使用。其他支持 Agent Plugins 的工具使用各自的安装入口。

安装插件后，还需按对应 README 准备 Python 环境，插件与 Python 包选择同一版本。仅使用 Python 包时，无需安装 Agent 或插件。

## 使用与更新

每个插件目录包含使用说明、`skills/` 工具及配置示例。把所需配置写入自己项目的 `.env`；数据、研究代码和运行结果也保存在自己的项目中。

保留项目的 `pyproject.toml` 和 `uv.lock`，在其他机器运行 `uv sync --locked` 即可安装相同版本。升级时，将安装命令中的 `<tag>` 换成新版本，同时更新插件和 Python 包。

修改插件时，在对应目录开发和检查；修改 Python 依赖后运行 `uv lock`。发布时为合并后的版本创建新标签，已有标签保持不变。

## 许可证

采用 [MIT License](LICENSE)。数据源和第三方服务遵循各自的使用条件。
