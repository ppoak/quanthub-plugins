---
name: factor-publish
description: 使用脚本发布因子完整评估资产，诊断并修复发布失败。
---

# Factor Publish

运行前按 [Factor Protocol 安装说明](../factor-protocol/SKILL.md#安装)检查项目已有环境和依赖。`<path-to-skill>` 指本 Skill 的安装目录。

`scripts/run_publish.py` 发布因子包的完整评估资产。发布由用户决定；Agent 按需运行脚本，在失败时诊断和修复。

发布和同步 GitHub 记录还需 Git、GitHub CLI 及已授权账号。

## 脚本入口

Evaluate 完成且用户同意发布后，在项目根目录激活项目虚拟环境。需安装并登录 GitHub CLI（`gh`），参数与用法以脚本帮助为准：

~~~shell
uv run --locked python "<path-to-skill>/scripts/run_publish.py" --help
~~~

目录读取 `.env` 的 `FACTORZOO`、`DATA`、`ARTIFACTS`，见 [`.env.example`](../factor-protocol/assets/.env.example)。用 `--dry-run` 核对输入并查看上传计划，不修改远端或验收状态。

目标 Release 必须由 Design 创建，tag 与包名一致且非 Draft，保留参考 PDF 或经用户确认的缺失记录。

## 结果与验收

发布以因子包为单位，要求 README 第 4 节第 1–7 项通过，数据身份、验证和评估产物有效。全部声明列各上传 4 份 CSV 和 `overview.png`；`config.json` 保留在本地。

脚本核对远端文件名、大小及可用的内容摘要，再将 README 第 9 节绑定到已上传的 overview PNG，并同步完整 README 与标题。同步核验成功后才将 Release 转正并设为 latest，更新 active schema 并勾选第 8 项；失败以非零状态退出，第 8 项保留为未通过。

Design 创建 prerelease 时同步设计版 README，Publish 同步完成版。发布默认执行 `--operation publish`，同时更新本地图片链接和 Release 说明。仅重同步当前 README 与 Release 标题时运行：

```sh
uv run --locked python "<path-to-skill>/scripts/run_publish.py" --project-root . --factor-name <factor_name> --operation readme
```

该操作仅同步 Release 标题与正文，保留本地验收状态；`--dry-run` 只预览。Release 说明直接采用项目中当前因子 README 全文。

发布结果提交推送后，用 [sync_pr.py](scripts/sync_pr.py) 同步 Design 阶段已创建的同名 Draft PR，描述直接采用远程最新因子 README，用法见 `--help`。PR 由用户人工合并。

## 失败介入

先读终端的失败阶段和原因，再核对对应列的评估产物、目标 Release 和 README。

- 验收、身份或评估不符：核对当前定义、数据目录和全部声明列；按 Calculate、Validate、Evaluate 的对应错误返工。
- Release 缺失、仍为 Draft 或参考资料缺失：按 Design 补齐，确认仓库和 tag 与目标因子一致；无 PDF 的缺失原因须由 Design 脚本写入 README。
- GitHub 访问失败：检查 `gh` 登录、目标仓库权限和网络。
- 上传或远端资产核验失败：核对本地产物、失败文件及网络；修复后重跑，脚本覆盖同名评估资产，保留参考 PDF。
- 转正或 latest 核验失败：检查 `gh` 版本、Release 编辑权限和远端状态，再重跑完整发布。
- 名称或说明同步失败：核对 README 英文标题与包名是否对应，以及 `gh` 编辑权限，修复后重跑；脚本核验远端名称与完整正文。
- README 或 active schema 回写失败：检查模板、注册表格式和写入权限；远端上传成功仍需重跑完成本地验收。
- PR 同步失败：确认 Design 已创建同名 Draft PR、分支已推送且远程因子 README 存在，再按 `sync_pr.py --help` 排查。

修复后重跑脚本，以完整核验恢复验收。

## 参考

- [评估产物](../factor-evaluate/references/evaluation-artifacts.md)：各项结果的内容和使用说明。
- [因子设计](../factor-design/SKILL.md)：参考资料与 prerelease 准备。
- [因子工作流](../factor-workflow/SKILL.md)：阶段顺序与返工路径。
