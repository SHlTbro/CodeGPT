# 架构与交互视图

CodeGPT 由本机 Setup、MCP、Tunnel 程序及按需 LSP 组成；TOML 与项目目录留在本机，返回的内容传给客户端。

[在线交互配置流程](https://shltbro.github.io/CodeGPT/diagrams/configuration-workflow.html) · [离线 HTML](../site/diagrams/configuration-workflow.html) · [可编辑 JSON](../site/diagrams/configuration-workflow.json)

- 浏览器 → localhost Setup：`web/app.js` 和 `setup.py` 的受保护配置 API。
- Setup → `ConfigStore.save`：验证、CAS、原子 TOML、验证和 last-good。
- Setup → Runtime → MCP：管理自己的子进程、应用配置并实际探测。
- Tunnel → localhost MCP：公网数据面；不转发 Setup。服务商连通与公网 MCP 验证分开。
- MCP → 项目/语言服务器：`tools.py`、`security.py`、`writer.py`、`intel.py`，权限在服务端执行。

原有 15 个工具保留。`local_agents` 中部分模块是配置/版本兼容依赖；Agent 默认 OFF，不表示远程模型执行已启用。没有额外的管理服务、VMware 或自研 OAuth 部署。

前端截图来自此次独立合成项目的真实 Setup 页面；不是模拟接口。图源码为 RC2 代码证据，在线源链接不伪装为内部候选分支。

## 系统边界与核心进程

[系统边界 HTML](../site/diagrams/architecture-context.html) 把 CodeGPT 视为运行在用户电脑上的软件系统。维护者通过本机页面登记目录与权限；MCP 客户端直连本机，或经过可选公网隧道。项目目录是用户的原始文件；工具返回内容才会离开本机。

[核心进程 HTML](../site/diagrams/architecture-containers.html) 展开浏览器、Setup、FastMCP、可选 Tunnel 和配置文件。Runtime 是 Setup 进程内的职责；TOML、last-good、pending 是文件存储。Git、ripgrep、LSP 属于按需工具进程，这张核心图未展开它们，不代表引入新的管理服务。

公网隧道的上游只指向 MCP。Setup 另做回环来源、Host / Origin、代理头与修改请求 token 检查。虚线表示启用后才存在的调用关系，不证明当前公网连接成功，也不提供访问者身份认证。

## 保存与恢复

[成功保存 HTML](../site/diagrams/configuration-save-success.html) 展示请求、版本与配置校验通过后的持久化和 MCP 验证；全部成功后才确认配置。它不承诺 ChatGPT 会话或公网已经连接。

[应用失败恢复 HTML](../site/diagrams/configuration-recovery.html) 展示 last-good 成功恢复和恢复失败的处理。恢复成功仍返回本次保存失败；恢复失败需要检查端口、进程和权限后重读。完整图保留拒绝、并发冲突、回退和重新提交路径。
