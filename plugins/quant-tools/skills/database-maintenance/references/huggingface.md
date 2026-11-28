# Hugging Face Storage Bucket 备份

## 备份目标

将允许名单内的本地数据库 Parquet 表同步到由 `HUGGINGFACE_BUCKET_ID` 指定的私有 Storage Bucket，用于异机备份与恢复。生产备份使用 Storage Bucket 的对象同步能力。

## 脚本分工

| 文件 | 用途 |
| --- | --- |
| `scripts/huggingface/sync_huggingface_bucket.py` | 按表白名单生成同步目标，并通过 `hf buckets sync` 执行带重试的同步。 |


## 同步范围

脚本仅同步以下白名单数据表：

```text
quotes_day
index_quotes_day
index_weights
financial_report
industry_mapping
instruments_info
ex_factor
eastmoney_stock_sentiment
eastmoney_intraday_changes
xueqiu_social_count
xueqiu_hot_rank
xueqiu_status
xueqiu_cube_rebalance
```

`quotes_min` 默认从 `2015-01-01` 起按年份同步，命令使用 `hf buckets sync --include date=<YYYY>-*/**` 选择分区。备份范围由数据表白名单和分区配置共同确定。

远端对象按下列结构保存：

```text
tables/<table>/<local parquet path under table>
```

示例：

```text
tables/quotes_day/date=2026-06-05/data_0.parquet
tables/quotes_min/date=2026-06-05/<part>.parquet
tables/xueqiu_status/date=2026-06-02/part_0000.parquet
```

同步任务默认保留远端额外对象。审核完整本地与远端清单后，可通过 `--delete-remote-extra` 或 `HUGGINGFACE_BACKUP_DELETE_REMOTE_EXTRA=1` 启用远端清理；`--no-delete-remote-extra` 可覆盖环境变量并保留远端对象。

迁移时请检查旧 `.env`：原先只用于调度任务的 `HUGGINGFACE_BACKUP_DELETE_REMOTE_EXTRA=1` 现在也会影响手动运行。

## 环境配置

在项目根目录 `.env` 中设置：

```dotenv
HUGGINGFACE_TOKEN=<private-token>
HUGGINGFACE_BUCKET_ID=<user-or-org>/<private-bucket>
HUGGINGFACE_BACKUP_DATA_ROOT=data
HUGGINGFACE_BACKUP_REMOTE_PREFIX=tables
HUGGINGFACE_BACKUP_QUOTES_MIN_START_DATE=2015-01-01

HUGGINGFACE_BACKUP_MAX_RETRIES=5
HUGGINGFACE_BACKUP_RETRY_SLEEP_SECONDS=60
```

新工作树未包含被忽略的数据目录时，将 `HUGGINGFACE_BACKUP_DATA_ROOT` 指向实际数据目录，例如 `<absolute-data-directory>`。

## 运行命令

查看可用 bucket：

```sh
./.venv/Scripts/hf buckets list --token <token>
```

小范围 dry-run（连接 bucket 比对，不执行远端写入）：

```sh
uv run --locked python "<path-to-skill>/scripts/huggingface/sync_huggingface_bucket.py" --include-table eastmoney_intraday_changes --dry-run
```

执行完整备份，保留远端额外对象：

```sh
uv run --locked python "<path-to-skill>/scripts/huggingface/sync_huggingface_bucket.py" --no-delete-remote-extra
```


## 运行维护

- 备份表名单在 `FULL_TABLES` 中维护，新增来源时同步审核表名与分区范围。
- 生产同步使用 `hf buckets sync`；目标对象经本地与远端清单核对后再启用远端清理。
- 年度 `quotes_min` 同步失败时，使用相同配置重跑；已存在对象由同步工具按对象状态处理。
- 将 Hugging Face token 保存在 `.env`，并将数据和凭据保留在 Git 忽略范围。
