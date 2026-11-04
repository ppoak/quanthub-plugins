---
name: base-strategy
description: 提供策略框架的核心模型、四层契约、成交事实存储和执行评估。
---

# Base Strategy

运行前按[插件说明](../../README.md)准备目标项目环境。`<skill-root>` 为本 Skill 的安装目录。

本 skill 说明策略运行所需的基础模型、Account／Client／Exchange／Ledger 四层契约、订单与成交事实存储，以及基于成交记录的执行评估。

Python 包随插件依赖安装；Excel 导出使用插件的 `excel` 依赖组。

## 核心契约

- `Order` 表示策略下达的交易意图，`Execution` 表示已确认的成交事实，`Position` 表示当前持仓状态。
- `BaseClient` 提供买入、卖出和撤单接口；`BaseExchange` 公开尚未进入终态的订单队列。
- `BaseAccount` 表达账户初始资金、当前现金和持仓；`BaseLedger` 记录终态订单与已确认成交。
- `Store` 去重并保存订单、成交事实，可导出 Excel。

## 核心运行流程

```text
Strategy -> Client -> Exchange -> Account
                         |
                         +-> Ledger -> Store -> Evaluator
```

策略向 `Client` 提交交易意图；`Exchange` 管理订单生命周期并产生已确认的成交；成交同步到账户状态并记录到 `Ledger`。`Store` 汇集可回放的订单与成交事实，`evaluate_executions` 结合价格序列和初始资金重建持仓与净值。组件关系和事实流见[框架运行模型](references/framework.md)。

## 示例

[订单、成交与评估示例](examples/example_store_evaluation.py) 展示如何记录基础模型、去重成交事实，并从成交序列重建净值：

```powershell
.venv/Scripts/python -m unittest discover -s "<skill-root>/examples" -p "example_*.py"
```
