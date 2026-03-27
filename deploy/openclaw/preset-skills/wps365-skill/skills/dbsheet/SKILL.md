---
name: wps365-dbsheet
description: 多维表（DbSheet）查询与管理。获取 Schema（数据表/视图/字段）、列举/检索/创建/更新/删除记录；支持按条件筛选、删除空记录、创建数据表与视图。
---

## 主要功能

- **获取 Schema**：查看数据表、视图、字段结构
- **列举记录**：查询数据表中的所有记录
- **检索记录**：按条件筛选记录
- **创建记录**：向数据表添加新记录
- **更新记录**：修改现有记录
- **删除记录**：删除指定记录或空记录
- **创建数据表**：新建数据表
- **创建视图**：新建视图

## 可用命令

```bash
# 获取多维表 Schema
python3 skills/dbsheet/run.py schema <file_id> [--sheet-id <sheet_id>] [--json]

# 列举记录
python3 skills/dbsheet/run.py list-records <file_id> <sheet_id> [--limit <limit>] [--offset <offset>] [--json]

# 检索记录（按条件筛选）
python3 skills/dbsheet/run.py search-records <file_id> <sheet_id> [--filter <filter>] [--limit <limit>] [--offset <offset>] [--json]

# 创建记录
python3 skills/dbsheet/run.py create-records <file_id> <sheet_id> --json '<record_data>'

# 更新记录
python3 skills/dbsheet/run.py update-record <file_id> <sheet_id> <record_id> --json '<update_data>'

# 删除记录
python3 skills/dbsheet/run.py delete-record <file_id> <sheet_id> <record_id>

# 删除空记录
python3 skills/dbsheet/run.py delete-empty-records <file_id> <sheet_id>

# 创建数据表
python3 skills/dbsheet/run.py create-sheet <file_id> --json '<sheet_config_json>'

# 创建视图
python3 skills/dbsheet/run.py create-view <file_id> <sheet_id> --name <view_name> --type <view_type> --options '<options_json>'
```

## 📝 字段值格式说明

### 格式1：JSON对象格式（推荐）
```json
[
  {
    "字段名1": "值1",
    "字段名2": 123,
    "字段名3": "2026-03-11"
  }
]
```

### 格式2：字符串格式（旧版）
```json
[
  {
    "fields_value": "字段名1=值1|字段名2=值2|字段名3=值3"
  }
]
```

**注意**：字段名必须与数据表中的字段名完全匹配，区分大小写。

## 🚀 完整使用流程示例

### 步骤1：创建多维表文档
```bash
python3 skills/drive/run.py create "我的多维表.dbt"
```

### 步骤2：查看表结构
```bash
python3 skills/dbsheet/run.py schema <文件ID>
```

### 步骤3：清理空记录（重要！）
```bash
python3 skills/dbsheet/run.py delete-empty-records <文件ID> 1
```
**注意**：新建的多维表默认会预留18行空记录，建议先清理空记录再添加新数据，避免新记录从第19行开始。

### 步骤4：创建记录
```bash
python3 skills/dbsheet/run.py create-records <文件ID> 1 --json '[{"名称": "测试", "数量": 1, "日期": "2026-03-11", "状态": "测试"}]'
```

### 步骤5：验证记录
```bash
python3 skills/dbsheet/run.py list-records <文件ID> 1
```

### 步骤6：更新记录
```bash
python3 skills/dbsheet/run.py update-records <文件ID> 1 --json '[{"id": "记录ID", "fields_value": {"名称": "更新后", "数量": 2, "日期": "2026-03-12", "状态": "已完成"}}]'
```

### 步骤7：删除记录
```bash
python3 skills/dbsheet/run.py delete-records <文件ID> 1 记录ID
```

## 使用示例

### 1. 获取多维表 Schema
```bash
python3 skills/dbsheet/run.py schema <文件ID>
```

### 2. 列举记录
```bash
python3 skills/dbsheet/run.py list-records <文件ID> 1
```

### 3. 检索记录（按条件筛选）
```bash
python3 skills/dbsheet/run.py search-records <文件ID> 1 --filter '名称包含"测试"'
```

### 4. 创建记录（JSON对象格式）
```bash
python3 skills/dbsheet/run.py create-records <文件ID> 1 --json '[{"名称": "测试记录", "数量": 10, "日期": "2026-03-11", "状态": "进行中"}]'
```

### 5. 创建记录（字符串格式）
```bash
python3 skills/dbsheet/run.py create-records <文件ID> 1 --json '[{"fields_value": "名称=测试记录|数量=10|日期=2026-03-11|状态=进行中"}]'
```

### 6. 更新记录
```bash
python3 skills/dbsheet/run.py update-record <文件ID> 1 record_123 --json '{"名称": "更新后的记录", "状态": "已完成"}'
```

### 7. 删除记录
```bash
python3 skills/dbsheet/run.py delete-record <文件ID> 1 record_123
```

### 8. 删除空记录
```bash
python3 skills/dbsheet/run.py delete-empty-records <文件ID> 1
```

### 9. 创建数据表（重要：使用正确的字段格式）
```bash
python3 skills/dbsheet/run.py create-sheet <文件ID> --json '{
  "name": "新数据表",
  "fields": [
    {"name": "字段1", "field_type": "MultiLineText"},
    {"name": "字段2", "field_type": "Number"}
  ],
  "views": [
    {"name": "表格视图", "view_type": "Grid"}
  ]
}'
```

### 10. 创建视图
```bash
python3 skills/dbsheet/run.py create-view <文件ID> 1 --name "我的视图" --type "grid" --options '{"filter": {"条件": "状态=已完成"}}'
```

## 🔧 常见问题与解决方法

### 问题1：字段未找到
**错误信息**: "Field not found"
**原因**: 字段名不匹配或字段不存在
**解决**: 使用`schema`命令查看正确的字段名

### 问题2：数据类型错误
**错误信息**: "invalid value for type"
**原因**: 数据类型不匹配
**解决**: 确保数字字段使用数字类型，日期字段使用字符串格式

### 问题3：权限问题
**错误信息**: "permission denied"
**原因**: 没有操作权限
**解决**: 检查文件ID是否正确，确认有访问权限

### 问题4：JSON格式错误
**错误信息**: "cannot unmarshal fieldsValue"
**原因**: JSON格式不正确
**解决**: 使用正确的JSON对象格式，确保字段名用双引号包裹

### 问题5：create-sheet命令字段格式错误
**错误信息**: "usage: run.py create-sheet [-h] --json JSON file_id"
**原因**: 字段定义使用了错误的属性名（使用`type`而不是`field_type`）
**解决**: 使用正确的字段格式：`{"name": "字段名", "field_type": "字段类型"}`

### 问题6：search-records命令误解
**错误理解**: 以为`search-records`是条件筛选
**正确理解**: `search-records`是按记录ID查询，不是条件筛选
**使用**: `python3 skills/dbsheet/run.py search-records <file_id> <sheet_id> <record_id1> <record_id2>`

## 🐛 调试建议

### 1. 使用--json参数
```bash
python3 skills/dbsheet/run.py schema <文件ID> --json
```

### 2. 逐步测试
1. 先测试单条记录
2. 验证字段名和数据类型
3. 再批量添加记录

### 3. 验证步骤
1. 创建文档 → 获取文件ID
2. 查看schema → 确认字段结构
3. **清理空记录** → 删除预留的18行空记录
4. 添加测试记录 → 验证格式
5. 查询记录 → 确认添加成功

## 🏆 最佳实践

### 1. 字段命名规范
- 使用中文或英文，避免特殊字符
- 保持字段名简洁明确

### 2. 数据类型选择
- **创建记录时**：
  - 文本字段：使用`text`类型
  - 数字字段：使用`number`类型
  - 日期字段：使用`date`类型
  - 选择字段：使用`singleSelect`或`multiSelect`类型
- **创建数据表时**（`create-sheet`命令）：
  - 文本字段：使用`"field_type": "MultiLineText"`
  - 数字字段：使用`"field_type": "Number"`
  - 日期字段：使用`"field_type": "Date"`
  - 单选字段：使用`"field_type": "SingleSelect"`

### 3. 字段类型对照表
| 功能 | 创建记录时 | 创建数据表时 |
|------|------------|--------------|
| 文本字段 | `"text"` | `"field_type": "MultiLineText"` |
| 数字字段 | `"number"` | `"field_type": "Number"` |
| 日期字段 | `"date"` | `"field_type": "Date"` |
| 单选字段 | `"singleSelect"` | `"field_type": "SingleSelect"` |
| 多选字段 | `"multiSelect"` | `"field_type": "MultiSelect"` |

### 3. 批量操作
- 小批量添加（建议每次不超过10条）
- 使用JSON数组格式批量添加
- 添加后立即验证

### 4. 文档管理
- 记录文件ID和链接ID
- 定期备份重要数据
- 使用有意义的文档名称
- 新建文档后先清理空记录再添加数据
- 使用JSON文件存储复杂数据，避免命令行参数过长

## 参数说明

- `<file_id>`: 多维表文件ID（必填）
- `<sheet_id>`: 数据表ID（必填，通常为1）
- `<record_id>`: 记录ID（更新/删除时使用）
- `--json`: 输出JSON格式（便于程序处理）
- `--limit`: 返回记录数量限制
- `--offset`: 返回记录偏移量
- `--filter`: 筛选条件
- `--name`: 数据表/视图名称
- `--type`: 视图类型（grid/kanban/calendar等）
- `--json`: 数据表配置JSON（`create-sheet`命令使用）
- `--options`: 视图选项JSON

## 注意事项

1. **文件ID获取**：通过`drive`技能的`create`或`upload`命令获取
2. **默认数据表**：新创建的多维表通常有一个默认数据表（sheet_id=1）
3. **字段名匹配**：字段名必须完全匹配，包括空格和标点
4. **数据类型**：注意数字字段不要加引号，日期字段使用字符串格式
5. **权限验证**：确保有操作该多维表的权限
6. **空记录清理**：新建的多维表默认会预留18行空记录，建议先使用`delete-empty-records`命令清理空记录，再添加新数据，避免新记录从第19行开始
7. **创建数据表格式**：`create-sheet`命令的`--json`参数中，字段必须使用`field_type`属性（如`"field_type": "MultiLineText"`），而不是`type`属性
8. **单选字段选项**：`SingleSelect`字段需要预先定义选项，添加记录时值必须完全匹配定义的选项，否则可能保存为空

## 相关技能

- [wpsv7-drive](../drive/SKILL.md)：云文档管理（创建/上传多维表）
- [wpsv7-calendar](../calendar/SKILL.md)：日历与日程管理
- [wpsv7-contacts](../contacts/SKILL.md)：通讯录搜索
- [wpsv7-meeting](../meeting/SKILL.md)：会议管理
- [wpsv7-im](../im/SKILL.md)：聊天会话管理