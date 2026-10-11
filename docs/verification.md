# 验证范围与发行身份

运行源码和前端同步自 Core RC2 `55a608cd9b84260d10e9b017a78ab68f02afd8ab`，并保留原 GitHub main 历史。后续 GitHub 文档/图提交不重写既有 RC2 tag 或安装 ZIP。

已有 RC2 本地安装、配置热加载/last-good、15工具、双向回退、真实 ChatGPT 读取、MCP重启与新会话证据复用。固定域名 Tunnel 重启后相同地址和原连接读取通过，范围仅无敏感合成只读项目。私人公网身份限制、Wi-Fi/VPN/睡眠等主机情形不因此通过。

此次仅同步冻结代码、更新失效入口与交互图，并运行导出源码的代表性配置/安全/真实浏览器联调。测试使用临时合成目录、独立端口、TunnelOFF/AgentOFF，不读取私人项目或改生产实例。

源码克隆没有发行 build-manifest 时，启动身份诚实显示 unknown。受测发行包的身份：产品1.2.0，来源RC2完整SHA，Build ID `d4e29a5e032d4a14853143ad1574b93b`，原ZIP SHA-256 `50ef5ac52039be99ecc633ba55a96cfea626a8da5451bfe94f872ba7fb3ea050`。不将当前 Git HEAD冒充运行代码。

公开测试保留Core相关回归，排除依赖私有实验、固定内部归档目录的Agent验收文件。历史根 contracts 仅为原15工具冻结契约的测试参考，Agent模型执行仍后置。

本次公开源码实测：`python -B -m pytest tests/unit/test_config_store.py tests/security/test_setup_secrets.py tests/integration/test_core_setup_browser.py::test_core_browser_default_off_real_save_and_explicit_optional_settings -q`，退出码 0，76 PASS。包含真正启动 Setup/MCP、浏览器保存合成项目、核对 TOML 与 `list_projects`；没有付费模型请求或公网凭据。

Archify 3.0.1 的校验、交付、严格检查和浏览器检查全部通过。节点来源面板、正反搜索、路径深链和重置经过真实浏览器交互；HTML 无外部脚本、样式和图片依赖。

展示更新保留旧 GitHub 历史和不可变的 RC2 发行身份。内部制作日志和安全扫描记录不作为产品功能，不进入公开首页。
