# 核心流程

> 三条链路全部来自真实使用过程：部署接入（deploy 脚本与部署经验文档）、首轮取证（工具说明的推荐调用序列）、受控写回路（写安全链与人机闭环约定）。图源为 GitHub 原生 Mermaid。

## 链路一：接入（部署一次，双击开始）

```mermaid
flowchart TD
    a["双击 deploy.bat"] --> b["检查并安装依赖<br/>Python 3.12+ · fastmcp · ripgrep · git · cloudflared"]
    b --> c{"config.yaml 已存在？"}
    c -->|"首次运行"| d["生成配置模板<br/>自动登记一个示例项目"]
    d --> e["编辑 config.yaml<br/>登记你的真实项目路径"]
    e --> f["重跑 deploy.bat"]
    c -->|"已有配置"| f
    f --> g["探测空闲端口 → 启动服务 → 启动公网隧道"]
    g --> h["屏幕打印可粘贴的公网 URL<br/>同时写入 public-url.txt"]
    h --> i["粘进 ChatGPT 连接器<br/>服务器 URL · 无身份验证"]
    i --> j["对话里问：我有哪些项目？<br/>返回已登记清单即接入成功"]
```

两个常见疑问：隧道地址每次重启会变（要固定地址用 Cloudflare named 隧道，配置一次终身可用）；本机浏览器打不开隧道地址不影响 ChatGPT 使用，判据看服务端日志有没有外部请求。

## 链路二：首轮取证（每个新项目的第一轮对话）

```mermaid
flowchart TD
    t1["project_info<br/>顶层目录 · 文件数 · 标志性文件"] --> t2["list_files<br/>限深树看结构，或 paths 一次批量看多目录"]
    t2 --> t3["read_batch<br/>一次读 README / 清单文件 / 入口文件 / glob"]
    t3 --> t4{"需要更细的证据？"}
    t4 -->|"位置已知，要看细节"| t5["read_file<br/>按行区间定向深读"]
    t4 -->|"不知道位置"| t6["search_code<br/>ripgrep 全文搜索"]
    t5 --> t7["回答附带真实文件路径与行号"]
    t6 --> t7
```

这四步是服务端推荐的调用序列：第一轮就建立架构认知，避免逐文件多轮往返。批量读取有双层字符预算，内容超限的文件以 `BUDGET_EXHAUSTED` 标注，不会打爆上下文。

## 链路三：受控写回路（默认关闭，逐项目开启）

```mermaid
sequenceDiagram
    actor U as 你（用户）
    participant M as ChatGPT（模型）
    participant S as 本机服务器
    participant F as 项目文件

    U->>M: 「把这份会议记录整理成 docs 下的文档」
    M->>U: 展示拟写内容（路径 + 全文）
    U->>M: 确认，指示写入
    M->>S: write_file（path, content, expected_absent / content_sha256）
    S->>S: 安全链校验：registry → write_enabled → 路径解析 → 白名单 → 敏感过滤 → 冲突 → 预算
    alt 校验全部通过
        S->>F: 原子写入（tmp + replace）
        S-->>M: 成功（返回全文 sha256）
        S->>S: server.log 记一行审计（不含内容）
    else 文件在读取后被他人改动
        S-->>M: REVISION_CONFLICT（拒绝，不静默覆盖）
        M->>S: read_file 取新的 content_sha256
        M->>S: 重新提交 write_file
    end
```

MCP 协议没有审批弹窗，所以约定的人机闭环是：模型在对话里展示拟写内容，你确认后才指示写入；服务端闸门负责兜底（范围、敏感过滤、哈希冲突、大小预算），审计可查。工作区级重构走两段式：`rename_preview` 先出逐文件编辑计划（只读），`apply_workspace_edit` 应用时全部校验通过才写，任一不匹配整体拒绝、零文件落盘。

## 图与事实的映射

| 图要素 | 事实来源 |
|---|---|
| 部署九步（依赖/模板/端口/隧道/URL） | 一键部署脚本流程与部署经验文档的 v2 流程节 |
| URL 写入 public-url.txt；named 隧道固定地址 | 部署脚本的 URL 抓取与 named 模式分支 |
| 首轮四步调用序列与批量预算 | README「批量取证」节（服务端强制规则） |
| 展示拟写内容 → 用户确认 → 服务端兜底 | README「审批语义」节（人机闭环约定） |
| 10 环安全链与 REVISION_CONFLICT 行为 | `writer.py` 写链实现（顺序不可变）与冲突检查代码 |
| 审计一行不含内容 | `server.py` 日志纪律；`writer.py` 审计行实例 |
