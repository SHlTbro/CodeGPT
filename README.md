<p align="center">
  <img src="./assets/github-social-preview.png" width="100%" alt="上版项目展示海报：Local Codebase MCP、工具能力与历史验证基线">
</p>

<h1 align="center">CodeGPT</h1>

<p align="center">运行在你自己电脑上的 MCP 服务器：ChatGPT 等云端 AI 按授权检索、阅读、搜索本机的多个项目；可选开启受控写，写入逐环校验。</p>

封面沿用上版视觉（2026-10-01 历史展示）。**当前产品名为 CodeGPT，版本为 Core V1.2.0 RC2**；封面中的 326 项测试属于历史基线，当前推荐入口为 `setup.bat`。原始文件保存在本机，但工具返回的内容会传给所连接的模型服务端。

## 它是什么，解决什么问题

让云端模型理解本地代码，常见的做法是打包上传。上传的 zip 是某个时刻的快照，项目继续改动，模型的理解就停在旧版本；node_modules、构建产物这些噪音还会占掉上下文；代码交出去之后，每改一次就得重传一次。

CodeGPT 在你的电脑上跑一个 MCP 服务器，把「上传快照」换成「按需读取」：模型经 MCP 协议读此刻磁盘上的真实文件与 git 状态，问到哪就读到哪。模型能看到什么，由一张项目注册表决定，没登记的项目对它不存在。核心理念一句话：模型负责思考，MCP 只提供事实，用户掌握决策。

| 你得到什么 | 靠什么实现 |
|---|---|
| 回答基于当下事实，项目改完下一问就是新状态 | 实时读取本机文件与 git 状态，无快照 |
| 在对话中逐步建立项目的架构认知 | 批量读取（glob 通配 + 双层字符预算）、ripgrep 全文搜索、按行区间深读 |
| 跳转定义、查引用、看编译诊断 | 按项目标记文件自动探测语言服务器（Python/TS/Go/Rust），未安装时返回明确降级提示，可改用全文搜索 |
| 让 AI 直接整理文档、落地生成的文件 | 三个受控写工具（默认全项目关闭）：文档写入、二进制文档写入、工作区重构 |
| 不必反复整包上传项目 | 原始文件保存在本机；按授权读取的片段返回给模型，公网可达性由配置的隧道提供 |

## 本次新增：Core V1.2.0 RC2

在原有工具和使用方式上，补充本机配置页面与运行恢复能力。源码来自受测提交 `55a608cd9b84260d10e9b017a78ab68f02afd8ab`；此次展示同步不移动 RC2 发行 tag，也不切换现用服务。

| 新增内容 | 实际作用 |
|---|---|
| 本机 Setup 页面 | 添加/修改项目、默认只读、逐项目设置受控写；配置页与公网 MCP 分离 |
| 单一 TOML 配置 | 版本冲突检查 → 原子写入 → 热加载或受控重启 → MCP 验证；失败保留 last-good |
| 连接教程 | 首部提供 ngrok 注册、程序安装、Token 粘贴与 ChatGPT 接入步骤；Cloudflare 临时连接和仅本机为备选 |
| 真实网络状态 | 区分 Local MCP、Tunnel、Public URL 与实际工具调用，近期波动来自真实探测 |
| 生命周期与版本 | 复用进程恢复、Windows 登录启动；发行构建身份在进程启动时冻结 |
| 可选 Codex 设置 | Agent 默认 OFF，不安装或登录 Codex 时 Core 仍可用；自动阅读、VMware、自研 OAuth继续后置 |

[查看最新页面](https://shltbro.github.io/CodeGPT/#setup) · [打开交互流程图](https://shltbro.github.io/CodeGPT/diagrams/configuration-workflow.html)

![RC2 真实本机配置页：合成只读项目](assets/setup-current.png)

图为真实浏览器联调截图，配置操作请在本机运行 `setup.bat`。GitHub Pages 只展示页面，不保存凭据或读取本机文件。

## 工具面（15 个 = 12 只读 + 3 受控写）

| 类别 | 工具 |
|---|---|
| 发现 | `list_projects` `project_info` |
| 浏览读取 | `list_files` `read_file` `read_batch` |
| 搜索 | `search_code`（ripgrep） |
| 版本 | `git_info`（非 git 项目降级，不报错） |
| 智能（LSP，可选） | `find_definition` `find_references` `workspace_symbols` `get_diagnostics` `rename_preview` |
| 受控写（默认全关） | `write_file` `write_binary_file` `apply_workspace_edit` |

写能力默认对全部项目关闭，逐项目 `write_enabled = true` 开启。`write_file` 和 `write_binary_file` 的可写范围由项目级 `write_roots` 白名单约束（默认只有 `docs/`）。`apply_workspace_edit` 用于工作区源码重命名，**不受 `write_roots` 限制，可修改项目源码**，仍校验项目写权限、路径与敏感文件、整批哈希和锁内预检。更新需要对应文件哈希，文件被别人改过就拒绝写入，不会静默覆盖。

## 能力与验证

原有 15 个工具保留；LSP 正向能力需要对应语言服务器和工程上下文。工具注册成功不代表所有项目的语言分析能力均已通过。

本次公开源码联调 **76 PASS**，覆盖配置存储、安全和真实浏览器保存项目，并核对 TOML 与 MCP `list_projects`。沿用已有 RC2 安装、last-good、双向回退、真实 ChatGPT 读取和重连证据，不重复累加测试数字。详细范围见 [验证说明](docs/verification.md)。

<details>
<summary>保留上版能力图与历史验证基线（2026-10-01）</summary>

下图为上版 15 工具、9 生产模块、25 测试文件、326 项测试的历史快照，不代表当前源码规模或 RC2 新验收结果。

<p align="center">
  <img src="./assets/capability-verification.svg" width="720" alt="2026-10-01 历史基线：15 个工具、9 个生产模块、25 个测试文件、326 项测试通过">
</p>

</details>

## 快速开始

前置：Python 3.12+；`search_code` 依赖 ripgrep，Git 可选，LSP 按所用语言安装。

### 先在本机使用

一键（Windows）：下载本仓库源码 ZIP 或 `git clone https://github.com/SHlTbro/CodeGPT.git`，放到短路径，例如 `D:\CodeGPT`；进入 `local-codebase-mcp/`，双击 **setup.bat**。启动器在项目 `.venv` 安装依赖，并打开本机配置页 `http://127.0.0.1:8169/`。

1. 在页面添加已有目录，填写项目名和唯一 ID，第一次保持只读。
2. 点击 **保存并生效**；后端验证成功后才显示成功。
3. 本机 MCP 客户端连接 `http://127.0.0.1:8168/mcp`。ChatGPT 云端不能直接访问这个回环地址。
4. 连接 ChatGPT 时按下方教程配置隧道，添加连接后实际调用 `list_projects` 验证项目。

手动运行 MCP（不启动 Setup 或 Tunnel）：

```powershell
cd local-codebase-mcp
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config.example.toml config.toml
# 编辑 config.toml，添加真实项目；示例配置不启用公网隧道。
.\.venv\Scripts\python.exe server.py --config config.toml --check
.\.venv\Scripts\python.exe server.py --config config.toml
```

配置示例（完整空模板见 [`config.example.toml`](local-codebase-mcp/config.example.toml)）：

```toml
[[projects]]
id = "my-project"
name = "My Project"
path = "D:/my-project" # 替换为已存在的真实目录
write_enabled = false

[server]
host = "127.0.0.1"
port = 8168
path = "/mcp"

[tunnel]
mode = "off"
public_url = ""
```

用户配置以 TOML 为唯一事实源，不继续维护 YAML/TOML 两套权威配置。普通项目与权限修改支持热生效；需要重启的变更由现有生命周期处理，失败恢复 last-good。更多启动、停止和端口冲突说明见 [部署文档](docs/deployment.md)。

### 新增：连接 ChatGPT

- **ngrok，持续使用首选。** 注册独立账号、安装 ngrok，在本机页面粘贴 Authtoken；高级设置可填写账号分配的固定开发域名。启用后等待真实公网验证，再复制包含 `/mcp` 的地址。
- **Cloudflare 临时连接，备选。** 安装 cloudflared 后在页面启用，无需注册账号；临时地址重建会变化，不能算固定域名恢复。
- **仅本机，备选。** 不启用隧道；供本机 MCP 客户端使用，无法直接连接 ChatGPT 云端。

在 [ChatGPT 连接设置](https://chatgpt.com/plugins) 添加自定义 MCP，填写页面给出的公网地址，选中 CodeGPT 后问「我有哪些项目？」并确认实际 `list_projects` 返回。入口受账号和工作区策略影响，参见 [官方说明](https://developers.openai.com/plugins/deploy/connect-chatgpt)。

现有公网 MCP 没有最终用户身份认证。固定 URL、只读权限和 ngrok Authtoken 均不能代替访问身份；匿名候选只用于无敏感合成项目。私人代码公网接入需另行落实可信访问限制，详见 [安全边界](docs/security.md)。Token 只在本机页面输入，不提交 Git。

## 架构与流程

架构、配置调用链和源码依据见 [docs/architecture.md](docs/architecture.md)。部署接入、保存验证与失败恢复见 [docs/workflow.md](docs/workflow.md)。

### 新增：可交互流程图

[打开配置保存与失败恢复流程图](https://shltbro.github.io/CodeGPT/diagrams/configuration-workflow.html)。点击节点查看源码位置和上下游关系，支持搜索、路径聚焦、主题切换和导出。README 图片是静态展示；交互请打开网页，也可下载 `site/diagrams/configuration-workflow.html` 离线打开。

## 安全模型

服务器的安全立场是：默认最小暴露、逐环可拒绝、写入默认全关、闸门全部在服务端。完整的安全模型文档（每一环的威胁、闸门与源码实现）单独成文：[docs/security.md](docs/security.md)。要点：

- 回环绑定：只监听 `127.0.0.1` 等回环地址，配置校验直接拒绝其他绑定。
- 注册表白名单：只有登记过的项目存在；越界路径、路径穿越和链接逃逸在服务端拒绝。
- 敏感文件读写同闸：`.env`、`*.key`、`*.pem`、`credentials*`、`.ssh/**`、`.aws/**` 默认不可见。
- 文本/二进制文档写入的 10 环安全链：注册表 → 写开关 → 路径解析 → 可写白名单 → 敏感过滤 → 哈希冲突检查 → 长度预算 → 锁内预检 + 原子写 → 审计一行，由实际服务端实现执行。工作区重命名另按项目写权限、路径与敏感过滤、整批哈希及锁内检查执行，不沿用文档目录白名单。
- 无 shell：ripgrep/git/语言服务器全部 argv 数组直传，并有静态测试断言兜底；日志只记调用 metadata，不记文件内容与查询内容。

## 项目边界（如实声明）

- Core 不提供任意 shell、Agent 规划或代码索引数据库；可选 Agent 默认关闭，未经验证的公网调用不得启动 Codex。
- 应用层约束不是 OS 沙箱：安全模型约束「经 MCP 工具面可达的路径」。
- 模型读取的内容会发送到所连接的模型服务端；本项目解决授权边界与写入安全。
- 无性能基准数据；欢迎实测反馈。

## 成熟度

- Core 本地 RC2 就绪；固定域名 Tunnel 恢复已在真实 ChatGPT 合成只读候选验收。私人公网可信访问仍是独立条件。
- 已有首次安装、配置热加载/last-good、项目与权限保留、版本回退证据；启动器继续复用现有依赖安装和恢复逻辑。
- 发布范围为 Core；Agent 默认 OFF，VMware、自研 OAuth 和自动阅读后置，不拖住 Core。
- 封面和历史能力图中的 9 模块、25 测试文件、326 PASS 是早期快照；当前依赖以 `requirements.txt` 为准，不再宣称仅两条依赖。
- 原 15 工具契约保留；此次 GitHub 更新不等于本机生产升级，不移动发行 tag 或改写安装 ZIP。

## 文档导航

| 主题 | 内容 | 链接 |
|---|---|---|
| 一页介绍 | 给第一次看到本项目的人 | [docs/intro.md](docs/intro.md) |
| 架构 | 真实组件边界、配置调用链与源码依据 | [docs/architecture.md](docs/architecture.md) |
| 核心流程 | 配置保存、MCP 验证与失败恢复 | [docs/workflow.md](docs/workflow.md) |
| 安全模型 | 逐环闸门与源码实现对照 | [docs/security.md](docs/security.md) |
| 部署 | 一键部署、固定地址隧道、排障表 | [docs/deployment.md](docs/deployment.md) |
| 冻结契约 | 原 15 工具 schema 与错误码 | [contracts/localcodebase-mcp-mvp.json](contracts/localcodebase-mcp-mvp.json) |
| 最新页面 | RC2 真实配置页展示 | [GitHub Pages](https://shltbro.github.io/CodeGPT/#setup) |
| 交互流程图 | 节点、源码、路径与恢复分支 | [在线打开](https://shltbro.github.io/CodeGPT/diagrams/configuration-workflow.html) |
| 验证范围 | RC2 身份、此次联调与验收限制 | [docs/verification.md](docs/verification.md) |

## 许可

[MIT](LICENSE)。第三方组件与图表查看器见 [许可声明](THIRD_PARTY_NOTICES.md)。
