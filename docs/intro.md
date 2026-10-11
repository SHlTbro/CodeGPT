# CodeGPT Core V1.2.0

运行在本机的 Python/FastMCP 服务。用户在本机页面登记项目与权限，客户端通过 15 个工具按需浏览、读取、检索与受控修改。配置保存后经过原子 TOML、后端应用和真实验证；失败保留 last-good。

首版优先稳定、轻量和既有能力。Codex/Agent 默认关闭；VMware、高隔离 Worker、自研 OAuth、Claude 和自动开发委派后置，不阻塞 Core。

先按根 README 启动 Setup，再决定仅本机或公网连接。当前匿名隧道不能视为私人代码安全接入。历史 YAML/8000/旧部署说明由当前 TOML 与8168/8169说明替代。
