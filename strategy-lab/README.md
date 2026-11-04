# Strategy Lab

提供策略公共模型、本地 A 股回测和东财模拟组合接入。

| Skill | 用途 |
| --- | --- |
| [base-strategy](skills/base-strategy/SKILL.md) | 账户、订单、成交、执行接口及基于成交的评估 |
| [ashare-backtest-strategy](skills/ashare-backtest-strategy/SKILL.md) | 下一 bar 撮合、T+1、整手、费用和滑点 |
| [eastmoney-strategy](skills/eastmoney-strategy/SKILL.md) | 东财模拟组合、订单与成交同步、账户镜像 |

## 安装与环境

插件采用 Agent Plugins 1.0 格式，可单独分发本目录。Codex CLI 安装示例：

```sh
codex plugin marketplace add https://github.com/ppoak/quanthub-plugins.git --ref main
codex plugin add strategy-lab@quanthub
```

`<plugin-root>` 为安装后的插件目录，`<project-root>` 为保存配置、策略和输出的项目目录；Skill 文档中的 `<skill-root>` 为对应 Skill 的安装目录。

使用 Python 3.12 或更新版本和 `uv`。在目标项目创建环境：

```sh
uv venv .venv
```

然后在插件目录安装依赖；`<project-root>` 使用绝对路径：

```sh
cd "<plugin-root>"
uv export --frozen --no-emit-project | uv pip install --python "<project-root>/.venv/Scripts/python.exe" -r -
```

导出的三个 Python 包以 editable 方式安装，相对安装路径以插件目录为基准。需要 Excel 导出时，在 `uv export` 后增加 `--group excel`。Linux／macOS 使用 `.venv/bin/python`。在 QuantHub 仓库开发时，在仓库根目录运行 `uv sync`。

## 配置与输入

从目标项目目录运行策略，按实际输入构建行情、信号和价格序列。A 股回测接收以证券代码为索引的行情 DataFrame；执行评估接收成交事实和价格矩阵。数据准备与策略循环见对应 Skill。

东财接入将 [.env.example](.env.example) 中需要的设置写入目标项目 `.env`，填写模拟组合 ID、认证值及连接参数。进程环境变量优先于 `.env`。在其他目录运行时，通过 `from_env(dotenv_path=...)` 明确指定配置文件；相对文件路径以进程工作目录为基准。

本地开发使用固定响应示例。`EMClient` 的 `live=False` 用于预览委托；连接真实模拟组合、查询状态或扫码登录仍会访问东财服务。`live=True` 会发送模拟盘委托或撤单，按用户明确授权的账户和交易意图执行。

## 示例

从目标项目目录运行本地策略循环：

```sh
.venv/Scripts/python "<plugin-root>/skills/ashare-backtest-strategy/examples/example_strategy_loop.py"
```

运行成交评估、本地撮合与东财固定响应检查：

```sh
.venv/Scripts/python -m unittest discover -s "<plugin-root>/skills/base-strategy/examples" -p "example_*.py"
.venv/Scripts/python -m unittest discover -s "<plugin-root>/skills/ashare-backtest-strategy/examples" -p "example_*.py"
.venv/Scripts/python -m unittest discover -s "<plugin-root>/skills/eastmoney-strategy/examples" -p "example_*.py"
.venv/Scripts/python "<plugin-root>/skills/eastmoney-strategy/examples/example_mock_portfolio_loop.py"
```

配置、二维码、报表及策略输出保存在目标项目。自定义导出路径以策略代码传入的位置为准。

## 问题处理与维护

- 包无法导入：检查当前 Python 和安装后的插件目录，重新安装依赖。
- 回测结果不符：核对信号时点、行情顺序、成交价格、T+1、费用和滑点。
- 东财认证或请求失败：检查项目配置、目标组合和终端错误，按东财 Skill 排查。
- 账户或成交不一致：先刷新平台状态，再核对订单、成交标识和账本记录。
- Excel 导出失败：安装 `excel` 依赖组，确认输出目录可写。

修改依赖后更新插件 `uv.lock`。插件版本记录在 `plugin.json` 与 `pyproject.toml`；发布前运行上述示例，核对撮合、账户、账本和评估结果。
