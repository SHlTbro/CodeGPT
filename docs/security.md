# 安全模型

> 本文回答一个问题：当云端 AI 通过 Local Codebase MCP 访问你的电脑时，什么约束着它能看什么、能改什么。每一条约束都标注了源码实现位置，安全声明全部可回源核对。

## 立场

默认最小暴露，逐环可拒绝，写入默认全关，闸门全部在服务端。模型是调用方，不是被信任方：它能做的事完全由服务器决定，不依赖模型自觉。安全基座是应用层路径约束（application-level path confinement），不是 OS 级沙箱。

## 静态基座（只读面）

| 约束 | 行为 | 实现位置 |
|---|---|---|
| 回环绑定 | `server.host` 仅允许 `127.0.0.1` / `localhost` / `::1`，其他值（含 `0.0.0.0`）配置校验直接拒绝；服务显式绑定 host，不落默认 | `config.py` 回环白名单与校验；`server.py` 绑定段 |
| 注册表白名单 | 只有 `config.yaml` 登记的项目存在，项目 id 重复/路径不存在启动即失败（fail-fast） | `config.py` 校验；`projects.py` |
| 路径守卫（PathGuard） | `~`、UNC、盘符、绝对路径一律拒绝；symlink/junction 解析后做 casefold 前缀包含判定，`..` 逃逸由最终包含判定兜底 | `security.py` `resolve_path` |
| 敏感文件过滤 | `.env`、`*.key`、`*.pem`、`credentials*`、`.ssh/**`、`.aws/**` 默认不可见；无 `/` 的模式同时匹配 basename（子目录下的 `.env` 同样拦下）；list/search/read/write 四路共享同一可见性判定 | `security.py` `is_sensitive` 与默认模式表 |
| 排除目录 | `.git`、`node_modules`、`dist`、`build`、`__pycache__` 永不出现 | `security.py` 默认排除目录表 |
| 返回预算 | 单文件 5MB、单次返回 50000 字符截断、搜索 100 条、目录 2000 项、搜索单行 500 字符；批量读取双层预算，超限文件以 `BUDGET_EXHAUSTED` 标注 | `security.py` 预算常量与截断函数；`tools_services.py` |
| 无 shell | ripgrep、git、语言服务器全部以 argv 数组直传子进程（不经 shell），stdin 接 DEVNULL；静态测试断言生产源码不含 `shell=True` / `os.system` / `os.popen` | `tools_services.py`、`intel.py` 子进程调用；`tests/security/test_no_shell.py` |
| 日志纪律 | 日志只记时间戳、工具名、项目 id、耗时、结果码，禁止记录文件内容与查询内容 | `server.py` 日志纪律声明；`writer.py` 审计行实例 |

## 受控写：10 环安全链

写工具默认对全部项目关闭。逐项目 `write_enabled: true` 开启后，每次写入按固定顺序过以下十环，任何一环不满足即返回结构化错误，顺序不可变：

| 环 | 闸门 | 拒绝时返回 |
|---|---|---|
| 1 | 项目必须在注册表中 | `INVALID_ARGUMENT` |
| 2 | 该项目 `write_enabled: true` | `WRITE_DISABLED` |
| 3-4 | 路径解析：绝对/盘符/UNC/`~` 拒绝，symlink/junction 解析后必须仍在项目根内 | `PATH_OUTSIDE_WORKSPACE` |
| 5 | 目标相对路径必须命中项目 `write_roots` 白名单（默认 `["docs"]`，仅允许干净相对目录名） | `WRITE_PATH_FORBIDDEN` |
| 6 | 敏感过滤同样约束写（`docs/.env` 也写不进） | `FILE_NOT_VISIBLE` |
| 7 | 冲突检查：新建须 `expected_absent: true`；更新须携带读时返回的 `content_sha256` | `FILE_ALREADY_EXISTS` / `REVISION_CONFLICT` |
| 8 | 长度预算：文本 1MB，二进制解码后 10MB，扩展名白名单 7 种 | `PAYLOAD_TOO_LARGE` / `EXTENSION_NOT_ALLOWED` |
| 9 | per-project 锁内预检 + 写入（防 TOCTOU：检查与写入在同一把锁内） | （竞态被串行化） |
| 10 | 原子写：tmp 文件 + `os.replace`，崩溃残留不破坏既有文件；随后落一行审计日志 | （成功路径） |

实现：`writer.py` 写链函数（docstring 记录链序，代码注释逐环编号）与 `config.py` 的 `write_roots` 校验。

三个写工具的差异：`write_file` 写 UTF-8 文本；`write_binary_file` 写 7 种白名单扩展名的二进制（base64 严格解码，哈希凭据来自上次写入返回值）；`apply_workspace_edit` 应用工作区重构计划，不受 `docs/` 白名单限制（重构语义允许改源码），但仍强制路径守卫、敏感过滤与单文件 1MB 预算，且多文件应用全部校验通过才写、任一不匹配整体拒绝零落盘。

## 零静默覆盖

哈希冲突检查的目的：AI 的写入永远不覆盖人在两次读取之间的手工修改。文件被改过，提交的哈希与当前内容不符，写入被拒绝（`REVISION_CONFLICT`），模型需要重新读取拿到新哈希再重试。这条语义对单文件与多文件重构一致。

## 审批语义（写之前谁说了算）

MCP 协议没有审批弹窗，服务器无法在写入前向用户确认。约定的闭环是：模型在对话里展示拟写内容（路径 + 全文），用户确认后才指示其调用写工具；服务端十环闸门负责兜底。每次写入在 `server.log` 留一行审计（项目、相对路径、字节数、前后哈希、created/updated），不含文件内容，可按工具名检索定位。

## 测试覆盖

安全行为有专门测试文件：`tests/security/` 下三个文件覆盖静态无 shell 断言（含生产源码扫描与子进程形态检查）、Windows 路径守卫、写闸行为；单元与集成测试覆盖冲突检查、`write_roots` 校验、配置热重载等。2026-10-01 全新目录实测 326 项全部通过。

## 已知边界

- 应用层约束不是沙箱：进程权限等同运行用户；约束的是「经 MCP 工具面可达的路径」。
- 公网暴露面由部署决定：隧道把回环服务转发到公网，隧道自身的安全（token 卫生、域名管理）见部署文档；服务端只保证「不经隧道不可达、经工具面必过闸」。
- 模型读取的内容会发往所连接的模型服务端；本项目解决授权边界与写入安全，不宣称数据不出网。
