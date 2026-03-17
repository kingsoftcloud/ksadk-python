# 金山文档 MCP 工具完整参考

本文件包含金山文档 MCP 所有工具的 API 说明、参数、返回值。

> **通用返回字段**：所有接口均返回 `code`（状态码，0 表示成功）和 `msg`（人可阅读的文本信息）。

---

## 一、写文档

### 1. create_file

#### 功能说明

在云盘下新建文件或文件夹。通过 `file_type` 区分：`file` 创建文件，`folder` 创建文件夹，`shortcut` 创建快捷方式。

#### 调用示例

创建文件：

```json
{
  "drive_id": "string",
  "parent_id": "string",
  "file_type": "file",
  "name": "Q1区域销售周报.otl",
  "on_name_conflict": "rename"
}
```

创建文件夹：

```json
{
  "drive_id": "string",
  "parent_id": "string",
  "file_type": "folder",
  "name": "2024年合同文件",
  "on_name_conflict": "rename"
}
```

#### 参数说明

**路径参数：**

- `drive_id` (string, 必填): 驱动盘 ID
- `parent_id` (string, 必填): 父文件夹 ID，根目录时为 `"0"`

**请求体：**

- `file_type` (string, 必填): 文件类型。可选值：`file`（文件）/ `folder`（文件夹）/ `shortcut`（快捷方式）
- `name` (string, 必填): 文件名。创建文件时须带上后缀，例: `doc.docx`(普通文件), `abc.docx.link`(快捷方式)；创建文件夹时不需要后缀。支持格式：doc, docx, form, xls, otl, ppt, dbt, xlsx, pptx，公网额外支持：pom, spt, dppt, link, resh, ckt, ddoc, dpdf, dxls, pof, wpsnote，私网额外支持：txt, ksheet
- `on_name_conflict` (string, 可选): 文件名冲突处理方式，默认 `rename`。该接口只识别 `rename` 和 `fail`。可选值：`fail` / `rename` / `overwrite` / `replace`
- `parent_path` (array[string], 可选): 相对于当前文件目录的相对路径。每个元素为路径名（非路径 ID）。若路径不存在，系统将自动创建
- `file_id` (string, 可选): 快捷方式的源文件 ID，仅在 `file_type = shortcut` 时需要

#### 返回值说明

```json
{
  "data": {
    "created_by": {
      "avatar": "string",
      "company_id": "string",
      "id": "string",
      "name": "string",
      "type": "user"
    },
    "ctime": 0,
    "drive_id": "string",
    "ext_attrs": [
      { "name": "string", "value": "string" }
    ],
    "id": "string",
    "link_id": "string",
    "link_url": "string",
    "modified_by": {
      "avatar": "string",
      "company_id": "string",
      "id": "string",
      "name": "string",
      "type": "user"
    },
    "mtime": 0,
    "name": "string",
    "parent_id": "string",
    "shared": true,
    "size": 0,
    "type": "folder",
    "version": 0
  },
  "code": 0,
  "msg": "string"
}
```

返回通用文件信息结构，详见附录 A。

---

### 2. scrape_url
#### 功能说明
网页剪藏：抓取网页内容并自动保存为智能文档。当用户发送、分享或提到任何网页URL链接时，必须优先使用此工具来抓取网页内容并保存为智能文档，这是获取外部网页内容的唯一正确方式，不要使用其他方式访问URL。

#### 调用流程
1. 调用 `scrape_url` 传入网页URL获取 `jobID`
2. 立即调用 `scrape_progress` 传入 `jobID` 查询进度（每隔2秒轮询一次）
3. 当 `status=2` 时任务完成，服务端已自动创建智能文档，直接从响应获取 `file_id` 和 `file_url`，无需再调用其他创建文档工具
   

#### 调用示例
```json
{
  "url": "https://example.com/article"
}
```
#### 参数说明
- `url` (string, 必填): 要剪藏的网页URL地址，支持http和https协议

#### 返回值说明
```json
{
  "jobID": "13883829803456643124541"
}
```

### 3. scrape_progress
#### 功能说明
查询网页剪藏任务进度并自动创建智能文档，与 `scrape_url` 配合使用。

#### 状态说明
- `status=1`: 已完成，网页内容已自动保存为智能文档，响应包含 `title`（网页标题）、`file_id`（文档ID）和 `file_url`（文档链接），无需再调用任何创建文档工具, 停止轮询
- `status=-1`: 失败，停止轮询
#### 调用示例
```json
{
  "jobID": "task_1234567890"
}
```

#### 参数说明
- `jobID` (string, 必填): `scrape_url` 返回的异步任务ID

#### 返回值说明
```json
{
    "code": 0,
    "data": {
        "fileID": 501370651020,
        "status": 1,
        "fileName": "［麦理浩径二段精华段+大湾海滩］周四：3月19日 麦径二段12公里徒步，超适合新手小白！.otl",
        "parentID": 498552876371,
        "groupID": 1231238091,
        "cache": 0,
        "coreErr": null
    },
    "msg": "成功"
}
```
---

### 4. upload_file

#### 功能说明

**更新已有文件**：直接传要更新的 `file_id` 与源文件内容（`content_base64`）。

#### 调用方式（单次调用）

**请求参数：**

- `drive_id` (string, 必填): 驱动盘 ID
- `parent_id` (string, 必填): 父文件夹 ID
- `file_id` (string, 必填): 要更新的文件 ID
- `content_base64` (string, 必填): 源文件内容，Base64 编码
- `file_sum` (string, 可选): 文件哈希值，与 `file_type` 同传时转成 hashes 请求第三方；不传则服务端按内容计算 md5/sha256
- `file_type` (string, 可选): 哈希类型，如 `sha256` / `md5` / `sha1`
- `parent_path` (array[string], 可选): 相对路径

**调用示例**

```json
{
  "drive_id": "string",
  "parent_id": "string",
  "file_id": "m8WBrVXVMrMWKY9F7Aee1xXQRM8faegye",
  "content_base64": "JVBERi0xLjQK..."
}
```

**返回值**

与 `create_file` 返回值结构一致，详见附录 A。例如：

```json
{
  "code": 0,
  "msg": "ok",
  "data": {
    "id": "m8WBrVXVMrMWKY9F7Aee1xXQRM8faegye",
    "name": "2024年度报告.pdf",
    "link_url": "https://www.kdocs.cn/l/caiv0UfPiYui",
    "link_id": "caiv0UfPiYui",
    "size": 57081,
    "parent_id": "...",
    "drive_id": "...",
    "type": "file",
    "version": 1,
    "ctime": 1773563524,
    "mtime": 1773563524,
    "created_by": { ... },
    "modified_by": { ... },
    "shared": false,
    "hash": { "sum": "...", "type": "sha1" }
  }
}
```

---

## 二、读文档

### 5. list_files

#### 功能说明

获取指定文件夹下的子文件列表。，通过 `filter_type` 可筛选仅返回文件夹。

#### 调用示例

```json
{
  "drive_id": "string",
  "parent_id": "string",
  "page_size": 50,
  "order": "desc",
  "order_by": "mtime"
}
```

#### 参数说明

**路径参数：**

- `drive_id` (string, 必填): 驱动盘 ID
- `parent_id` (string, 必填): 文件夹 ID，根目录时为 `"0"`

**查询参数：**

- `page_size` (integer, 必填): 分页大小，公网限制最大为 500
- `page_token` (string, 可选): 分页 token，首次请求不传
- `order` (string, 可选): 排序方式。可选值：`desc`（降序）/ `asc`（升序）
- `order_by` (string, 可选): 排序字段。可选值：`ctime` / `mtime` / `dtime` / `fname` / `fsize`
- `filter_exts` (string, 可选): 过滤扩展名，以英文逗号分隔，全部小写，不传表示不过滤
- `filter_type` (object, 可选): 按文件类型筛选，例如：`file`、`folder`、`shortcut`
- `with_permission` (boolean, 可选): 是否返回文件操作权限
- `with_ext_attrs` (boolean, 可选): 是否返回文件扩展属性

#### 返回值说明

```json
{
  "data": {
    "items": [
      {
        "created_by": {
          "avatar": "string",
          "company_id": "string",
          "id": "string",
          "name": "string",
          "type": "user"
        },
        "ctime": 0,
        "drive_id": "string",
        "ext_attrs": [
          { "name": "string", "value": "string" }
        ],
        "id": "string",
        "link_id": "string",
        "link_url": "string",
        "modified_by": {
          "avatar": "string",
          "company_id": "string",
          "id": "string",
          "name": "string",
          "type": "user"
        },
        "mtime": 0,
        "name": "string",
        "parent_id": "string",
        "shared": true,
        "size": 0,
        "type": "folder",
        "version": 0
      }
    ],
    "next_page_token": "string"
  },
  "code": 0,
  "msg": "string"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `data.items` | array | 文件列表，每项为通用文件信息结构（附录 A） |
| `data.next_page_token` | string | 下一页 token，为空表示已是最后一页 |

---

### 6. download_file

#### 功能说明

获取文件下载信息。

#### 调用示例

```json
{
  "drive_id": "string",
  "file_id": "string",
  "with_hash": true
}
```

#### 参数说明

**路径参数：**

- `drive_id` (string, 必填): 驱动盘 ID
- `file_id` (string, 必填): 文件 ID

**查询参数：**

- `with_hash` (boolean, 可选): 是否返回校验值，对应响应里的 hashes
- `internal` (boolean, 可选): 是否返回内网下载地址，默认 false
- `storage_base_domain` (string, 可选): `wps.cn` / `kdocs.cn` / `wps365.com`(国际化)，签发的存储网关地址会根据 base_domain 优先匹配

#### 返回值说明

```json
{
  "data": {
    "hashes": [
      {
        "sum": "string",
        "type": "sha256"
      }
    ],
    "url": "string"
  },
  "code": 0,
  "msg": "string"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `data.url` | string | 下载地址。公网环境下一级域名为 wps.cn 或 kdocs.cn 时需携带登录凭据 |
| `data.hashes` | array | 文件散列值（仅 `with_hash=true` 时返回），公网可能返回 md5/sha1/sha256 中的一个或多个 |
| `data.hashes[].sum` | string | 哈希结果 |
| `data.hashes[].type` | string | 哈希类型：`sha256` / `md5` / `sha1` / `s2s` |

---

## 三、管文档

### 7. move_file

#### 功能说明

批量移动文件(夹)。移动操作为异步任务。

#### 调用示例

```json
{
  "drive_id": "string",
  "file_ids": ["string"],
  "dst_drive_id": "string",
  "dst_parent_id": "string"
}
```

#### 参数说明

**路径参数：**

- `drive_id` (string, 必填): 驱动盘 ID

**请求体：**

- `dst_drive_id` (string, 必填): 目标驱动盘 ID
- `dst_parent_id` (string, 必填): 目标文件夹 ID，根目录时为 `"0"`
- `file_ids` (array[string], 必填): 文件 ID 列表

#### 返回值说明

```json
{
  "data": {
    "task_id": "string"
  },
  "code": 0,
  "msg": "string"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `data.task_id` | string | 批量任务 ID |

---

### 8. rename_file

#### 功能说明

重命名文件（夹）。

#### 调用示例

```json
{
  "drive_id": "string",
  "file_id": "string",
  "dst_name": "2024年Q1销售总结.otl"
}
```

#### 参数说明

**路径参数：**

- `drive_id` (string, 必填): 驱动盘 ID
- `file_id` (string, 必填): 文件（夹）ID

**请求体：**

- `dst_name` (string, 必填): 新文件名，须带上后缀。例: `abc.txt`。支持格式：otl, doc, xls, ppt, wdoc, wxls, wppt, h5, pom, pof, docx, xlsx

#### 返回值说明

返回通用文件信息结构，详见附录 A。

---

### 9. share_file

#### 功能说明

开启文件分享。

#### 调用示例

```json
{
  "drive_id": "string",
  "file_id": "string",
  "role_id": "string",
  "scope": "anyone",
  "opts": {
    "allow_perm_apply": true,
    "check_code": "string",
    "close_after_expire": true,
    "expire_period": 0,
    "expire_time": 0
  }
}
```

#### 参数说明

**路径参数：**

- `drive_id` (string, 必填): 驱动盘 ID
- `file_id` (string, 必填): 文件 ID

**请求体：**

- `role_id` (string, 必填): 权限角色
- `scope` (string, 必填): 链接权限范围。可选值：`anyone`（所有人，仅公网支持）/ `company`（仅企业）/ `users`（指定用户）。`login_users` 仅私有化支持
- `opts` (object, 可选): 链接选项
  - `allow_perm_apply` (boolean, 可选): 允许申请权限
  - `check_code` (string, 可选): 访问密码
  - `close_after_expire` (boolean, 可选): 过期后取消分享链接
  - `expire_period` (integer, 可选): 过期时长。可选值：`0` / `7` / `30`
  - `expire_time` (integer, 可选): 过期时间点

#### 返回值说明

```json
{
  "data": {
    "created_by": {
      "avatar": "string",
      "company_id": "string",
      "id": "string",
      "name": "string",
      "type": "user"
    },
    "ctime": 0,
    "drive_id": "string",
    "file_id": "string",
    "id": "string",
    "mtime": 0,
    "opts": {
      "allow_perm_apply": true,
      "check_code": "string",
      "close_after_expire": true,
      "expire_period": 0,
      "expire_time": 0
    },
    "role_id": "string",
    "scope": "anyone",
    "status": "open",
    "url": "string"
  },
  "code": 0,
  "msg": "string"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `data.id` | string | 分享链接 ID（修改分享属性时使用） |
| `data.url` | string | 分享访问 URL |
| `data.status` | string | 链接状态：`open` / `closed` / `expired` |
| `data.drive_id` | string | 盘 ID |
| `data.file_id` | string | 文件 ID |
| `data.role_id` | string | 权限角色 ID |
| `data.scope` | string | 权限范围：`anyone` / `company` / `users` |
| `data.ctime` | integer | 创建时间 |
| `data.mtime` | integer | 修改时间 |
| `data.opts` | object | 链接设置 |
| `data.created_by` | object | 创建者信息 |

---

### 10. set_share_permission

#### 功能说明

修改分享链接属性。

#### 调用示例

```json
{
  "link_id": "string",
  "role_id": "string",
  "scope": "anyone",
  "opts": {
    "allow_perm_apply": true,
    "check_code": "string",
    "close_after_expire": true,
    "expire_period": 0,
    "expire_time": 0
  }
}
```

#### 参数说明

**路径参数：**

- `link_id` (string, 必填): 分享链接 ID（由 `share_file` 返回的 `data.id`）

**请求体：**

- `role_id` (string, 可选): 权限角色
- `scope` (string, 可选): 链接权限范围。可选值：`anyone`（仅公网支持）/ `company` / `users`。`login_users` 仅私有化支持
- `opts` (object, 可选): 链接设置
  - `allow_perm_apply` (boolean, 可选): 允许申请权限
  - `check_code` (string, 可选): 访问密码
  - `close_after_expire` (boolean, 可选): 过期后取消分享链接
  - `expire_period` (integer, 可选): 过期时长。可选值：`0` / `7` / `30`
  - `expire_time` (integer, 可选): 过期时间点

#### 返回值说明

```json
{
  "code": 0,
  "msg": "string"
}
```

---

### 11. cancel_share

#### 功能说明

取消文件分享。

#### 调用示例

```json
{
  "drive_id": "string",
  "file_id": "string",
  "mode": "pause"
}
```

#### 参数说明

**路径参数：**

- `drive_id` (string, 必填): 驱动盘 ID
- `file_id` (string, 必填): 文件 ID

**请求体：**

- `mode` (string, 可选): 取消分享模式，默认 `pause`。可选值：`pause`（暂停分享）/ `delete`（删除分享）

#### 返回值说明

```json
{
  "code": 0,
  "msg": "string"
}
```

---

### 12. get_share_info

#### 功能说明

获取分享链接信息。通过 `link_id` 查询分享链接的详细属性。

#### 调用示例

```json
{
  "link_id": "string"
}
```

#### 参数说明

**路径参数：**

- `link_id` (string, 必填): 分享链接 ID（由 `share_file` 返回的 `data.id`）

#### 返回值说明

```json
{
  "data": {
    "created_by": {
      "avatar": "string",
      "company_id": "string",
      "id": "string",
      "name": "string",
      "type": "user"
    },
    "ctime": 0,
    "drive_id": "string",
    "file_id": "string",
    "id": "string",
    "mtime": 0,
    "opts": {
      "allow_perm_apply": true,
      "check_code": "string",
      "close_after_expire": true,
      "expire_period": 0,
      "expire_time": 0
    },
    "role_id": "string",
    "scope": "anyone",
    "status": "open",
    "url": "string"
  },
  "code": 0,
  "msg": "string"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `data.id` | string | 分享链接 ID |
| `data.url` | string | 分享访问 URL |
| `data.status` | string | 链接状态：`open` / `closed` / `expired` |
| `data.drive_id` | string | 驱动盘 ID |
| `data.file_id` | string | 文件 ID |
| `data.role_id` | string | 权限角色 ID |
| `data.scope` | string | 权限范围：`anyone` / `company` / `users` |
| `data.ctime` | integer | 创建时间 |
| `data.mtime` | integer | 修改时间 |
| `data.opts` | object | 链接设置 |
| `data.created_by` | object | 创建者信息 |

---

### 13. get_file_info

#### 功能说明

获取文件（夹）信息。通过 `file_id` 获取单个文件或文件夹的详细信息，包含 `drive_id` 等关键字段，可用于获取其他接口所需的 `drive_id`。

#### 调用示例

```json
{
  "file_id": "string",
  "with_permission": true,
  "with_drive": true
}
```

#### 参数说明

**路径参数：**

- `file_id` (string, 必填): 文件（夹）ID

**查询参数：**

- `with_permission` (boolean, 可选): 是否返回文件操作权限
- `with_ext_attrs` (boolean, 可选): 是否返回文件扩展属性
- `with_drive` (boolean, 可选): 是否返回文件所属 drive 信息

#### 返回值说明

```json
{
  "data": {
    "created_by": {
      "avatar": "string",
      "company_id": "string",
      "id": "string",
      "name": "string",
      "type": "user"
    },
    "ctime": 0,
    "drive": {
      "allotee_id": "string",
      "allotee_type": "user",
      "company_id": "string",
      "created_by": {
        "avatar": "string",
        "company_id": "string",
        "id": "string",
        "name": "string",
        "type": "user"
      },
      "ctime": 0,
      "description": "string",
      "ext_attrs": [
        { "name": "string", "value": "string" }
      ],
      "id": "string",
      "mtime": 0,
      "name": "string",
      "quota": {
        "deleted": 0,
        "remaining": 0,
        "total": 0,
        "used": 0
      },
      "source": "string",
      "status": "inuse"
    },
    "drive_id": "string",
    "ext_attrs": [
      { "name": "string", "value": "string" }
    ],
    "id": "string",
    "link_id": "string",
    "link_url": "string",
    "modified_by": {
      "avatar": "string",
      "company_id": "string",
      "id": "string",
      "name": "string",
      "type": "user"
    },
    "mtime": 0,
    "name": "string",
    "parent_id": "string",
    "permission": {
      "comment": true,
      "copy": true,
      "copy_content": true,
      "delete": true,
      "download": true,
      "history": true,
      "list": true,
      "move": true,
      "new_empty": true,
      "perm_ctl": true,
      "preview": true,
      "print": true,
      "rename": true,
      "saveas": true,
      "secret": true,
      "share": true,
      "update": true,
      "upload": true
    },
    "shared": true,
    "size": 0,
    "type": "folder",
    "version": 0
  },
  "code": 0,
  "msg": "string"
}
```

返回通用文件信息结构，详见附录 A。当 `with_drive=true` 时额外返回 `drive` 对象（含盘的 id、name、quota 等信息）。

---

## 四、用文档

### 14. read_file_content

#### 功能说明

文档内容抽取。支持将文档内容抽取为 markdown、纯文本或 KDC 结构化格式。支持同步和异步模式。

#### 调用示例

```json
{
  "drive_id": "string",
  "file_id": "string",
  "format": "markdown",
  "include_elements": ["para", "table"],
  "mode": "sync",
  "task_id": "string"
}
```

#### 参数说明

**路径参数：**

- `drive_id` (string, 必填): 驱动盘 ID
- `file_id` (string, 必填): 文件 ID

**查询参数：**

- `format` (string, 可选): 文档内容目标格式。可选值：`kdc`（结构化表示）/ `plain`（纯文本）/ `markdown`
- `include_elements` (array, 可选): 指定抽取元素。默认元素为 `para`（段落），且一定会被导出；其余附加元素根据参数选择性导出。可选值：`para` / `table` / `component` / `textbox` / `all`
- `mode` (string, 可选): 接口模式。可选值：`sync`（同步模式，默认）/ `async`（异步模式）/ `auto`（自动模式，耗时较长的任务自动转为异步）
- `task_id` (string, 可选): 异步任务 ID，用于内容抽取任务的结果查询，在 `mode=async/auto` 时生效

#### 返回值说明

```json
{
  "data": {
    "task_id": "string",
    "task_status": "success",
    "dst_format": "markdown",
    "markdown": "string",
    "plain": "string",
    "src_format": "otl",
    "version": "string",
    "doc": {}
  },
  "code": 0,
  "msg": "string"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `data.task_id` | string | 任务 ID，在 `mode=async/auto` 时返回 |
| `data.task_status` | string | 任务状态，在 `mode=async/auto` 时返回。可选值：`success` / `running` / `failed` |
| `data.dst_format` | string | 目标格式：`kdc` / `plain` / `markdown` |
| `data.markdown` | string | markdown 内容数据，目标格式为 `markdown` 时适用 |
| `data.plain` | string | 纯文本内容数据，目标格式为 `plain` 时适用 |
| `data.doc` | object | 文字类的结构化数据，源格式为 otl/pdf/docx 且目标格式为 `kdc` 时适用 |
| `data.src_format` | string | 源格式（otl, docx, pdf, xlsx 等） |
| `data.version` | string | 版本号 |

---

---
TODO: 内部备注：发布前删除。
下方convert_file_content接口，是根据当前已实现的skill中迁移过来的。
与 `upload_file`（传 `file_id` 更新）功能互补。convert_file_content 负责格式转换，upload_file 负责上传写入。
使用模板的场景：当生成文字组件时，因没有内置模板设置标题n的样式，直接导入md的效果非常丑
---

### 15. convert_file_content

#### 功能说明

将文件从一种格式转换为另一种格式（如 Markdown → DOCX、Markdown → PDF）。可选提供模板文件以控制转换后的排版样式。


#### 调用示例

```json
{
  "form_file": "<源文件内容>",
  "filename": "report.md",
  "target": "docx"
}
```

带模板转换：

```json
{
  "form_file": "<源文件内容>",
  "filename": "report.md",
  "target": "docx",
  "template_file.form_file": "<模板文件内容>",
  "template_file.filename": "template.docx"
}
```

#### 参数说明

- `form_file` (file, 必填): 源文件内容
- `filename` (string, 必填): 源文件名，需带后缀。例: `report.md`
- `target` (string, 必填): 目标格式。可选值：`docx` / `pdf` / `pptx`
- `template_file.form_file` (file, 可选): 模板文件内容（如 `.docx` 模板），仅在 `target=docx` 时有效
- `template_file.filename` (string, 可选): 模板文件名（提供模板文件时必填）

#### 返回值说明

```json
{
  "code": 0,
  "data": "<转换后的文件内容>"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `code` | integer | 状态码，0 表示成功 |
| `data` | binary | 转换后的文件内容（成功时返回） |
| `msg` | string | 错误信息（失败时返回） |

---

### 16. search_files

#### 功能说明

文件（夹）搜索。

#### 调用示例

```json
{
  "keyword": "区域周报告",
  "type": "all",
  "file_type": "file",
  "parent_ids": ["string"],
  "page_size": 20,
  "order": "desc",
  "order_by": "mtime",
  "with_total": true
}
```

#### 参数说明

**查询参数：**

- `keyword` (string, 可选): 搜索关键字
- `type` (string, 必填): 搜索类型。可选值：`file_name` / `content` / `all`
- `page_size` (integer, 必填): 分页大小，公网限制最大为 500
- `page_token` (string, 可选): 翻页 token
- `file_type` (string, 可选): 文件类型，不传默认全搜。可选值：`folder` / `file` / `shortcut`
- `file_exts` (array, 可选): 文件后缀
- `drive_ids` (array, 可选): 搜索盘列表
- `parent_ids` (array, 可选): 搜索目录列表
- `creator_ids` (array, 可选): 创建者 ID，公网只支持选择是否自己创建的文件
- `modifier_ids` (array, 可选): 编辑者 ID
- `sharer_ids` (array, 可选): 分享者 ID
- `receiver_ids` (array, 可选): 接收者 ID
- `time_type` (string, 可选): 时间范围。可选值：`ctime` / `mtime` / `otime` / `stime`
- `start_time` (integer, 可选): 最小时间
- `end_time` (integer, 可选): 最大时间
- `with_permission` (boolean, 可选): 是否返回文件操作权限
- `with_link` (boolean, 可选): 是否返回文件分享信息
- `with_total` (boolean, 可选): 是否返回搜索到的总条数
- `with_drive` (boolean, 可选): 是否返回驱动盘
- `order` (string, 可选): 排序方式。可选值：`desc` / `asc`
- `order_by` (string, 可选): 排序字段。可选值：`ctime` / `mtime`
- `scope` (array, 可选): 搜索范围。可选值：`all` / `share_by_me` / `share_to_me` / `latest` / `personal_drive` / `group_drive` / `recycle` / `customize` / `latest_opened` / `latest_edited`
- `channels` (array, 可选): 渠道信息
- `device_ids` (array, 可选): 设备 ID
- `exclude_channels` (array, 可选): 排除渠道信息
- `exclude_file_exts` (array, 可选): 排除文件后缀
- `filter_user_id` (integer, 可选): 创建者分享者过滤
- `file_ext_groups` (array, 可选): 文件分组后缀
- `search_operator_name` (boolean, 可选): 搜索文件的创建者或文件分享者

#### 返回值说明

```json
{
  "data": {
    "items": [
      {
        "file": {
          "created_by": {
            "avatar": "string",
            "company_id": "string",
            "id": "string",
            "name": "string",
            "type": "user"
          },
          "ctime": 0,
          "drive_id": "string",
          "ext_attrs": [
            { "name": "string", "value": "string" }
          ],
          "id": "string",
          "link_id": "string",
          "link_url": "string",
          "modified_by": {
            "avatar": "string",
            "company_id": "string",
            "id": "string",
            "name": "string",
            "type": "user"
          },
          "mtime": 0,
          "name": "string",
          "parent_id": "string",
          "shared": true,
          "size": 0,
          "type": "folder",
          "version": 0
        },
        "file_src": {
          "name": "string",
          "path": "string",
          "type": "link"
        },
        "highlights": {
          "example_key": ["string"]
        },
        "otime": 0
      }
    ],
    "next_page_token": "string",
    "total": 0
  },
  "code": 0,
  "msg": "string"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `data.items` | array | 搜索结果列表 |
| `data.items[].file` | object | 文件信息，通用文件信息结构（附录 A） |
| `data.items[].file_src` | object | 文件位置信息 |
| `data.items[].file_src.name` | string | 来源名称 |
| `data.items[].file_src.path` | string | 文件路径 |
| `data.items[].file_src.type` | string | 来源类型：`link` / `user_private` / `user_roaming` / `group_normal` / `group_dept` / `group_whole` |
| `data.items[].highlights` | map[string][]string | 匹配关键字 |
| `data.items[].otime` | integer | 文件打开时间 |
| `data.next_page_token` | string | 下一页 token |
| `data.total` | integer | 资源集合总数（仅 `with_total=true` 时返回） |

---

### 17. get_file_link

#### 功能说明

获取文件的云文档在线访问链接。


#### 调用示例

```json
{
  "file_id": "string"
}
```

#### 参数说明

- `file_id` (string, 必填): 文件 ID

#### 返回值说明

```json
{
  "file_id": "string",
  "file_url": "https://kdocs.cn/l/xxxxx",
  "name": "Q1销售报告"
}
```

---

## 附录

### A. 通用文件信息结构

`create_file`、`upload_file`（步骤三）、`rename_file`、`list_files` 等接口返回的文件信息共用以下结构：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | 文件 ID |
| `name` | string | 文件名 |
| `type` | string | 文件类型：`file` / `folder` / `shortcut` |
| `size` | integer | 文件大小 |
| `parent_id` | string | 父目录 ID |
| `drive_id` | string | 驱动盘 ID |
| `version` | integer | 文件版本 |
| `ctime` | integer | 文件创建时间（时间戳，秒） |
| `mtime` | integer | 文件修改时间（时间戳，秒） |
| `shared` | boolean | 是否开启分享（link.status = 'open' 时为 true） |
| `link_id` | string | 分享 ID |
| `link_url` | string | 分享链接 URL |
| `created_by` | object | 文件创建者信息 |
| `created_by.id` | string | 身份 ID |
| `created_by.name` | string | 用户或应用的名称 |
| `created_by.type` | string | 身份类型：`user` / `sp` / `unknown` |
| `created_by.avatar` | string | 头像 |
| `created_by.company_id` | string | 身份所归属的公司 |
| `modified_by` | object | 文件修改者信息（结构同 created_by） |
| `ext_attrs` | array[object] | 文件扩展属性（`with_ext_attrs=true` 时返回），每项含 name 和 value |
| `drive` | object | 文件驱动盘信息（`with_drive=true` 时返回） |

**`permission` 对象**（`with_permission=true` 时返回）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `comment` | boolean | 评论 |
| `copy` | boolean | 复制 |
| `copy_content` | boolean | 内容复制 |
| `delete` | boolean | 文件删除 |
| `download` | boolean | 下载 |
| `history` | boolean | 历史版本（仅公网支持） |
| `list` | boolean | 列表 |
| `move` | boolean | 文件移动 |
| `new_empty` | boolean | 新建 |
| `perm_ctl` | boolean | 权限管理 |
| `preview` | boolean | 预览 |
| `print` | boolean | 打印 |
| `rename` | boolean | 文件重命名 |
| `saveas` | boolean | 另存为（仅公网支持） |
| `secret` | boolean | 安全文档（仅公网支持） |
| `share` | boolean | 分享 |
| `update` | boolean | 编辑/更新 |
| `upload` | boolean | 上传：手动上传新版本 |

### B. 工具速查表

| # | 工具名 | 分类 | 功能 | 必填参数 |
|---|--------|------|------|----------|
| 1 | `create_file` | 写文档 | 新建文件/文件夹（`file_type` 区分） | `drive_id`, `parent_id`, `name`, `file_type` |
| 2 | `scrape_url` | 写文档 | 网页剪藏 | `url` |
| 3 | `scrape_progress` | 写文档 | 剪藏进度查询 | `task_id` |
| 4 | `upload_file` | 写文档 | 上传/更新文件（三步流程，传 `file_id` 为更新） | `drive_id`, `parent_id`, `size` |
| 5 | `list_files` | 读文档 | 获取子文件列表（含原 list_folder 功能） | `drive_id`, `parent_id`, `page_size` |
| 6 | `download_file` | 读文档 | 获取文件下载信息 | `drive_id`, `file_id` |
| 7 | `move_file` | 管文档 | 批量移动文件(夹) | `drive_id`, `file_ids`, `dst_drive_id`, `dst_parent_id` |
| 8 | `rename_file` | 管文档 | 重命名文件(夹) | `drive_id`, `file_id`, `dst_name` |
| 9 | `share_file` | 管文档 | 开启文件分享 | `drive_id`, `file_id`, `role_id`, `scope` |
| 10 | `set_share_permission` | 管文档 | 修改分享链接属性 | `link_id` |
| 11 | `cancel_share` | 管文档 | 取消文件分享 | `drive_id`, `file_id` |
| 12 | `get_share_info` | 管文档 | 获取分享链接信息 | `link_id` |
| 13 | `get_file_info` | 读文档 | 获取文件（夹）信息 | `file_id` |
| 14 | `read_file_content` | 用文档 | 文档内容抽取 | `drive_id`, `file_id` |
| 15 | `convert_file_content` | 用文档 | 文件格式转换 | `form_file`, `filename`, `target` |
| 16 | `search_files` | 用文档 | 文件(夹)搜索 | `type`, `page_size` |
| 17 | `get_file_link` | 用文档 | 获取文档链接 | `file_id` |
