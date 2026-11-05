# 因子数据读取

## 定位数据

项目 `.env` 的 `DATA` 指定输入数据目录，`ARTIFACTS` 指定研究产物目录，相对路径以项目根目录为基准。路径和因子产物位置见[项目路径配置](../SKILL.md#项目路径配置)。

输入表通常按以下方式组织：

```text
$DATA/<table>/date=YYYY-MM-DD/*.parquet
$DATA/<table>/*.parquet
```

先列出实际表目录与日期分区，再选择研究所需的数据。分区表从 `date=...` 读取日期；非分区表按文件中的日期字段读取。因子表由 `FactorStorage.table_root()` 定位到研究产物目录。

## 查看字段与样本

从一个相关日期分区开始，用 DuckDB `DESCRIBE SELECT` 查看字段和类型，用限定列的 `SELECT ... LIMIT` 查看少量样本。确定时间字段后，在选定范围内查询日期边界，再扩大研究范围。

```python
from pathlib import Path
from factor_protocol import FactorStorage

project_root = Path(".").resolve()
table = "<source_table>"
day = "<YYYY-MM-DD>"

with FactorStorage("sample_factor", project_root=project_root) as storage:
    partition = storage.table_root(table) / f"date={day}"
    files = storage.sql_literal(partition / "*.parquet")
    source = f"parquet_scan({files}, hive_partitioning=true, union_by_name=true)"
    fields = storage.df(f"DESCRIBE SELECT * FROM {source}")
    sample = storage.df(f"SELECT * FROM {source} LIMIT 5")
```

查看静态表时将扫描路径换成该表的实际文件。先查看结构，再限定所需列、日期和证券抽样；大表避免直接全历史扫描。样本用于理解数据结构，不代替因子验证所需的独立样本。

按实际字段和研究目标确定证券键、时间字段、记录粒度及关联方式。单位、复权口径或时间含义无法从数据确认时，取得依据后再使用。具体选择记录在因子 README 中。

## 有界读取

确认字段后，通过 `FactorStorage` 选择必要列并限定日期。下面的 `date` 应替换为实际使用的日期字段：

```python
with FactorStorage("sample_factor", project_root=project_root) as storage:
    query = storage.select_sql(
        table,
        columns=("<column_a>", "<column_b>"),
        where=(
            f"date >= CAST({storage.sql_literal(begin)} AS DATE)",
            f"date <= CAST({storage.sql_literal(end)} AS DATE)",
        ),
    )
    frame = storage.df(query)
```

生产计算使用 SQL；独立复算可读取有界 DataFrame。临时 DataFrame 用 `register_dataframe` 注册，使用结束后释放。SQL 空值判断使用 `IS NULL` / `IS NOT NULL`。

## 时间与更新

根据研究定义选择输出日期、预热范围、时间关联和可见时间。对输出时点 `t`，仅使用当时已经可知的信息；分区日期、业务事件时间和采集时间分别判断。

更新范围由因子使用的数据分区和交易日历决定。多表输入按因子定义对齐；事件类或低频表可按已确定的时间规则使用，不能一律按各表最新日期取最小值。Evaluate 使用执行滞后和持有期确定有完整收益标签的日期。
