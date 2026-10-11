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
