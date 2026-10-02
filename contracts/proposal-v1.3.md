# 契约变更提案 v1.3：Phase 3 LSP 智能增强（4 工具）

> 依据：owner 2026-09-22 指令「Phase 3（LSP 智能增强）拆解入队,执行」= 授权本 delta（执行令即批准，落盘时引用本条）。
> 方案选择（corrections C9 比选）：**Option A 薄适配器**——按 Windows 支持/生命周期复杂度/project_id 路由保持/无 stateful 耦合四项标准，优于 sidecar（B）与整套借鉴（C）。禁止自建 Symbol DB/AST DB（v3.1 §4.2 红线不变）；调用现有 LSP servers 属 C6 B 类复用。

## 新增工具段（4 个，全部 readOnlyHint=true / openWorldHint=false / project_id 必填）

| 工具 | 输入 | 输出 | 特有错误 |
|---|---|---|---|
| `find_definition` | `{project_id, path, line(1-based), character(1-based)}` | `{locations:[{path,line,character}]}` | LSP_UNAVAILABLE |
| `find_references` | `{project_id, path, line, character, include_declaration=false}` | `{locations:[...], truncated?}`（max 100） | LSP_UNAVAILABLE |
| `workspace_symbols` | `{project_id, query}` | `{symbols:[{name,kind,path,line,character}], truncated?}`（max 100） | LSP_UNAVAILABLE |
| `get_diagnostics` | `{project_id, path?}` | `{diagnostics:[{path,severity,line,message,source}], truncated?}`（max 100） | LSP_UNAVAILABLE |

**新错误码（1 个）**：`LSP_UNAVAILABLE`（recoverable=false）——项目语言服务器未安装或启动失败；hint：「安装对应 language server（如 pyright-langserver / typescript-language-server）或改用 search_code」。**禁止自动安装**（纪律不变）。

**行号口径**：对外统一 **1-based**（与 read_file 一致），适配层内部转 LSP 0-based。

**configSchema 扩展**（可选段）：`intel.servers: {<语言>: {command: [argv]}}` 覆盖自动探测；自动探测规则：pyproject.toml|requirements.txt→pyright-langserver、package.json→typescript-language-server(npx)、go.mod→gopls、Cargo.toml→rust-analyzer；均未命中/未安装 → LSP_UNAVAILABLE。

**降级保证**：LSP 不可用不报 500、不影响 7 个既有工具；hint 指引 fallback to search_code。

## 受影响重验面
新增 4 工具全套测试（mock LSP server 确定性测试 + 真实服务器检测 skip 语义）、契约一致性、全量回归。既有 7 工具契约逐字不动。
