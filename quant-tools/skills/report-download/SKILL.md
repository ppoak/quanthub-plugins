---
name: report-download
description: 根据报告来源调用对应脚本下载并校验研究报告 PDF，使用项目环境配置管理保存目录。
---

# 报告下载

属于 [Quant Tools](../../README.md)。环境准备见插件说明，配置模板见 [.env.example](../../.env.example)。下文 `<skill-root>` 指本 `SKILL.md` 所在目录，命令使用安装后的实际位置。

本 skill 根据报告来源选择下载脚本并校验 PDF。下载目录由项目根目录 `.env` 中的 `REPORT_DOWNLOAD_DIR` 配置；命令行 `--output-dir` 可为单次下载指定目录。项目根目录默认为当前目录，可通过 `--project-root` 指定；相对下载目录以该根目录为基准。参数优先级为命令行、进程环境变量、目标项目 `.env`、脚本默认值。

在其他项目运行时，例如：

```powershell
.venv/Scripts/python <report-download-skill>/scripts/bigquant_download.py "<BigQuant URL>" --project-root <project_root>
```

下载脚本按来源保存在 `scripts/` 中。脚本和 CLI 提示使用英文，面向用户的说明使用中文。运行 Python 命令使用项目环境中的 `.venv/Scripts/python`。

## 来源选择

| 来源 | 脚本 | 适用情况 |
| --- | --- | --- |
| `fxbaogao` | `scripts/fxbaogao_download.py` | 用户提供 `fxbaogao.com` 报告 ID，或需要下载发现报告 |
| `bigquant` | `scripts/bigquant_download.py` | 用户提供 BigQuant 文档页、PDF.js 查看器地址或 PDF 地址 |

来源不明确时，根据用户提供的 ID 或 URL 确认平台，再选择脚本。

## 下载发现报告

从 `https://www.fxbaogao.com/detail/<id>` 中提取报告 ID。把保存目录设置在项目根目录 `.env`：

```dotenv
REPORT_DOWNLOAD_DIR=<下载目录>
```

使用项目 `.env` 中的下载目录运行：

```dotenv
FXBG_USER_ID=<user_id>
FXBG_USER_TOKEN=<user_token>
```

```powershell
.venv/Scripts/python <skill-root>/scripts/fxbaogao_download.py <report_id>
```

下载受限时，使用用户提供的登录凭据或带 `auth_key` 的授权 PDF 地址。一次性凭据可以通过环境变量传入：

```powershell
$env:FXBG_USER_ID="<user_id>"
$env:FXBG_USER_TOKEN="<user_token>"
.venv/Scripts/python <skill-root>/scripts/fxbaogao_download.py <report_id>
```

使用授权地址时，传入 `--auth-url`；`--output-dir` 可单次覆盖 `.env` 中的目录：

```powershell
.venv/Scripts/python <skill-root>/scripts/fxbaogao_download.py <report_id> --auth-url "<authorized_pdf_url>" --output-dir <下载目录>
```

脚本会校验 PDF 内容并打印保存路径。

## 下载 BigQuant 报告

直接提供 BigQuant 文档页、PDF.js 查看器地址或 PDF 地址：

```powershell
.venv/Scripts/python <skill-root>/scripts/bigquant_download.py "<BigQuant 地址>"
```

脚本使用相同的 `REPORT_DOWNLOAD_DIR` 配置。运行时可用 `--output-dir` 指定本次下载目录。

## 增加下载来源

1. 在 `scripts/` 下为新来源增加 `<provider>_download.py`。
2. 提供报告 ID 或 URL 参数、来源所需的认证参数，以及 `--project-root` 和 `--output-dir`。
3. 默认保存目录读取 `REPORT_DOWNLOAD_DIR`，并校验下载内容为 PDF 后再报告成功。
4. 在来源表中补充脚本和适用条件。
5. 使用项目 Python 环境检查脚本语法：

```powershell
.venv/Scripts/python -m py_compile <skill-root>/scripts/<provider>_download.py
```

PDF、认证凭据和浏览器授权信息保存在本地；Git 版本中维护下载脚本和说明文档。

## 结果说明

完成后说明所用来源、PDF 保存路径，以及认证或授权状态；扩展下载来源时列明新增脚本和说明变更。
