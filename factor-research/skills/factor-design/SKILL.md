---
name: factor-design
description: 设计并维护九节因子 README，记录因子定义、数据口径和研究结果。
---

# 因子设计

运行前按 [Factor Protocol 安装说明](../factor-protocol/SKILL.md#安装)检查项目已有环境和依赖。`<skill-root>` 指本 Skill 的安装目录，`<plugin-root>` 指插件根目录。

## 目标

项目根目录 `.env` 中的 `FACTORZOO` 指定因子源码根目录；因子设计规范保存在 `$FACTORZOO/<factor_name>/README.md`。README 先于代码更新；GitHub Issue 补充研究背景和历史决策，研究依据来自正式文献，后续开发、验证与交接按 README 当前定义执行。公式、输入字段、窗口、输出列和边界形成明确方案后进入实现。

因子包集中保存按同一流程计算、更新和发布的一组因子，以 `FACTOR_COLUMNS` 声明完整输出。

README 使用固定九节结构，记录可复核的设计、来源和结果摘要；第 9 节通过 Release 链接展示评估图表。

## 工作流程

1. 按[因子数据读取](../factor-protocol/references/data-access.md)查看项目数据目录、字段和有界样本，结合相关代码确认研究输入。正式文献支撑定义，GitHub Issue 补充背景。
2. 写好第 1、2、3、5 节正文，分别传入 `--motivation`、`--definition`、`--parameters`、`--references`。`--title` 使用英文标题，各词首字母大写，允许数字和下划线，以空格分隔，例如 `Turnover Distribution Uniformity`；包名自动生成 `turnover_distribution_uniformity`，须符合 `[A-Za-z_][A-Za-z0-9_]*`；`Barra CNE5 Beta` 生成 `barra_cne5_beta`。
3. 运行 [create_readme.py](scripts/create_readme.py)。新建时自动复制[模板](assets/factor_readme_template.md)，填入标题与设计正文、检查结构，并创建同名 prerelease；已有 README 重跑时保留验收状态与第 6–9 节，第 1–3 节变化取消旧验收勾选。Release tag 使用包名，名称与 README 标题一致，说明同步当前 README。
4. 用 `--reference-pdf` 上传参考 PDF，多个文件重复传入；无可提供的 PDF 时，经用户确认后用 `--no-reference-pdf <reason>` 将原因写入第 5 节。已有正式 Release 或 prerelease 重跑时，同步名称、正文与参考资料，保留原发布状态。
5. 提交推送生成的设计 README，运行 [sync_pr.py](../factor-publish/scripts/sync_pr.py) 的 `--factor-name <factor_name> --create` 创建与因子分支同名、指向 `main` 的 Draft PR。description 直接采用远端分支最新提交的因子 README 全文。
6. 核对 PR 为 Draft、标题与因子分支同名、目标为 `main`、description 已采用最新 README。经用户认可设计后进入开发，后续阶段推送 README 由 GitHub Actions 自动同步 PR 描述。

在项目根目录激活项目虚拟环境，参数与用法见脚本帮助；`--dry-run` 不修改 README 或远端：

~~~shell
uv run --locked python "<skill-root>/scripts/create_readme.py" --help
~~~

设计 README 提交推送后创建 Draft PR：

~~~shell
uv run --locked python "<plugin-root>/skills/factor-publish/scripts/sync_pr.py" --factor-name <factor_name> --create
~~~

## README 内容

- **第 1 节 因子动机**：研究问题、假设、经济机制和来源。
- **第 2 节 定义与数据**：公式、变量、字段、时间语义、频率、窗口、预热、边界、缺失/零/`inf`/常数规则和方向含义。
- **第 3 节 参数与输出**：对比参数、稳定列名、依赖关系、单位和特殊值处理。
- **第 4 节 验收标准**：保留模板验收清单，Review 维护第 1–3 项，各阶段脚本按 [工作流对应关系](../factor-workflow/SKILL.md#验收勾选)维护第 4–8 项；设计阶段不预先勾选。
- **第 5 节 参考文献**：每条文献使用单条 APA 链接，包含作者、年份、题名和出版物/机构，并指向可获取的正式来源。

README 定位为因子说明，重点是详细、明确、无歧义的定义；具体实现细节不放在定义板块。

## 自动回写

阶段脚本按 README 标记区域生成并更新结果：

| Skill | README 区域 | 内容 |
|---|---|---|
| factor-validate | 第 6 节 | 更新验证表；全部声明列通过才勾选第 4 项 |
| factor-benchmark、factor-update | 第 7 节 | 在统一性能块中更新批次及单日增量基准、正式增量运行记录 |
| factor-evaluate | 第 8 节 | 更新全部声明列与变体的评估摘要，生成 overview 并验收第 7 项 |
| factor-publish | 第 9 节 | 核验完整资产、转正 Release、更新图表链接并验收第 8 项 |

各脚本保留标记区域外的说明。测试状态依据实际结果记录。

## 设计约束

- 一般情况下，分钟滚动窗口不超过 20 个交易日，日频滚动窗口不超过 240 个交易日。
- 预热期根据对应数据表的交易日日历确定 `source_begin`。
- 同一概念的并列频率输出统一采用 1min。
- 输出列保持稳定、唯一，并使用 `(date, code)` 作为唯一键。
