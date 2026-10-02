# 契约变更提案：v1.1（list_files 双视图）+ v1.2（受控写 write_file）

> 依据 changePolicy：Coding Agent 禁止自行改契约；**owner 批准小修，只重验受影响部分**。
> 本文件是两笔 delta 的完整提案，owner 逐段批准后由编排者落盘 contracts/localcodebase-mcp-mvp.json（version 1.2.0, FROZEN）。

## Delta 1（v1.0 → v1.1）：list_files 双视图

**动机**：模型需多次调用递归扁平列表才能拼出项目结构全貌（实测摩擦）。

**变更段**（仅此一段，其余 5 工具与 errorCodes 逐字不动）：

```json
{
  "name": "list_files",
  "description": "列出项目内某目录的内容。默认返回该目录的直接子目录与文件（ls 风格）；传 depth（1-5）时返回限深目录树。默认过滤 .git/node_modules/dist/build/__pycache__ 与敏感文件。",
  "input": {
    "type": "object",
    "properties": {
      "project_id": { "type": "string" },
      "path": { "type": "string", "default": "." },
      "depth": { "type": "integer", "minimum": 0, "maximum": 5, "default": 0,
                 "description": "0=直接子项分组；≥1=限深树形" }
    },
    "required": ["project_id"]
  },
  "output": {
    "depth=0": { "dirs": ["直接子目录名"], "files": ["直接文件名"] },
    "depth≥1": { "tree": { "name": "str", "dirs": ["(嵌套同构,至 depth 层)"], "files": ["str"] } }
  },
  "responseFields": { "project_id": true, "truncated": "全树条目总数超 2000 时 true" },
  "errors": ["PROJECT_NOT_FOUND", "INVALID_ARGUMENT", "PATH_OUTSIDE_WORKSPACE", "FILE_NOT_VISIBLE", "SEARCH_BACKEND_ERROR"]
}
```

**受影响重验面**：list_files 全部测试迁移 + 6 工具契约一致性测试；其余工具回归绿即证未受波及。

## Delta 2（v1.1 → v1.2）：受控写 write_file（Phase 4 提前，owner 2026-09-21 裁决）

**动机**：owner 核心诉求「AI 审查后直接编写/更新项目文档」；ChatGPT 已实测连通，写能力是下一个真实价值点。

**新增工具段**：

```json
{
  "name": "write_file",
  "description": "创建或更新项目 docs/ 目录下的 UTF-8 文本文件（文档维护专用）。新建文件必须 expected_absent=true；更新已有文件必须传入最近一次 read_file 返回的 content_sha256（防覆盖他人/用户新改动）。禁止写 docs/ 之外路径。",
  "annotations": { "readOnlyHint": false, "destructiveHint": true, "openWorldHint": false },
  "input": {
    "type": "object",
    "properties": {
      "project_id": { "type": "string" },
      "path": { "type": "string", "description": "docs/ 下相对路径，如 docs/design/xx.md" },
      "content": { "type": "string", "maxBytes": 1048576 },
      "expected_absent": { "type": "boolean", "description": "新建时必须为 true" },
      "content_sha256": { "type": "string", "description": "更新已有文件时必填=写入前最近一次 read_file 返回值" }
    },
    "required": ["project_id", "path", "content"]
  },
  "output": {
    "path": "string(final relative path)",
    "bytes_written": "int",
    "sha256": "string(写入后全文 hash)",
    "created": "boolean",
    "write_seq": "int(项目级写序号，自 1 递增，供模型检测并发写冲突)"
  },
  "responseFields": { "project_id": true, "truncated": false },
  "errors": ["PROJECT_NOT_FOUND", "INVALID_ARGUMENT", "PATH_OUTSIDE_WORKSPACE", "FILE_NOT_VISIBLE", "WRITE_PATH_FORBIDDEN(新)", "REVISION_CONFLICT(新)", "FILE_ALREADY_EXISTS(新)", "WRITE_DISABLED(新)", "PAYLOAD_TOO_LARGE(新)"]
}
```

**新增错误码（4 个，写入 errorCodes）**：

| code | meaning | recoverable | modelHandling |
|---|---|---|---|
| WRITE_PATH_FORBIDDEN | 目标不在 docs/** 白名单（基于 final resolved path 判定） | false | 放弃该写操作；可建议用户手动移动 |
| REVISION_CONFLICT | 文件在 read 之后被修改（sha256 不匹配） | true | 重新 read_file 获取新 sha256 后重试 |
| FILE_ALREADY_EXISTS | 新建但文件已存在（expected_absent=true 而文件在） | true | 改为更新流程：read → 带 sha256 重写 |
| WRITE_DISABLED | 项目配置 write_enabled=false（默认 false） | false | 告知用户在 config 中开启 |
| PAYLOAD_TOO_LARGE | content 超 1MB | false | 缩小或拆分文档 |

**配套契约改动**：
- read_file 输出**恢复 `content_sha256` 字段**（v1.0 曾有、owner 曾裁删；写能力下它是更新冲突检测的必要条件——这是本次唯一触碰其他工具的点，特此显式报批）。
- configSchema 增 `projects[].write_enabled: bool(默认 false)` 与 `security.write_paths: ["docs/**"](默认，仅此一值)`。

**安全链（实现必须按此顺序，缺一不可）**：
```text
project_id → registry → write_enabled? → root.resolve() → normalize path
→ resolve symlink/junction → final path ∈ root（casefold）
→ final relative path ∈ docs/**（非 fnmatch 输入串）→ 非 hard-deny/敏感（.env 等写不进）
→ 冲突检查（expected_absent / content_sha256）→ per-project asyncio.Lock → 原子写（tmp+rename）
→ 审计（server.log: path/bytes/sha 前后/结果，无内容）
```
必测逃逸：`docs/../src/x.py`、`docs/link→外`、junction 逃逸、`src/docs/x.py`（项目根下非顶层 docs 不在白名单——**白名单锚定 root/docs**）、大小写 `DOCS/x.md` 视 OS 语义（Windows 不区分，白名单按 casefold 匹配）。

**并发写语义（多会话冲突消解，owner 要求的 approval 等效机制）**：
- ChatGPT 多对话并发写同一文件：sha256 预检 + write_seq 单调递增 + REVISION_CONFLICT 强制重读——后写者必须基于最新内容，不会静默覆盖。
- 用户侧 final approval：MCP 不做人工审批 UI（v3.1 边界不变）；approval 由 ChatGPT 客户端自然承担（模型展示拟写内容→用户看到→用户指示继续）。服务端闸门是**范围（docs/**）+ 新鲜度（sha256）**。

**受影响重验面**：新工具全套测试 + read_file 契约一致性 + 安全逃逸矩阵（真实 junction）+ e2e（开 write_enabled 的项目实写 docs）。

## 不变声明

- 工具总数 6→7（write_file 为 owner 裁决新增，突破「别再加」冻结——已获本次裁决授权）。
- list_projects/project_info/list_files(除 delta1)/search_code/git_info 契约逐字不动。
- 只读工具行为零变化；write 默认全项目关闭（write_enabled=false 需逐项目显式开启）。
