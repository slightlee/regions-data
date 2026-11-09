# 行政区划数据与数据库设计

## 1. 文档范围

本文档只描述当前版本的数据来源、采集流程、清洗规则和 SQLite 数据库实现。

历史数据及旧版格式说明见 [`data/archive/README.md`](../data/archive/README.md)，不作为当前生产流程的输入或设计依据。

## 2. 数据来源

数据来源于中国·国家地名信息库：

- 行政区划代码页面：<https://dmfw.mca.gov.cn/XzqhVersionPublish.html>
- 接口服务说明：<https://dmfw.mca.gov.cn/interface.html>

官方页面声明发布全国省、地、县、乡四级行政区划建制代码。项目按官方接口当前返回内容采集，不对源头尚未发布、暂缺或尚未同步的数据做补充推断。

## 3. 数据处理流程

```text
fetch_regions.py
    ↓ 采集原始数据（data/raw/）
verify_data.py
    ↓ 验收采集结果
clean_regions_data.py
    ↓ 确定性码位规范化（data/processed/）
import_regions.py
    ↓ 导入 SQLite
regions.db
```

### 3.1 采集

采集脚本使用国家地名信息库页面提供的两类查询能力：

- **树查询**：一次获取省、地、县三级层级关系；1~3 级 `type` 字段采用接口返回原值；
- **列表查询**：通过分页获取完整记录；乡级 `type` 当前不返回，因此原始数据和数据库中均为 `NULL`。

具体接口地址和调用参数不随项目文档发布，请以官方接口说明和页面当前实现为准。分页响应即时缓存在 `data/raw/cache/`，用于断点续传和两类查询结果的交叉验收；缓存目录不进入版本库。

### 3.2 采集验收

`verify_data.py` 在清洗之前运行，检查：

1. 省、地、县、乡四级是否齐全，实际记录数是否等于 `meta.total`；
2. 原始码位是否符合当前接口返回形态：省级 2 位、地级 4 位、县级 6 位、乡级 9 位；
3. 每条乡级记录的父级代码能否解析到县级或地级单位；
4. 树接口和前缀搜索接口返回的 1~3 级代码、名称是否一致。

源头两个接口更新不同步时，差异会带完整行政归属路径输出，作为信息项记录；搜索接口存在而树接口不存在的记录，可能导致漏采，验收会失败。

### 3.3 清洗

清洗脚本只做确定性变换，不增加、不删除记录：

| 级别 | raw 代码形态 | processed / DB 代码形态 |
|------|---------------|--------------------------|
| 省级 | 2 位短码，如 `13` | 6 位，如 `130000` |
| 地级 | 4 位短码，如 `1303` | 6 位，如 `130300` |
| 县级 | 6 位，如 `130304` | 6 位，不变 |
| 乡级 | 9 位，如 `130304100` | 9 位，不变 |

清洗规则：

- 省、地、县级纯数字代码统一为 6 位；
- 乡级纯数字代码统一为 9 位；
- 父级代码同步规范化为 6 位；
- 非数字占位符原样保留，不补零造值。例如台湾省官方代码为 `资料暂缺`，processed 和数据库中仍为 `资料暂缺`；
- 输入和输出记录数必须相等，否则清洗终止。

清洗脚本对 `data/raw/` 中的源文件只读；输出写入 `data/processed/`，并与 raw 文件保持同名。`regions_*_tmp.json` 属于未完成采集文件，验收和清洗都会排除。

### 3.4 导入

导入脚本机械映射清洗结果，不补充源数据未提供的业务值：

- `type`：采用接口返回原值，乡级保持 `NULL`；
- `type_code`：接口不提供且无权威国家标准，保持 `NULL`；
- `depth`：当前实现等于 `level`；
- `path`：根据 `parent_code` 递归生成代码路径；
- `year`：取 `meta.crawl_time` 的年份，仅表示采集批次年份，不表示官方数据版本年份；
- `region_type_dict`：由实际入库数据按 `level + type` 聚合生成，不使用人工种子数据。

## 4. 行政层级与父子关系

当前数据库只存储 level 1~4，根节点不落库。

| level | depth | 说明 | 示例 |
|-------|-------|------|------|
| 1 | 1 | 省级 | 河北省、北京市、内蒙古自治区 |
| 2 | 2 | 地级 | 秦皇岛市、阿拉善盟、阿里地区 |
| 3 | 3 | 县级 | 北戴河区、正蓝旗、六枝特区 |
| 4 | 4 | 乡级 | 海滨镇、东华门街道 |

普通省份通常为：

```text
省级 → 地级 → 县级 → 乡级
```

但父子关系不能仅依赖 level 推断，必须以接口返回的 `parent_code` 为准。例如：

- 直辖市：北京市（level 1）直接下辖东城区（level 3）；
- 省直辖县级单位：县级记录可直接隶属于省级；
- 不设县级单位的地级市：东莞市等地级单位可直接下辖乡级单位。

因此级联查询统一按 `parent_code` 获取下一级，不假设固定层级深度。

## 5. 当前数据统计

当前生产批次：`20261109_105900`。

| 级别 | 数量 |
|------|-----:|
| 省级 | 34 |
| 地级 | 333 |
| 县级 | 2,847 |
| 乡级 | 38,723 |
| **总计** | **41,937** |

1~3 级接口实际返回的类型分布：

| level | type | 数量 |
|------:|------|-----:|
| 1 | 省 | 23 |
| 1 | 自治区 | 5 |
| 1 | 直辖市 | 4 |
| 1 | 特别行政区 | 2 |
| 2 | 地级市 | 293 |
| 2 | 自治州 | 30 |
| 2 | 地区 | 7 |
| 2 | 盟 | 3 |
| 3 | 县 | 1,302 |
| 3 | 市辖区 | 976 |
| 3 | 县级市 | 398 |
| 3 | 自治县 | 117 |
| 3 | 旗 | 49 |
| 3 | 自治旗 | 3 |
| 3 | 林区 | 1 |
| 3 | 特区 | 1 |

乡级类型接口当前不提供，38,723 条乡级记录的 `type` 和 `type_code` 均为 `NULL`。

## 6. SQLite 表结构

### 6.1 regions

```sql
CREATE TABLE regions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code VARCHAR(12) NOT NULL,
    name VARCHAR(100) NOT NULL,
    level TINYINT NOT NULL,
    depth TINYINT NOT NULL,
    parent_code VARCHAR(12),
    path VARCHAR(200),
    type VARCHAR(20),
    type_code TINYINT,
    year SMALLINT,
    status TINYINT NOT NULL DEFAULT 1,
    is_deleted TINYINT NOT NULL DEFAULT 0,
    sort_order INT DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

字段来源与语义：

| 字段 | 来源 | 当前语义 |
|------|------|----------|
| `id` | SQLite | 自增技术主键 |
| `code` | API + 清洗 | 省/地/县 6 位、乡级 9 位；非数字官方占位符原样保留 |
| `name` | API | 行政区划标准名称 |
| `level` | API | 1=省级、2=地级、3=县级、4=乡级 |
| `depth` | 程序重述 | 当前等于 `level` |
| `parent_code` | API + 清洗 | 父级代码，省级为 `NULL` |
| `path` | 程序计算 | 从根级区划到当前区划的代码路径，逗号分隔 |
| `type` | API | 1~3 级接口原值；乡级为 `NULL` |
| `type_code` | 未启用 | 当前全部为 `NULL` |
| `year` | 批次元数据 | `meta.crawl_time` 的年份 |
| `status` | 数据库默认值 | 当前均为 1 |
| `is_deleted` | 数据库默认值 | 当前均为 0 |
| `sort_order` | 程序计算 | 按导入顺序递增 |
| `created_at` / `updated_at` | SQLite | 导入时间 |

约束与索引：

```sql
CREATE UNIQUE INDEX uk_code_year ON regions(code, year);
CREATE INDEX idx_parent_code ON regions(parent_code);
CREATE INDEX idx_level ON regions(level);
CREATE INDEX idx_depth ON regions(depth);
CREATE INDEX idx_path ON regions(path);
CREATE INDEX idx_name ON regions(name);
```

### 6.2 region_type_dict

该表由 `regions` 中实际存在的非空 `type` 聚合生成，不手工维护。

```sql
CREATE TABLE region_type_dict (
    level TINYINT NOT NULL,
    type_name VARCHAR(20) NOT NULL,
    region_count INT NOT NULL,
    batch VARCHAR(20),
    PRIMARY KEY (level, type_name)
);
```

当前共 16 行；乡级类型未由接口提供，因此不存在 level 4 字典记录。

## 7. 当前数据示例

| code | name | level | depth | type | parent_code | path |
|------|------|------:|------:|------|-------------|------|
| `130000` | 河北省 | 1 | 1 | 省 | `NULL` | `130000` |
| `130300` | 秦皇岛市 | 2 | 2 | 地级市 | `130000` | `130000,130300` |
| `130304` | 北戴河区 | 3 | 3 | 市辖区 | `130300` | `130000,130300,130304` |
| `130304100` | 海滨镇 | 4 | 4 | `NULL` | `130304` | `130000,130300,130304,130304100` |
| `110000` | 北京市 | 1 | 1 | 直辖市 | `NULL` | `110000` |
| `110101` | 东城区 | 3 | 3 | 市辖区 | `110000` | `110000,110101` |
| `资料暂缺` | 台湾省 | 1 | 1 | 省 | `NULL` | `资料暂缺` |

## 8. 常用查询

### 查询所有省级行政区划

```sql
SELECT code, name, type
FROM regions
WHERE level = 1 AND is_deleted = 0
ORDER BY sort_order, code;
```

### 获取指定区划的直接下级

```sql
SELECT code, name, level, type
FROM regions
WHERE parent_code = ? AND is_deleted = 0
ORDER BY sort_order, code;
```

该查询可同时适配普通省份、直辖市、省直辖县级单位和地级直辖乡级单位。

### 查询某条路径中的全部区划

```sql
SELECT code, name, level, type
FROM regions
WHERE code IN ('130000', '130300', '130304', '130304100')
ORDER BY level;
```

### 名称模糊搜索

```sql
SELECT code, name, level, type
FROM regions
WHERE name LIKE '%北戴河%' AND is_deleted = 0;
```

## 9. 相关文件

- [`scripts/fetch_regions.py`](../scripts/fetch_regions.py)：采集
- [`scripts/verify_data.py`](../scripts/verify_data.py)：采集结果验收
- [`scripts/clean_regions_data.py`](../scripts/clean_regions_data.py)：清洗
- [`scripts/import_regions.py`](../scripts/import_regions.py)：导入
- [`data/archive/README.md`](../data/archive/README.md)：历史归档说明
