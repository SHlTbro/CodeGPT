# 配置保存与失败恢复

[打开可交互流程图](https://shltbro.github.io/CodeGPT/diagrams/configuration-workflow.html)

前端修改 → 后端校验 → 检查配置版本 → 原子写 TOML → 热加载或受控重启 → 真实 MCP 验证 → 页面确认。

输入错误不写配置；版本冲突保留输入并要求读取最新配置后合并；应用失败恢复 last-good，再验证恢复结果，仍显示本次保存失败。恢复未能验证时提示人工修复，不能显示假成功。修复后重新读取当前配置版本再保存，未自动重复模型调用。

源码：`local-codebase-mcp/web/app.js`、`setup.py`、`config.py` 和 `runtime.py`。交互图可点击节点、搜索/聚焦，下载 HTML 后亦可离线运行。
