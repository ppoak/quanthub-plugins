# RiceQuant 数据采集

## 脚本分工

| 文件 | 用途 |
| --- | --- |
| `scripts/ricequant/client.py` | 在 RiceQuant 研究服务器调用 `rqdatac`，导出 Parquet 数据集并打包为 `data.tar.gz`。 |
| `scripts/ricequant/fetch_ricequant.py` | 通过 Selenium 运行服务器 notebook，下载并解包数据，规范化后 upsert 到本地 Hive 分区 Parquet。 |
| `scripts/scheduler.py` | 默认在交易日 `19:30` 调用 `fetch_ricequant.py`，并维护任务日志和通知。 |

## 环境配置

从项目根目录 `.env` 读取设置：

| 变量 | 用途 |
| --- | --- |
| `CHROME_DRIVER_PATH` | 可选 ChromeDriver 路径；留空时由 Selenium Manager 匹配本机 Chrome 并下载、缓存驱动。 |
| `RICEQUANT_USER` | RiceQuant 登录账号。 |
| `RICEQUANT_PASSWORD` | RiceQuant 登录密码。 |
| `DOWNLOAD_PATH` | Selenium 下载目录。 |
| `DATASET_PATH` | 本地数据根目录，例如 `data` 或自定义绝对路径。 |
| `RICEQUANT_HEADLESS` | 默认 `0`；设为 `1` 使用无头 Chrome。 |
| `RICEQUANT_SCHEDULE_TIME` | 调度时间，默认 `19:30`。 |
| `RICEQUANT_SCHEDULE_DAYS` | 调度星期，默认工作日；统一调度器再校验 A 股交易日。 |

调度器在任务结束后发送状态邮件。邮件使用 Database Maintenance 的全局 SMTP 配置与收件人，单封邮件以表格列出起止时间、耗时及各表实际完成的写入输入条数/路径，不附全量日志。

浏览器后端使用本机安装的 Chrome。默认无需手工安装 ChromeDriver；首次启动需要联网准备驱动，后续复用缓存。Chrome 更新后自动匹配驱动；显式指定 `CHROME_DRIVER_PATH` 时需自行保持版本匹配。旧 `EDGE_DRIVER_PATH` 不再被 RiceQuant 读取。

也可先通过命令行准备驱动，无安装界面：

```shell
uv run --locked python -c "import subprocess; from selenium.webdriver.common.selenium_manager import SeleniumManager; subprocess.run([str(SeleniumManager()._get_binary()), '--browser', 'chrome', '--avoid-browser-download'], check=True)"
```

## 数据契约

RiceQuant 服务器导出带数据集前缀的 Parquet 文件，并组成 `data.tar.gz`。本地脚本将压缩包成员映射到以下数据表：

| 数据集 | 分区 | Upsert 键 |
| --- | --- | --- |
| `quotes_day` | 从 `time` 提取 `date` | `time, code` |
| `quotes_min` | 从 `time` 提取 `date` | `time, code` |
| `index_quotes_day` | 从 `time` 提取 `date` | `time, code` |
| `financial_report` | 从 `time` 提取 `date` | `time, code, account_name` |
| `index_weights` | 从 `time` 提取 `date` | `time, code, index_code` |
| `industry_mapping` | 从 `time` 提取 `date` | `time, code` |
| `instruments_info` | 静态快照 | `code` |
| `ex_factor` | 单文件 | `ex_date, code` |

`ex_factor` 写入 `DATASET_PATH/ex_factor/data_0.parquet`。字段和分区说明见[本地数据表契约](data-catalog.md)。

## 运行命令

完整本地更新：

```shell
uv run --locked python "<path-to-skill>/scripts/ricequant/fetch_ricequant.py"
```

导入已下载的数据包：

```shell
uv run --locked python -c "import runpy; module=runpy.run_path('<path-to-skill>/scripts/ricequant/fetch_ricequant.py'); module['dump_data']('downloads/data.tar.gz', 'data')"
```

统一调度器清单与 dry-run：

```shell
uv run --locked python "<path-to-skill>/scripts/scheduler.py" --list-jobs
uv run --locked python "<path-to-skill>/scripts/scheduler.py" --run-once ricequant --date 2026-06-04 --dry-run
```

DuckDB 查询最近日期分区：

```python
import duckdb

print(duckdb.sql("""
    SELECT date, count(*) AS rows
    FROM parquet_scan('data/quotes_day/date=*/**/*.parquet', hive_partitioning=true)
    GROUP BY date ORDER BY date DESC LIMIT 5
""").df())
```

## 维护流程

- 新增数据集时，同步维护服务器端导出函数与本地 `dump_data()` 映射。
- 先用小型 `data.tar.gz` 验证导入，再运行全量日常更新。
- 出现 Upsert 键重复时，先检查并修正服务器导出结果，再执行导入。
- Selenium、下载和数据源服务状态记录在任务日志及调度通知中。
- 下载前将本地同名旧包保留为 `data.tar.gz.previous-<id>`，新包须完成下载且能读取 tar 成员后才进入导入。服务器归档保留，浏览器在成功或异常退出时关闭。
- 空数据集记录为跳过，不写入空分区。
- Notebook 执行结束后检查错误输出，执行失败时终止下载。
