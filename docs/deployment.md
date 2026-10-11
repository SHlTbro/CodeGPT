# 部署与恢复

进入 `local-codebase-mcp/` 双击 `setup.bat`。Python 3.12+，推荐短路径；依赖只装到该目录 `.venv`。默认 MCP 8168、Setup 8169，首次没有 TOML 时创建空项目配置，Tunnel OFF、Agent OFF。

## 配置与入口

- `setup.bat`：本机配置页和统一生命周期。
- `deploy.bat`：复用 Setup，兼容已有调用；没有第二套配置。
- `stop-all.bat`：默认 Setup 地址的受控停止；独立端口实例在自己的页面停止，不按进程名批量结束。
- 页面登录启动：当前 Windows 用户登录后启动，默认关闭；保存后核验，不是系统服务。

配置只认 `config.toml`。缺少 TOML 时可一次性迁移旧 YAML，后续 YAML 不生效。独立实例先创建配置并确认 canonical 和 last-good 的 host/port/tunnel/startup，再启动；命令行覆盖不是启动前的隔离措施。

```toml
projects = []
[server]
host = "127.0.0.1"
port = 18168
path = "/mcp"
[tunnel]
mode = "off"
public_url = ""
[startup]
enabled = false
```

## ngrok 操作

1. 注册/登录 https://dashboard.ngrok.com/signup ，完成账号验证。
2. 从 https://ngrok.com/download/windows 下载并把 ngrok.exe 放到程序目录。
3. 在 https://dashboard.ngrok.com/get-started/your-authtoken 复制 Authtoken；只粘贴到本机页面密码框。
4. 如果需要固定地址，在 https://dashboard.ngrok.com/domains 查看账号开发域名，在高级连接设置填写完整 `https://<域名>/mcp`。
5. 一键启用，等候公网 MCP 验证，再把完整 URL 添加到 ChatGPT。

有效凭据不保证账号未超限或网络可达。页面会显示服务商错误；额度耗尽不能靠重输 Token 解除。Cloudflare Quick Tunnel 是免注册备选；地址重建会变化，本 RC 拒绝不能核验 ingress 范围的 named 隧道。

## 恢复与版本回退

MCP/Tunnel 意外退出由 Runtime 恢复，Setup 意外退出由同一启动器恢复。正常停止、配置无效和端口占用不无限重启。恢复不重放已接受任务，不重新应用初次覆盖参数。实际 ChatGPT 恢复要以客户端调用确认。

升级前受控停止自己的实例，备份代码、TOML/last-good、`.setup` 及任务报告到只有 owner/SYSTEM/Administrators 可读的本机目录。新版本解压新目录，预先禁用 Tunnel/Agent/登录启动，独立端口验证。不要覆盖正在运行的源码或用户配置。

回退保留两套目录和最新合法配置修改；新增 Agent 字段可用发行代码内 `local_agents.config_migration` 按当前 SHA 进行显式降级，保留 migration record 与任务/报告。失败停止新实例，使用已核验仅本机恢复配置启动旧源码；不强制 reset，不删除状态，不自动切换公网地址。
