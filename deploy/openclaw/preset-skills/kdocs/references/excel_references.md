# Excel 表格操作参考文档

本文件包含金山文档 MCP 中 Excel（在线表格）相关工具的完整 API 说明、详细调用示例、参数说明和返回值说明。

> **通用返回字段**：所有接口均返回 `code`（状态码，0 表示成功）和 `msg`（人可阅读的文本信息）。

---

## 通用说明

### Excel 工具概述

Excel 工具专门用于操作金山文档中的在线表格（Excel 格式），提供工作表管理、单元格数据读写、筛选、条件格式等功能。

### 创建 Excel 文件

通过 `create_file` 创建，`name` 须带 `.xlsx` 后缀，`file_type` 设为 `file`：

```json
{
  "name": "销售数据表.xlsx",
  "file_type": "file",
  "parent_id": "folder_abc123"
}
```

> Excel 文件的读取和更新都通过本文件中的 sheets 系列工具完成，不使用 `upload_file`。

### 读取 Excel 内容

读取 Excel 内容分两步：先通过 `sheets_list_worksheets` 获取工作表列表，再通过 `sheets_get_range_data` 获取指定工作表的选区数据。

**步骤一：获取工作表列表**

```json
{
  "file_id": "string"
}
```

返回所有工作表的 `worksheet_id`、`name`、`row_count`、`col_count` 等信息。

**步骤二：获取选区数据**

```json
{
  "file_id": "string",
  "worksheet_id": "string",
  "row_from": 0,
  "row_to": 100,
  "col_from": 0,
  "col_to": 10
}
```

返回指定范围内的单元格数据。详细参数和返回值见下方 `sheets_list_worksheets` 和 `sheets_get_range_data` 章节。

### 更新 Excel 内容

更新 Excel 内容同样先通过 `sheets_list_worksheets` 获取工作表信息，再通过 `sheets_update_range_data` 批量更新选区数据。

```json
{
  "file_id": "string",
  "worksheet_id": "string",
  "range_data": {
    "row_from": 0,
    "col_from": 0,
    "rows": [
      {
        "cells": [
          { "value": "区域" },
          { "value": "销售额" },
          { "value": "增长率" }
        ]
      },
      {
        "cells": [
          { "value": "广州" },
          { "value": "160万" },
          { "value": "15%" }
        ]
      }
    ]
  }
}
```

详细参数和返回值见下方 `sheets_update_range_data` 章节。

### 适用场景

| 场景 | 说明 |
|------|------|
| 数据记录 | 销售数据、财务报表 |
| 数据分析 | 结构化数据的读取与处理 |
| 报表汇总 | 多维度数据汇总 |

### 与智能表格（ksheet）的区别

| 特性 | Excel | 智能表格 ksheet |
|------|-------|----------------|
| 数据组织 | 传统行列表格 | 结构化字段+记录 |
| 视图 | 单一表格视图 | 多视图（表格/看板/日历等） |
| 字段类型 | 通用单元格 | 丰富字段类型（单选/多选/日期/附件等） |
| 适用场景 | 数据计算、报表 | 项目管理、CRM、任务跟踪 |

---

## 一、工作表管理

### 1. sheets_list_worksheets

#### 功能说明

获取指定 Excel 文件的所有工作表列表，包含每个工作表的名称、索引、行列范围等信息。

#### 调用示例

```json
{
  "file_id": "string"
}
```

#### 参数说明

- `file_id` (string, 必填): Excel 文件 ID

#### 返回值说明

```json
{
  "code": 0,
  "msg": "string",
  "data": {
    "sheets": [
      {
        "sheet_id": 0,
        "name": "Sheet1",
        "index": 0,
        "empty": false,
        "hidden": false,
        "max_row": 1000,
        "max_col": 26,
        "resource_type": "string",
        "active_area": {
          "row_from": 0,
          "row_to": 50,
          "col_from": 0,
          "col_to": 10
        }
      }
    ]
  }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `data.sheets[].sheet_id` | integer | 工作表 ID |
| `data.sheets[].name` | string | 工作表名称 |
| `data.sheets[].index` | integer | 工作表索引 |
| `data.sheets[].empty` | boolean | 是否为空 |
| `data.sheets[].hidden` | boolean | 是否隐藏 |
| `data.sheets[].max_row` | integer | 最大行数（工作表总容量） |
| `data.sheets[].max_col` | integer | 最大列数 |
| `data.sheets[].resource_type` | string | 资源类型 |
| `data.sheets[].active_area` | object | 活动区域，`row_to` 比 `max_row` 更有参考价值 |

---

### 2. sheets_create_worksheet

#### 功能说明

在指定 Excel 文件中创建新的工作表。可指定名称、列宽和插入位置。

#### 调用示例

```json
{
  "file_id": "string",
  "name": "销售数据",
  "col_width": 100,
  "after_sheet_id": 1
}
```

#### 参数说明

- `file_id` (string, 必填): Excel 文件 ID
- `name` (string, 可选): 工作表名称
- `col_width` (integer, 可选): 默认列宽
- `before_sheet_id` (integer, 可选): 插入到指定工作表之前，与 `after_sheet_id` 互斥
- `after_sheet_id` (integer, 可选): 插入到指定工作表之后，与 `before_sheet_id` 互斥

> 若不指定 `before_sheet_id` 和 `after_sheet_id`，默认插入到末尾。

#### 返回值说明

```json
{
  "code": 0,
  "msg": "string",
  "data": {
    "sheet_id": 2
  }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `data.sheet_id` | integer | 新建的工作表 ID |

---

### 3. sheets_update_worksheet

#### 功能说明

更新工作表属性，支持重命名和调整位置。

#### 调用示例

```json
{
  "file_id": "string",
  "worksheet_id": 1,
  "name": "新名称",
  "move_sheet_id": 2,
  "move_type": "sheet_move_type_before"
}
```

#### 参数说明

- `file_id` (string, 必填): Excel 文件 ID
- `worksheet_id` (integer, 必填): 工作表 ID
- `name` (string, 可选): 新名称
- `move_sheet_id` (integer, 可选): 移动参照的工作表 ID
- `move_type` (string, 可选): 移动方向。可选值：`sheet_move_type_before` / `sheet_move_type_after`

#### 返回值说明

```json
{
  "code": 0,
  "msg": "string",
  "data": {}
}
```

---

### 4. sheets_delete_worksheets

#### 功能说明

批量删除工作表。删除不可逆，且禁止删除最后一个工作表。

#### 调用示例

```json
{
  "file_id": "string",
  "worksheet_ids": [2, 3]
}
```

#### 参数说明

- `file_id` (string, 必填): Excel 文件 ID
- `worksheet_ids` (array[integer], 必填): 要删除的工作表 ID 列表

#### 返回值说明

```json
{
  "code": 0,
  "msg": "string",
  "data": {}
}
```

---

### 5. sheets_copy_worksheet

#### 功能说明

复制指定工作表，生成副本。

#### 调用示例

```json
{
  "file_id": "string",
  "worksheet_id": 1,
  "copy_first_sheet": false
}
```

#### 参数说明

- `file_id` (string, 必填): Excel 文件 ID
- `worksheet_id` (integer, 必填): 源工作表 ID
- `copy_first_sheet` (boolean, 可选): 是否复制第一个工作表，默认 `false`

#### 返回值说明

```json
{
  "code": 0,
  "msg": "string",
  "data": {
    "sheet_id": 3
  }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `data.sheet_id` | integer | 新工作表 ID |

---

## 二、数据操作

### 6. sheets_get_range_data

#### 功能说明

获取指定工作表中某个矩形选区内的单元格数据。行列索引均为 0-based。

#### 调用示例

```json
{
  "file_id": "string",
  "worksheet_id": 1,
  "row_from": 0,
  "row_to": 10,
  "col_from": 0,
  "col_to": 5
}
```

#### 参数说明

- `file_id` (string, 必填): Excel 文件 ID
- `worksheet_id` (integer, 必填): 工作表 ID
- `row_from` (integer, 必填): 起始行（0-based）
- `row_to` (integer, 必填): 结束行
- `col_from` (integer, 必填): 起始列（0-based）
- `col_to` (integer, 必填): 结束列

#### 返回值说明

```json
{
  "code": 0,
  "msg": "string",
  "data": {
    "range_data": [
      {
        "cell_text": "广州",
        "original_cell_value": "广州",
        "row_from": 0,
        "row_to": 0,
        "col_from": 0,
        "col_to": 0,
        "num_format": "string",
        "is_cell_pic": false,
        "pic_content": "",
        "sha1": ""
      }
    ]
  }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `data.range_data[].cell_text` | string | 单元格显示文本 |
| `data.range_data[].original_cell_value` | string | 原始值 |
| `data.range_data[].row_from` | integer | 起始行 |
| `data.range_data[].row_to` | integer | 结束行 |
| `data.range_data[].col_from` | integer | 起始列 |
| `data.range_data[].col_to` | integer | 结束列 |
| `data.range_data[].num_format` | string | 数字格式 |
| `data.range_data[].is_cell_pic` | boolean | 是否为图片 |
| `data.range_data[].pic_content` | string | 图片内容（如有） |
| `data.range_data[].sha1` | string | 图片 SHA1（如有） |

---

### 7. sheets_update_range_data

#### 功能说明

批量更新单元格选区数据，支持写值/公式、设置格式、合并单元格、写入图片。每项操作必须包含 `row_from`、`row_to`、`col_from`、`col_to`。

#### 调用示例

写入值：

```json
{
  "file_id": "string",
  "worksheet_id": 1,
  "range_data": [
    {
      "op_type": "cell_operation_type_formula",
      "row_from": 0,
      "row_to": 0,
      "col_from": 0,
      "col_to": 0,
      "formula": "广州"
    }
  ]
}
```

设置格式：

```json
{
  "file_id": "string",
  "worksheet_id": 1,
  "range_data": [
    {
      "op_type": "cell_operation_type_format",
      "row_from": 0,
      "row_to": 0,
      "col_from": 0,
      "col_to": 0,
      "xf": {
        "font": {
          "name": "微软雅黑",
          "dy_height": 220,
          "bls": true,
          "color": {"type": 2, "value": 4294967295}
        },
        "fill": {
          "type": 1,
          "back": {"type": 2, "value": 4294967040}
        }
      }
    }
  ]
}
```

#### 参数说明

- `file_id` (string, 必填): Excel 文件 ID
- `worksheet_id` (integer, 必填): 工作表 ID
- `range_data` (array[object], 必填): 操作列表，每项结构如下：

**range_data 每项字段：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `op_type` | string | 是 | 操作类型（见下表） |
| `row_from` | integer | 是 | 起始行（0-based） |
| `row_to` | integer | 是 | 结束行 |
| `col_from` | integer | 是 | 起始列（0-based） |
| `col_to` | integer | 是 | 结束列 |
| `formula` | string | 否 | 值或公式（`cell_operation_type_formula` 时使用） |
| `xf` | object | 否 | 格式对象（`cell_operation_type_format` 时使用） |
| `merge_type` | string | 否 | 合并类型（`cell_operation_type_merge` 时使用） |
| `cell_pic_info` | object | 否 | 图片信息（`cell_operation_type_picture` 时使用） |

**op_type 操作类型：**

| op_type | 说明 | 需要的字段 |
|---------|------|-----------|
| `cell_operation_type_formula` | 写值/公式 | `formula` |
| `cell_operation_type_format` | 设置格式 | `xf` |
| `cell_operation_type_merge` | 合并单元格 | `merge_type` |
| `cell_operation_type_picture` | 写入图片 | `cell_pic_info` |

**merge_type 合并类型：**`merge_type_center`（居中合并）/ `merge_type_across`（跨越合并）/ `merge_type_cells`（合并单元格）

**cell_pic_info 图片信息：**

```json
{
  "tag": "sheet_pic_type_url",
  "pic_content": "https://example.com/image.png",
  "width": 100,
  "height": 100
}
```

`tag` 可选值：`sheet_pic_type_local` / `sheet_pic_type_attachment` / `sheet_pic_type_url`

**xf 格式对象：**

```json
{
  "font": {
    "name": "微软雅黑",
    "dy_height": 220,
    "char_set": 134,
    "bls": false,
    "italic": false,
    "strikeout": false,
    "uls": 0,
    "sss": 0,
    "theme_font": 2,
    "color": {"type": 2, "value": 4294967295}
  },
  "wrap": false,
  "shrink_to_fit": false,
  "locked": true,
  "hidden": false,
  "alc_h": 0,
  "alc_v": 1,
  "indent": 0,
  "reading_order": 0,
  "trot": 0,
  "dg_left": 0,
  "dg_right": 0,
  "dg_top": 0,
  "dg_bottom": 0,
  "clr_left": {"type": 2, "value": 4278190080},
  "clr_right": {"type": 2, "value": 4278190080},
  "clr_top": {"type": 2, "value": 4278190080},
  "clr_bottom": {"type": 2, "value": 4278190080},
  "numfmt": "G/通用格式",
  "fill": {
    "type": 1,
    "back": {"type": 2, "value": 4294967040},
    "fore": {"type": 255}
  }
}
```

- `alc_h`：水平对齐（1=左, 2=居中, 3=右, 4=填充, 5=两端, 6=跨列, 7=分散）
- `alc_v`：垂直对齐（0=上, 1=中, 2=下, 3=两端, 4=分散）
- `dg_*`：边框线型（0=无, 1=细线, 2=中等, 3=虚线, 4=点线, 5=粗线, 6=双线, 7=细虚线）

#### 返回值说明

```json
{
  "code": 0,
  "msg": "string",
  "data": {}
}
```

---

### 8. sheets_delete_range_data

#### 功能说明

删除指定选区内的单元格数据，并可指定删除后的移位方向。

#### 调用示例

```json
{
  "file_id": "string",
  "worksheet_id": 1,
  "range_data": [
    {
      "row_from": 0,
      "row_to": 5,
      "col_from": 0,
      "col_to": 3
    }
  ],
  "shift_type": "shift_up"
}
```

#### 参数说明

- `file_id` (string, 必填): Excel 文件 ID
- `worksheet_id` (integer, 必填): 工作表 ID
- `range_data` (array[object], 必填): 要删除的选区列表，每项含 `row_from`、`row_to`、`col_from`、`col_to`
- `shift_type` (string, 可选): 删除后的移位方向，默认 `shift_up`。可选值：`shift_up`（下方上移）/ `shift_left`（右侧左移）

#### 返回值说明

```json
{
  "code": 0,
  "msg": "string",
  "data": {}
}
```

---

### 9. sheets_create_rows

#### 功能说明

在工作表末尾添加行数据。每项指定列号和对应的值。

#### 调用示例

```json
{
  "file_id": "string",
  "worksheet_id": 1,
  "range_data": [
    {
      "col": 0,
      "formula": "广州",
      "op_type": "cell_operation_type_formula"
    },
    {
      "col": 1,
      "formula": "150万",
      "op_type": "cell_operation_type_formula"
    }
  ]
}
```

#### 参数说明

- `file_id` (string, 必填): Excel 文件 ID
- `worksheet_id` (integer, 必填): 工作表 ID
- `range_data` (array[object], 必填): 单元格数据列表，每项结构：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `col` | integer | 是 | 列索引（0-based） |
| `formula` | string | 是 | 值或公式 |
| `op_type` | string | 是 | 操作类型，通常为 `cell_operation_type_formula` |

#### 返回值说明

```json
{
  "code": 0,
  "msg": "string",
  "data": {}
}
```

---

### 10. sheets_find_range_data

#### 功能说明

在指定选区内按条件查找/筛选单元格数据，支持条件过滤、关键词搜索和重复值检测。

#### 调用示例

```json
{
  "file_id": "string",
  "worksheet_id": 1,
  "range": {
    "row_from": 0,
    "row_to": 100,
    "col_from": 0,
    "col_to": 10
  },
  "filter": {
    "condition": [
      {
        "col": 2,
        "info": [
          {"value": ">=5"},
          {"value": "<=10"}
        ],
        "mode": "filter_mode_and"
      }
    ],
    "search": [
      {"col": 0, "value": ["广州"]}
    ]
  },
  "page": {
    "page": 1,
    "page_size": 50
  },
  "show_total": true
}
```

#### 参数说明

- `file_id` (string, 必填): Excel 文件 ID
- `worksheet_id` (integer, 必填): 工作表 ID
- `range` (object, 必填): 查找范围，含 `row_from`、`row_to`、`col_from`、`col_to`
- `filter` (object, 必填): 筛选条件（结构见下文）
- `page` (object, 可选): 分页参数，含 `page`（页码）和 `page_size`（每页条数）
- `option_cols` (array[integer], 可选): 要返回的列索引列表
- `show_total` (boolean, 可选): 是否返回匹配总数，默认 `false`
- `ignore_hidden_cell` (boolean, 可选): 是否忽略隐藏单元格，默认 `false`

**filter 结构：**

```json
{
  "condition": [
    {
      "col": 0,
      "info": [{"value": ">=5"}, {"value": "<=10"}],
      "mode": "filter_mode_and"
    }
  ],
  "search": [
    {"col": 2, "value": ["关键词"]}
  ],
  "duplicates": {
    "col": [0]
  }
}
```

- `condition[].mode`：`filter_mode_and`（同时满足）/ `filter_mode_or`（任一满足）

**筛选条件语法：**

| 类型 | 语法 | 示例 |
|------|------|------|
| 等于 | `=值` | `=1` |
| 不等于 | `<>值` | `<>1` |
| 大于 | `>值` | `>5` |
| 大于等于 | `>=值` | `>=5` |
| 小于 | `<值` | `<10` |
| 小于等于 | `<=值` | `<=10` |
| 文本等于 | `= 文本` | `= 北京` |
| 文本不等于 | `<> 文本` | `<> 北京` |
| 开头是 | `= 文本*` | `= 北京*` |
| 结尾是 | `=* 文本` | `=* 市` |
| 包含 | `=* 文本*` | `=* 京*` |
| 不包含 | `<> *文本*` | `<> *京*` |
| 为空 | `=` | `=` |

#### 返回值说明

```json
{
  "code": 0,
  "msg": "string",
  "data": {
    "range_data": [],
    "merge_range_data": [],
    "option_col": [],
    "total": 0,
    "result_type": 0
  }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `data.range_data` | array | 匹配的单元格数据 |
| `data.merge_range_data` | array | 合并区域数据 |
| `data.option_col` | array | 选项列数据 |
| `data.total` | integer | 匹配总数（需 `show_total=true`） |
| `data.result_type` | integer | 结果类型 |

---

## 三、筛选

### 11. sheets_create_filter

#### 功能说明

在指定选区上开启自动筛选。开启后使用 `sheets_update_filter` 设置具体条件。如需修改筛选范围，先 `sheets_delete_filter` 关闭再重新开启。

#### 调用示例

```json
{
  "file_id": "string",
  "worksheet_id": 1,
  "range": {
    "row_from": 0,
    "row_to": 100,
    "col_from": 0,
    "col_to": 10
  },
  "expand_filter_range": true
}
```

#### 参数说明

- `file_id` (string, 必填): Excel 文件 ID
- `worksheet_id` (integer, 必填): 工作表 ID
- `range` (object, 必填): 筛选范围，含 `row_from`、`row_to`、`col_from`、`col_to`
- `expand_filter_range` (boolean, 可选): 是否自动扩展筛选范围，默认 `true`

#### 返回值说明

```json
{
  "code": 0,
  "msg": "string",
  "data": {}
}
```

---

### 12. sheets_update_filter

#### 功能说明

更新已开启筛选的条件。

> **注意**：`col` 是相对于筛选区域 `col_from` 的偏移量（0-based），**不是**工作表的绝对列索引。例如筛选区域 `col_from=1`，要筛选绝对列 8，则传 `col=7`（8 − 1 = 7）。`sheets_get_filters` 返回的 `col` 是绝对列号，两者语义不同。

#### 调用示例

```json
{
  "file_id": "string",
  "worksheet_id": 1,
  "col": 0,
  "condition": {
    "operator": "filter_operator_and",
    "param1": {
      "custom_type": "filter_custom_type_greater",
      "val": "80"
    },
    "param2": {
      "custom_type": "filter_custom_type_less_equ",
      "val": "100"
    }
  }
}
```

#### 参数说明

- `file_id` (string, 必填): Excel 文件 ID
- `worksheet_id` (integer, 必填): 工作表 ID
- `col` (integer, 必填): 列偏移量（相对于筛选区域 `col_from`，0-based）
- `condition` (object, 必填): 筛选条件，结构如下：

**condition 结构：**

```json
{
  "operator": "filter_operator_and",
  "param1": {"custom_type": "string", "val": "string"},
  "param2": {"custom_type": "string", "val": "string"}
}
```

- `operator`：`filter_operator_and` / `filter_operator_or`（单条件时用 `filter_operator_or`）

**custom_type 可选值：**

| custom_type | 说明 |
|-------------|------|
| `filter_custom_type_greater` | 大于 |
| `filter_custom_type_greater_equ` | 大于等于 |
| `filter_custom_type_less` | 小于 |
| `filter_custom_type_less_equ` | 小于等于 |
| `filter_custom_type_equals` | 等于 |
| `filter_custom_type_not_equ` | 不等于 |
| `filter_custom_type_begin_with` | 开头是 |
| `filter_custom_type_end_with` | 结尾是 |
| `filter_custom_type_contains` | 包含 |
| `filter_custom_type_not_contains` | 不包含 |
| `filter_custom_type_between` | 介于 |

#### 返回值说明

```json
{
  "code": 0,
  "msg": "string",
  "data": {}
}
```

---

### 13. sheets_get_filters

#### 功能说明

获取工作表当前的筛选状态和条件列表。

> **注意**：返回的 `col` 是工作表的**绝对列索引**，与 `sheets_update_filter` 的 `col`（相对偏移量）不同。若需转换，需减去筛选区域的 `col_from`。

#### 调用示例

```json
{
  "file_id": "string",
  "worksheet_id": 1
}
```

#### 参数说明

- `file_id` (string, 必填): Excel 文件 ID
- `worksheet_id` (integer, 必填): 工作表 ID

#### 返回值说明

```json
{
  "code": 0,
  "msg": "string",
  "data": {
    "autofilter_infos": [
      {
        "col": 0,
        "condition": {
          "operator": "filter_operator_and",
          "param1": {"custom_type": "filter_custom_type_greater", "val": "80"},
          "param2": {"custom_type": "filter_custom_type_less_equ", "val": "100"}
        }
      }
    ]
  }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `data.autofilter_infos` | array | 筛选条件列表 |
| `data.autofilter_infos[].col` | integer | 绝对列索引 |
| `data.autofilter_infos[].condition` | object | 筛选条件 |

---

### 14. sheets_delete_filter

#### 功能说明

关闭并删除工作表上的自动筛选。

#### 调用示例

```json
{
  "file_id": "string",
  "worksheet_id": 1
}
```

#### 参数说明

- `file_id` (string, 必填): Excel 文件 ID
- `worksheet_id` (integer, 必填): 工作表 ID

#### 返回值说明

```json
{
  "code": 0,
  "msg": "string",
  "data": {}
}
```

---

## 四、条件格式

### 15. sheets_create_cf_rule

#### 功能说明

创建条件格式规则，对满足条件的单元格应用指定样式。

#### 调用示例

```json
{
  "file_id": "string",
  "worksheet_id": 1,
  "rule": {
    "cf_rule_type": "cf_rule_type_value_range",
    "operator": "cf_rule_operator_greater",
    "formula1": "90",
    "formula2": "",
    "ranges": [{"row_from": 1, "row_to": 100, "col_from": 2, "col_to": 2}],
    "lastone": false,
    "rank": "",
    "xf": {
      "font": {"color": {"type": 2, "value": 4294901760}},
      "fill": {"type": 1, "back": {"type": 2, "value": 4294967040}}
    }
  }
}
```

#### 参数说明

- `file_id` (string, 必填): Excel 文件 ID
- `worksheet_id` (integer, 必填): 工作表 ID
- `rule` (object, 必填): 条件格式规则，结构如下：

**rule 结构：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `cf_rule_type` | string | 是 | 条件类型（见下表） |
| `operator` | string | 是 | 运算符（见下表） |
| `formula1` | string | 否 | 公式/值1 |
| `formula2` | string | 否 | 公式/值2（仅 `between` / `not_between` 时需要） |
| `ranges` | array[object] | 是 | 应用范围，每项含 `row_from`、`row_to`、`col_from`、`col_to` |
| `xf` | object | 是 | 条件满足时的格式样式（结构同 `sheets_update_range_data` 的 `xf`） |
| `lastone` | boolean | 否 | 是否为最后一条规则 |
| `rank` | string | 否 | 排名值（`rank_average` 类型时使用） |

**cf_rule_type 条件类型：**

| cf_rule_type | 含义 | 需要的参数 |
|------|------|-----------|
| `cf_rule_type_value_range` | 单元格值判断 | `operator` + `formula1`（+ `formula2` 仅 between/not_between） |
| `cf_rule_type_expression` | 自定义公式 | `formula1`（如 `"=A1>5"`） |
| `cf_rule_type_contains_value` | 文本/状态判断 | `operator` |
| `cf_rule_type_time_period` | 日期时间段 | `operator` |
| `cf_rule_type_rank_average` | 排名/平均值 | `operator` + `rank` |

**operator 运算符参考：**

| 条件类型 | 可用运算符 |
|---------|-----------|
| value_range | `cf_rule_operator_between`, `cf_rule_operator_not_between`, `cf_rule_operator_equal`, `cf_rule_operator_not_equal`, `cf_rule_operator_greater`, `cf_rule_operator_less`, `cf_rule_operator_greater_equal`, `cf_rule_operator_less_equal` |
| contains_value（文本） | `cf_rule_operator_string_conclude`(包含), `cf_rule_operator_string_exclude`(不包含), `cf_rule_operator_string_begins_with`, `cf_rule_operator_string_ends_with` |
| contains_value（状态） | `cf_rule_operator_unique_values`, `cf_rule_operator_duplicate_values`, `cf_rule_operator_blanks_condition`, `cf_rule_operator_no_blanks_condition`, `cf_rule_operator_errors`, `cf_rule_operator_no_errors` |
| time_period | `cf_rule_operator_today`, `cf_rule_operator_yesterday`, `cf_rule_operator_tomorrow`, `cf_rule_operator_this_week`, `cf_rule_operator_last_week`, `cf_rule_operator_next_week`, `cf_rule_operator_this_month`, `cf_rule_operator_last_month`, `cf_rule_operator_next_month`, `cf_rule_operator_last_7_days` |
| rank_average | `cf_rule_operator_top_10`, `cf_rule_operator_top_10_percent`, `cf_rule_operator_last_10`, `cf_rule_operator_last_10_percent`, `cf_rule_operator_above_average`, `cf_rule_operator_below_average` |

#### 返回值说明

```json
{
  "code": 0,
  "msg": "string",
  "data": {}
}
```

---

### 16. sheets_get_cf_rules

#### 功能说明

获取工作表中已设置的所有条件格式规则。

#### 调用示例

```json
{
  "file_id": "string",
  "worksheet_id": 1
}
```

#### 参数说明

- `file_id` (string, 必填): Excel 文件 ID
- `worksheet_id` (integer, 必填): 工作表 ID

#### 返回值说明

```json
{
  "code": 0,
  "msg": "string",
  "data": {
    "rules": [
      {
        "cf_rule_type": "cf_rule_type_value_range",
        "operator": "cf_rule_operator_greater",
        "formula1": "90",
        "formula2": "",
        "priority": 1,
        "ranges": [{"row_from": 1, "row_to": 100, "col_from": 2, "col_to": 2}],
        "xf": {}
      }
    ]
  }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `data.rules` | array | 条件格式规则列表 |
| `data.rules[].cf_rule_type` | string | 条件类型 |
| `data.rules[].operator` | string | 运算符 |
| `data.rules[].formula1` | string | 公式/值1 |
| `data.rules[].formula2` | string | 公式/值2 |
| `data.rules[].priority` | integer | 规则优先级 |
| `data.rules[].ranges` | array | 应用范围 |
| `data.rules[].xf` | object | 格式样式 |

---

### 17. sheets_delete_cf_rules

#### 功能说明

删除指定选区内的所有条件格式规则。

#### 调用示例

```json
{
  "file_id": "string",
  "worksheet_id": 1,
  "ranges": [
    {"row_from": 0, "row_to": 100, "col_from": 0, "col_to": 10}
  ]
}
```

#### 参数说明

- `file_id` (string, 必填): Excel 文件 ID
- `worksheet_id` (integer, 必填): 工作表 ID
- `ranges` (array[object], 必填): 要清除条件格式的选区列表，每项含 `row_from`、`row_to`、`col_from`、`col_to`

#### 返回值说明

```json
{
  "code": 0,
  "msg": "string",
  "data": {}
}
```

---

## 工具速查表

| # | 工具名 | 分类 | 功能 | 必填参数 |
|---|--------|------|------|----------|
| 1 | `sheets_list_worksheets` | 工作表管理 | 获取工作表列表 | `file_id` |
| 2 | `sheets_create_worksheet` | 工作表管理 | 创建工作表 | `file_id` |
| 3 | `sheets_update_worksheet` | 工作表管理 | 重命名/移动工作表 | `file_id`, `worksheet_id` |
| 4 | `sheets_delete_worksheets` | 工作表管理 | 批量删除工作表 | `file_id`, `worksheet_ids` |
| 5 | `sheets_copy_worksheet` | 工作表管理 | 复制工作表 | `file_id`, `worksheet_id` |
| 6 | `sheets_get_range_data` | 数据操作 | 获取选区数据 | `file_id`, `worksheet_id`, `row_from`, `row_to`, `col_from`, `col_to` |
| 7 | `sheets_update_range_data` | 数据操作 | 批量更新选区数据 | `file_id`, `worksheet_id`, `range_data` |
| 8 | `sheets_delete_range_data` | 数据操作 | 删除选区数据 | `file_id`, `worksheet_id`, `range_data` |
| 9 | `sheets_create_rows` | 数据操作 | 末尾添加行 | `file_id`, `worksheet_id`, `range_data` |
| 10 | `sheets_find_range_data` | 数据操作 | 查找/筛选数据 | `file_id`, `worksheet_id`, `range`, `filter` |
| 11 | `sheets_create_filter` | 筛选 | 开启自动筛选 | `file_id`, `worksheet_id`, `range` |
| 12 | `sheets_update_filter` | 筛选 | 更新筛选条件 | `file_id`, `worksheet_id`, `col`, `condition` |
| 13 | `sheets_get_filters` | 筛选 | 获取筛选状态 | `file_id`, `worksheet_id` |
| 14 | `sheets_delete_filter` | 筛选 | 关闭筛选 | `file_id`, `worksheet_id` |
| 15 | `sheets_create_cf_rule` | 条件格式 | 创建条件格式规则 | `file_id`, `worksheet_id`, `rule` |
| 16 | `sheets_get_cf_rules` | 条件格式 | 获取条件格式规则 | `file_id`, `worksheet_id` |
| 17 | `sheets_delete_cf_rules` | 条件格式 | 删除条件格式 | `file_id`, `worksheet_id`, `ranges` |