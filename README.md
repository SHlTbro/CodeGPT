<p align="center">
  <img src="./assets/github-social-preview.png" width="100%" alt="Local Codebase MCP + Agent + Code Intelligence：让 ChatGPT 等 AI Agent 检索、阅读、搜索本地多项目代码，代码留在本机，写入逐环受控">
</p>

<h1 align="center">Local Codebase MCP</h1>

<p align="center">运行在你自己电脑上的 MCP 服务器：ChatGPT 等云端 AI 通过它检索、阅读、搜索你本机的多个代码项目，代码文件不上传；可选开启受控写，写入逐环过闸。</p>

## 它是什么，解决什么问题

让云端模型理解本地代码，常见的做法是打包上传。上传的 zip 是某个时刻的快照，项目继续改动，模型的理解就停在旧版本；node_modules、构建产物这些噪音还会占掉上下文；代码交出去之后，每改一次就得重传一次。

Local Codebase MCP 在你的电脑上跑一个 MCP 服务器，把「上传快照」换成「按需读取」：模型经 MCP 协议读此刻磁盘上的真实文件与 git 状态，问到哪就读到哪。模型能看到什么，由一张项目注册表决定，没登记的项目对它不存在。核心理念一句话：模型负责思考，MCP 只提供事实，用户掌握决策。

| 你得到什么 | 靠什么实现 |
|---|---|
| 回答基于当下事实，项目改完下一问就是新状态 | 实时读取本机文件与 git 状态，无快照 |
| 一次对话建立整个项目的架构认知 | 批量读取（glob 通配 + 双层字符预算）、ripgrep 全文搜索、按行区间深读 |
| 跳转定义、查引用、看编译诊断 | 按项目标记文件自动探测语言服务器（Python/TS/Go/Rust），未安装自动降级全文搜索 |
| 让 AI 直接整理文档、落地生成的文件 | 三个受控写工具（默认全项目关闭）：文档写入、二进制文档写入、工作区重构 |
| 代码不离开你的电脑 | 服务器只监听本机回环地址，公网可达性只由你部署的隧道提供 |

## 工具面（15 个 = 12 只读 + 3 受控写）

| 类别 | 工具 |
|---|---|
| 发现 | `list_projects` `project_info` |
| 浏览读取 | `list_files` `read_file` `read_batch` |
| 搜索 | `search_code`（ripgrep） |
| 版本 | `git_info`（非 git 项目降级，不报错） |
| 智能（LSP，可选） | `find_definition` `find_references` `workspace_symbols` `get_diagnostics` `rename_preview` |
| 受控写（默认全关） | `write_file` `write_binary_file` `apply_workspace_edit` |

写能力默认对全部项目关闭，逐项目 `write_enabled: true` 开启；可写范围由项目级 `write_roots` 白名单约束（默认只有 `docs/`）。更新必须携带读取时返回的全文哈希，文件被别人改过就拒绝写入，不会静默覆盖。

## 能力与验证

下图每个数字都回源实测：工具数数自 `tools.py` 的工具注册段，模块行数来自逐文件统计，测试数为 2026-10-01 在全新目录实测的结果。

<p align="center">
  <img src="./assets/capability-verification.svg" width="720" alt="能力面与验证基线：15 个 MCP 工具、9 个生产模块、25 个测试文件、326 项测试通过">
</p>

## 快速开始

前置：Python 3.12+；`search_code` 依赖 ripgrep（缺失时启动自检会提示安装方式）；git 可选。

一键（Windows）：进入 `local-codebase-mcp/` 双击 `deploy.bat`。脚本自动装依赖（下载失败自动切镜像）、生成 `config.yaml` 模板、探测空闲端口、启动服务与公网隧道，并打印可粘进 ChatGPT 的 `/mcp` 地址。

手动（全平台，以下命令已在全新目录实测走通）：

```bash
cd local-codebase-mcp
pip install -r requirements.txt        # 仅 fastmcp、pyyaml 两条
cp config.example.yaml config.yaml     # 把 path 改成你本机真实项目目录
python server.py --check               # 只校验配置与端口占用，不启动；全部通过 exit 0
python server.py                       # 启动：N 个项目已登记，监听 http://127.0.0.1:8000/mcp
```

把隧道地址（`https://xxxx/mcp`）粘进 ChatGPT 设置的连接器表单（服务器 URL，无身份验证），对话里问「我有哪些项目？」即验证成功。纯本机场景不需要隧道：用 MCP Inspector 直连 `http://127.0.0.1:8000/mcp` 即可。

配置示例（完整模板见 [`local-codebase-mcp/config.example.yaml`](local-codebase-mcp/config.example.yaml)）：

```yaml
projects:
  - id: my-project
    name: My Project
    path: /absolute/path/to/my-project # 本机绝对路径；想让 AI 写文档再加 write_enabled: true
server:
  host: 127.0.0.1                      # 仅允许回环地址，0.0.0.0 会被校验拒绝
  port: 8000
  path: /mcp
```

运行中修改配置文件 ≤5 秒热重载：新增/删除项目、开关写入都不用重启。部署形态（固定地址的 named 隧道、ngrok、排障表）见 [docs/deployment.md](docs/deployment.md)。

## 架构与流程

系统上下文与容器两层架构图、逐节点源码锚点与「不存在的边」清单见 [docs/architecture.md](docs/architecture.md)。三条真实使用链路（部署接入、首轮取证调用序列、受控写冲突回路）见 [docs/workflow.md](docs/workflow.md)。

## 安全模型

服务器的安全立场是：默认最小暴露、逐环可拒绝、写入默认全关、闸门全部在服务端。完整的安全模型文档（每一环的威胁、闸门与源码实现）单独成文：[docs/security.md](docs/security.md)。要点：

- 回环绑定：只监听 `127.0.0.1` 等回环地址，配置校验直接拒绝其他绑定。
- 注册表白名单：只有登记过的项目存在；路径逃逸（绝对路径/盘符/UNC/`~`/`..`/symlink）一律拒绝。
- 敏感文件读写同闸：`.env`、`*.key`、`*.pem`、`credentials*`、`.ssh/**`、`.aws/**` 默认不可见。
- 受控写 10 环安全链：注册表 → 写开关 → 路径解析 → 可写白名单 → 敏感过滤 → 哈希冲突检查 → 长度预算 → 锁内预检 + 原子写 → 审计一行，顺序不可变。
- 无 shell：ripgrep/git/语言服务器全部 argv 数组直传，并有静态测试断言兜底；日志只记调用 metadata，不记文件内容与查询内容。

## 项目边界（如实声明）

- 不做命令执行、Agent 规划、代码索引数据库，它只是 AI 理解真实代码的入口。
- 应用层约束不是 OS 沙箱：安全模型约束「经 MCP 工具面可达的路径」。
- 模型读取的内容会发送到所连接的模型服务端；本项目解决授权边界与写入安全。
- 无性能基准数据；欢迎实测反馈。

## 成熟度

- 9 个生产模块共 2321 行 Python；声明依赖仅 `fastmcp` 与 `pyyaml` 两条。
- 25 个测试文件（单元/集成/安全/性能）；2026-10-01 在全新目录实测 326 项全部通过（pytest 9.1.1，不含需真实语言服务器的 6 个用例）。
- 已有从零到 ChatGPT 连接成功的一线部署记录；一键部署脚本内置镜像回退与空闲端口探测。
- 定位为 MVP：接口与错误码以包内 `contracts/` 冻结契约为准。

## 文档导航

| 主题 | 内容 | 链接 |
|---|---|---|
| 一页介绍 | 给第一次看到本项目的人 | [docs/intro.md](docs/intro.md) |
| 架构 | 系统上下文 + 容器两层图、源码锚点、禁边表 | [docs/architecture.md](docs/architecture.md) |
| 核心流程 | 部署接入 / 首轮取证 / 受控写回路 | [docs/workflow.md](docs/workflow.md) |
| 安全模型 | 逐环闸门与源码实现对照 | [docs/security.md](docs/security.md) |
| 部署 | 一键部署、固定地址隧道、排障表 | [docs/deployment.md](docs/deployment.md) |
| 冻结契约 | 工具 schema 与错误码 | [contracts/localcodebase-mcp-mvp.json](contracts/localcodebase-mcp-mvp.json) |

## 许可

仓库当前未附带 LICENSE 文件，授权条款由项目维护者确定后在此声明。
