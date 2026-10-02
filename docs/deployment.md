# 部署

## 前置依赖

- Python 3.12+（`python --version` 确认）
- ripgrep：`search_code` 依赖。Windows `winget install BurntSushi.ripgrep.MSVC`，macOS `brew install ripgrep`，Linux `apt install ripgrep`
- git（可选）：不装也能用，`git_info` 显示「非 git 项目」

## 形态一：一键部署（Windows）

双击 `local-codebase-mcp/deploy.bat`（或命令行 `powershell -NoProfile -ExecutionPolicy Bypass -File deploy.ps1`）。脚本自动完成：

1. 检查/安装依赖（Python 3.12+、fastmcp、ripgrep、git、cloudflared，下载失败自动切镜像）
2. 首次运行生成 `config.yaml` 模板，编辑登记真实项目后重跑一次
3. 探测空闲端口 → 启动服务 → 启动公网隧道 → 抓取公网 URL
4. 屏幕打印可粘进 ChatGPT 的地址，同时写入 `public-url.txt`

## 形态二：手动部署（全平台）

```bash
cd local-codebase-mcp
pip install -r requirements.txt
cp config.example.yaml config.yaml     # 编辑：登记你的真实项目路径
python server.py --check               # 校验配置与端口占用，不启动
python server.py                       # 启动服务
```

启动成功的标志是打印「local-codebase-mcp 启动：N 个项目已登记，监听 http://127.0.0.1:8000/mcp」；配置有误会打印 `CONFIG_ERROR` 与修复提示后退出。

## 接入 ChatGPT（需要公网隧道）

ChatGPT 网页版访问不到你的 `127.0.0.1`，所以必须有一条公网隧道把本机端口转发出去。三种形态：

| 形态 | 地址稳定性 | 前置 |
|---|---|---|
| cloudflared 快速隧道 | 每次重启会变 | 零账号，`deploy.bat` 默认 |
| cloudflared named 隧道 | 固定地址，一次配置终身可用 | 免费 Cloudflare 账号 + 托管在 Cloudflare 的域名 |
| ngrok | 固定域（付费计划） | ngrok 账号 |

named 隧道配置要点：在 Cloudflare Zero Trust 控制台创建隧道后，把 token 存为 `local-codebase-mcp/tunnel-token.txt`（或用 `-TunnelToken` 参数传入）；部署脚本检测到 token 后以 named 模式启动，token 经 `TUNNEL_TOKEN` 环境变量传给子进程，不写入命令行、日志或文件。隧道 dashboard 的 Service 端口必须与运行时端口一致。

接入手续：把 `https://xxx/mcp` 地址粘进 ChatGPT 设置 → 连接器 → 创建（服务器 URL，无身份验证），创建后在开发者模式选中，对话里问「我有哪些项目？」验证。本机浏览器打不开隧道地址是预期现象（部分网络的 DNS 对免费隧道域名解析失败），访问发生在模型服务侧；判定连通性看服务端日志有没有外部请求。

纯本机客户端（无需隧道）：MCP Inspector（`npx @modelcontextprotocol/inspector`）直连 `http://127.0.0.1:8000/mcp`。

## 运行期行为

- **配置热重载**：运行中修改配置文件 ≤5 秒生效，新增/删除项目、开关写入无需重启，隧道地址不变；`server` 段（host/port/path）变更需重启。
- **启动前校验**：`python server.py --check` 只校验配置与端口占用，全部通过 exit 0，任一失败 exit 1 并指明具体项。
- **日志**：`server.log` 只记调用 metadata（时间戳/工具名/项目 id/耗时/结果码），不记录文件与查询内容。

## 语言服务器安装（可选，LSP 四工具 + 重构预览）

| 项目标记文件 | 语言 | 服务器命令 |
|---|---|---|
| `pyproject.toml` / `requirements.txt` | Python | `pyright-langserver --stdio` |
| `package.json` | TS/JS | `npx typescript-language-server --stdio` |
| `go.mod` | Go | `gopls` |
| `Cargo.toml` | Rust | `rust-analyzer` |

任选其一全局安装后重启 server 即可（`npm install -g pyright` 等）。未安装时相关工具返回 `LSP_UNAVAILABLE` 并提示降级用 `search_code`，其余工具不受影响。服务器不在 PATH 时可用配置段 `intel.servers` 覆盖（argv 直传，不经 shell）。

## 排障表

| 现象（错误码） | 原因 | 处理 |
|---|---|---|
| `CONFIG_ERROR`，启动即退出 | 配置缺失 / YAML 语法错 / id 重复 / path 不存在 / host 非回环 | 按报错提示（含行号与示例）修复后重启 |
| `SEARCH_BACKEND_ERROR` | ripgrep 未安装或不可执行 | 安装 ripgrep 后重启 |
| `GIT_ERROR` | git 未安装或命令失败 | 确认 git 在 PATH；非 git 项目不是错误 |
| 端口被占用 | 默认端口被其他程序占用 | 改 `config.yaml` 的 `server.port`；一键部署会自动探测空闲端口 |
| `WRITE_DISABLED` | 目标项目未开写（默认全关） | 配置中该项目加 `write_enabled: true`，热重载生效 |
| `WRITE_PATH_FORBIDDEN` | 写目标不在 `write_roots` 白名单 | 只写白名单目录；或在配置中显式扩展该项目 `write_roots` |
| `REVISION_CONFLICT` | 读取后文件被他人/其他会话改动 | 重新读取获取新 `content_sha256` 再试（不会静默覆盖） |
| `EXTENSION_NOT_ALLOWED` | 二进制写扩展名不在白名单 | 仅 `.docx/.pdf/.xlsx/.zip/.pptx/.doc/.xls`；文本用 `write_file` |
| `LSP_UNAVAILABLE` | 语言服务器未安装/启动失败 | 安装对应服务器后重启，或降级用 `search_code` |
| ChatGPT 连不上 | 隧道断了或 URL 不含 `/mcp` | 重开隧道；确认 URL 以 `/mcp` 结尾；看服务端日志有无外部请求 |

一线部署中沉淀的十条连接失败原因（SSE 兼容性、隧道域名封锁、地址过期、防火墙、端口残留、Python/网络、工具缺失、配置错误、浏览器自测误判、账号侧问题）与逐段排障顺序，沉淀在项目维护者的部署经验记录中；上述排障表覆盖其中的服务端可判定位。
