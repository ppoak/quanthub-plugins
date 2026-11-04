# Strategy Lab

编写和评估交易策略，运行本地 A 股回测，连接东财模拟组合。

| Skill | 用途 |
| --- | --- |
| [base-strategy](skills/base-strategy/SKILL.md) | 管理账户、订单和成交，评估交易结果 |
| [ashare-backtest-strategy](skills/ashare-backtest-strategy/SKILL.md) | 按 A 股交易规则回测，包括 T+1、整手、费用和滑点 |
| [eastmoney-strategy](skills/eastmoney-strategy/SKILL.md) | 查询和操作东财模拟组合，同步订单与成交 |

## 安装

准备 Git、Codex CLI、Python 3.12+ 和 uv。在[版本列表](https://github.com/ppoak/quanthub-plugins/tags)中选择版本，将所有 `<tag>` 替换为同一版本名称。以下命令可用于 Windows PowerShell、Linux 和 macOS：

```sh
codex plugin marketplace add https://github.com/ppoak/quanthub-plugins.git --ref "<tag>"
codex plugin add strategy-lab@quanthub
```

在自己的项目目录安装 Python 包。新项目先运行 `uv init --bare --python 3.12`：

```sh
uv add "base-strategy @ git+https://github.com/ppoak/quanthub-plugins.git#subdirectory=strategy-lab/skills/base-strategy" "ashare-backtest-strategy @ git+https://github.com/ppoak/quanthub-plugins.git#subdirectory=strategy-lab/skills/ashare-backtest-strategy" "eastmoney-strategy @ git+https://github.com/ppoak/quanthub-plugins.git#subdirectory=strategy-lab/skills/eastmoney-strategy" --tag "<tag>"
```

仅使用 Python 包可跳过插件安装。两个策略包可按需选择，同时安装同一版本的 `base-strategy`。需要导出 Excel 时运行 `uv add "openpyxl>=3.1.0"`。

保留项目的 `pyproject.toml` 和 `uv.lock`，在其他机器运行 `uv sync --locked`。升级时，使用新版本重新执行插件和 Python 包的安装命令。

## 配置与使用

本地回测需要行情、信号和价格数据。输入格式和策略示例见对应 Skill。

东财模拟组合需要将 [.env.example](.env.example) 中的组合 ID、认证信息等写入项目 `.env`。环境变量中的同名设置优先；在其他目录运行时，可用 `from_env(dotenv_path=...)` 指定配置文件。

下文 `<plugin-root>` 是插件安装目录，替换为实际路径。在自己的项目目录运行本地回测示例：

```sh
uv run --locked python "<plugin-root>/skills/ashare-backtest-strategy/examples/example_strategy_loop.py"
```

运行无需连接东财的本地示例：

```sh
uv run --locked python "<plugin-root>/skills/eastmoney-strategy/examples/example_mock_portfolio_loop.py"
```

更多示例和检查方法见各 Skill 的 `examples/`。报表、二维码等文件按程序指定的位置保存；相对路径从运行目录计算。

## 常见问题

- Python 包无法导入：在项目目录运行 `uv sync --locked`。
- 回测结果不符：检查信号时间、行情顺序、成交价格、T+1、费用和滑点。
- 东财认证失败：核对配置、目标组合和报错信息。
- 账户或成交不一致：刷新平台数据，再核对订单、成交和本地记录。
