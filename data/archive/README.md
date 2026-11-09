# 历史数据归档

本目录用于保存已经退出生产目录的历史数据。生产流程只读取 `data/raw/`、`data/processed/` 和 `data/regions.db`，不会自动读取本目录。

## 目录规范

同一采集批次的原始数据和清洗数据应成对归档：

```text
{crawl_timestamp}/
├── raw/
│   └── regions_{crawl_timestamp}.json
└── processed/
    └── regions_{crawl_timestamp}.json
```

- `raw/`：官方接口返回的原始采集结果。
- `processed/`：同一 raw 文件经当时版本清洗脚本产生的结果；文件名与 raw 保持一致。
- 两者同时保存，才能完整追溯“源数据 → 清洗结果”的处理链路。

旧版独立格式无法对应当前 raw/processed 流程时，使用：

```text
{crawl_timestamp}/snapshot/
```

`snapshot/` 只表示历史快照，不代表当前 raw 或 processed 数据格式。

## 当前归档

### `20251224_142640/snapshot/`

- 文件：`regions_20251224_142640.json`
- 记录数：42,176
- 格式：旧版 12 位行政区划代码，记录中包含 `depth` 和细粒度 `type`
- 性质：独立历史快照，无法与当前清洗流程建立明确的一对一关系

### `20260304_173812/`

- `raw/regions_20260304_173812.json`：国家地名信息库采集原始数据，41,935 条
- `processed/regions_20260304_173812.json`：上述 raw 的清洗结果，41,935 条
- 两个文件通过 processed meta 中的 `original_file`、`cleaned=true` 明确关联

## 注意事项

- 不要将 `regions_*_tmp.json` 放入归档；它是未完成采集的断点文件。
- 归档文件不得作为当前生产数据输入。若需复现历史版本，应显式指定文件路径并记录使用的脚本版本。
- 移动归档文件后，应保持 raw/processed 的批次时间戳和文件名不变。
