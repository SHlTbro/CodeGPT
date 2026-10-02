"""注册层：register_tools(mcp) 把 15 个工具（12 只读 + write_file + apply_workspace_edit + write_binary_file）挂到 FastMCP。

- 各工具 description 照抄契约 tools[].description（面向模型的权威文案，禁止改写）。
- 只读注解统一 readOnlyHint=true / openWorldHint=false；write_file、apply_workspace_edit 与
  write_binary_file 用 readOnlyHint=false / destructiveHint=true / openWorldHint=false（契约 v1.2/v1.4/v1.6）。
- McpError → error_payload 信封；任何未捕获异常 → INTERNAL_ERROR 信封（traceback 只进日志不进响应）。
- 每次工具调用记 metadata 日志（时间戳/tool/project_id/duration_ms/结果码），不记录任何文件或查询内容。
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import errors
import tools_services
import writer
from config import Config, load_config
from errors import McpError, error_payload

CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"

# 热重载用可变引用单元：整体替换 cell[0]（原子防部分态），工具闭包每次读取最新值
_holder: dict = {"config": None}

log = logging.getLogger("local-codebase-mcp.tools")

# —— 契约 tools[].description 原文 ——
_DESC_LIST_PROJECTS = "列出 config.yaml 登记的全部可用项目。ChatGPT 首次连接时调用，用于发现有哪些项目可分析。"
_DESC_PROJECT_INFO = "返回指定项目的基础事实：顶层目录、文件数、标志性文件（README/package.json/pyproject.toml/go.mod/Cargo.toml 等）存在性。只报事实，不做架构判断。 新项目分析第一步建议：本工具 + list_files(depth=2) + read_batch(README/清单文件) 一次建立架构索引。"
_DESC_LIST_FILES = "列出项目内某目录或多个目录的内容（ls 风格分组或限深树）。默认过滤 .git/node_modules/dist/build/__pycache__ 与敏感文件。分析新项目时建议：先用 depth=2 树建立结构全貌，再定向读文件，避免逐目录多轮往返。"
_DESC_SEARCH_CODE = "在项目内用 ripgrep 搜索代码，返回 {file, line, text} 匹配列表。"
_DESC_READ_FILE = "按行区间读取项目内 UTF-8 文本文件。 批量读多个文件请改用 read_batch。"
_DESC_GIT_INFO = "返回项目 Git 上下文事实：分支、改动文件（路径+状态）、最近提交、per-file diff stat。非 git 项目返回 is_git_repo=false 降级而非报错。"
_DESC_WRITE_FILE = "创建或更新项目声明的可写目录（默认 docs/，可在 config.yaml 项目级 write_roots 扩展）下的 UTF-8 文本文件（文档维护专用）。新建文件必须 expected_absent=true；更新已有文件必须传入最近一次 read_file 返回的 content_sha256（防覆盖他人/用户新改动）。禁止写可写目录之外路径。"
_DESC_FIND_DEFINITION = "跳转到光标处符号的定义位置（基于项目语言服务器）。语言服务器未安装时返回 LSP_UNAVAILABLE 并建议 search_code。"
_DESC_FIND_REFERENCES = "查找光标处符号的全部引用位置（含/不含声明可选）。"
_DESC_WORKSPACE_SYMBOLS = "按名称模糊搜索项目工作区符号（类/函数/变量等，基于语言服务器）。"
_DESC_GET_DIAGNOSTICS = "获取项目（或指定文件）的诊断信息（编译错误/警告，基于语言服务器推送+拉取）。"
_DESC_RENAME_PREVIEW = "预览工作区级符号重命名的全部改动（基于语言服务器 rename），只读不写：返回每个受影响文件的逐处编辑计划（1-based 位置 + 新文本）。应用需再调 apply_workspace_edit 并附各文件最新 content_sha256。"
_DESC_APPLY_WORKSPACE_EDIT = "应用 rename_preview 返回的工作区编辑（多文件源码重构写）。必须提供每个受影响文件最近一次 read_file 的 content_sha256（全部校验通过才写，任一不匹配整体拒绝）。需项目 write_enabled=true；绕过 docs/** 白名单（workspace 重构语义），但仍受路径逃逸防护与敏感文件硬过滤约束。"
_DESC_READ_BATCH = "批量读取多个文件建立项目认知（一次调用替代多轮 read_file）。推荐第一轮取证即用：README、package.json/pyproject.toml、入口文件、配置文件一次读完。paths 支持通配符（如 packages/*/package.json、**/*.patch.yml，命中上限 50 个文件）。每文件独立截断，总额度兜底，防打爆上下文。"
_DESC_WRITE_BINARY_FILE = "将 base64 编码的二进制内容写入项目声明的可写目录（默认 docs/，可在 config.yaml 项目级 write_roots 扩展）下的文件（支持 .docx/.pdf/.xlsx/.zip/.pptx/.doc/.xls）。新建文件必须 expected_absent=true；更新已有文件必须传入最近一次 read_file 或 write_binary_file 返回的 content_sha256。禁止写可写目录之外路径及非白名单扩展名。"

# 契约 annotations（C8）
_READ_ONLY = {"readOnlyHint": True, "openWorldHint": False}
_WRITE = {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False}


def _finish(tool: str, project_id: str | None, t0: float, result) -> dict:
    """统一出口：记 metadata 日志并返回结果。错误信封的 code 记入结果码（成功记 ok）。"""
    code = result["error"]["code"] if isinstance(result, dict) and "error" in result else "ok"
    log.info("tool=%s project_id=%s duration_ms=%.1f result=%s", tool, project_id, (time.perf_counter() - t0) * 1000, code)
    return result


def register_tools(mcp, config: Config | None = None) -> None:
    """把 15 个工具（12 只读 + write_file + apply_workspace_edit + write_binary_file）注册到 FastMCP 实例 mcp；config 缺省时从本目录 config.yaml 加载。"""
    if config is None:
        config = load_config(CONFIG_PATH)
    _holder["config"] = config

    def _config() -> Config:
        return _holder["config"] if _holder["config"] is not None else config

    @mcp.tool(name="list_projects", description=_DESC_LIST_PROJECTS, annotations=_READ_ONLY)
    def _list_projects():
        t0, pid = time.perf_counter(), None
        try:
            return _finish("list_projects", pid, t0, tools_services.list_projects(_config()))
        except McpError as e:
            return _finish("list_projects", pid, t0, e.payload)
        except Exception:
            log.exception("tool=list_projects 未捕获异常")
            return _finish("list_projects", pid, t0, error_payload(errors.INTERNAL_ERROR, "内部错误，请查看 server 日志"))

    @mcp.tool(name="project_info", description=_DESC_PROJECT_INFO, annotations=_READ_ONLY)
    def _project_info(project_id: str):
        t0 = time.perf_counter()
        try:
            return _finish("project_info", project_id, t0, tools_services.project_info(_config(), project_id))
        except McpError as e:
            return _finish("project_info", project_id, t0, e.payload)
        except Exception:
            log.exception("tool=project_info project_id=%s 未捕获异常", project_id)
            return _finish("project_info", project_id, t0, error_payload(errors.INTERNAL_ERROR, "内部错误，请查看 server 日志"))

    @mcp.tool(name="list_files", description=_DESC_LIST_FILES, annotations=_READ_ONLY)
    def _list_files(project_id: str, path: str = ".", depth: int = 0, paths: list | None = None):
        t0 = time.perf_counter()
        try:
            return _finish("list_files", project_id, t0, tools_services.list_files(_config(), project_id, path, depth, paths))
        except McpError as e:
            return _finish("list_files", project_id, t0, e.payload)
        except Exception:
            log.exception("tool=list_files project_id=%s 未捕获异常", project_id)
            return _finish("list_files", project_id, t0, error_payload(errors.INTERNAL_ERROR, "内部错误，请查看 server 日志"))

    @mcp.tool(name="read_file", description=_DESC_READ_FILE, annotations=_READ_ONLY)
    def _read_file(project_id: str, path: str, start_line: int = 1, end_line: int | None = None):
        t0 = time.perf_counter()
        try:
            return _finish("read_file", project_id, t0, tools_services.read_file(_config(), project_id, path, start_line, end_line))
        except McpError as e:
            return _finish("read_file", project_id, t0, e.payload)
        except Exception:
            log.exception("tool=read_file project_id=%s 未捕获异常", project_id)
            return _finish("read_file", project_id, t0, error_payload(errors.INTERNAL_ERROR, "内部错误，请查看 server 日志"))

    # —— v1.5 批量取证（read_batch：多文件 + glob + 双层预算；阻塞 rg/读盘走 to_thread）——
    @mcp.tool(name="read_batch", description=_DESC_READ_BATCH, annotations=_READ_ONLY)
    async def _read_batch(project_id: str, paths: list, max_chars_per_file: int = 50000, max_chars_total: int = 200000):
        t0 = time.perf_counter()
        try:
            return _finish("read_batch", project_id, t0, await asyncio.to_thread(
                tools_services.read_batch, _config(), project_id, paths, max_chars_per_file, max_chars_total))
        except McpError as e:
            return _finish("read_batch", project_id, t0, e.payload)
        except Exception:
            log.exception("tool=read_batch project_id=%s 未捕获异常", project_id)
            return _finish("read_batch", project_id, t0, error_payload(errors.INTERNAL_ERROR, "内部错误，请查看 server 日志"))

    @mcp.tool(name="search_code", description=_DESC_SEARCH_CODE, annotations=_READ_ONLY)
    async def _search_code(project_id: str, query: str, path: str = "."):
        t0 = time.perf_counter()
        try:
            return _finish("search_code", project_id, t0, await tools_services.search_code(_config(), project_id, query, path))
        except McpError as e:
            return _finish("search_code", project_id, t0, e.payload)
        except Exception:
            log.exception("tool=search_code project_id=%s 未捕获异常", project_id)
            return _finish("search_code", project_id, t0, error_payload(errors.INTERNAL_ERROR, "内部错误，请查看 server 日志"))

    @mcp.tool(name="git_info", description=_DESC_GIT_INFO, annotations=_READ_ONLY)
    def _git_info(project_id: str):
        t0 = time.perf_counter()
        try:
            return _finish("git_info", project_id, t0, tools_services.git_info(_config(), project_id))
        except McpError as e:
            return _finish("git_info", project_id, t0, e.payload)
        except Exception:
            log.exception("tool=git_info project_id=%s 未捕获异常", project_id)
            return _finish("git_info", project_id, t0, error_payload(errors.INTERNAL_ERROR, "内部错误，请查看 server 日志"))

    @mcp.tool(name="write_file", description=_DESC_WRITE_FILE, annotations=_WRITE)
    async def _write_file(project_id: str, path: str, content: str,
                          expected_absent: bool = False, content_sha256: str | None = None):
        t0 = time.perf_counter()
        try:
            cfg = _config()
            # v1.7：write_roots 取自项目对象；未登记项目交由 writer 安全链 1 报 PROJECT_NOT_FOUND
            proj = cfg.registry.get(project_id)
            return _finish("write_file", project_id, t0, await asyncio.to_thread(
                writer.write_file, cfg, project_id, path, content, expected_absent, content_sha256,
                proj.write_roots if proj else ("docs",)))
        except McpError as e:
            return _finish("write_file", project_id, t0, e.payload)
        except Exception:
            log.exception("tool=write_file project_id=%s 未捕获异常", project_id)
            return _finish("write_file", project_id, t0, error_payload(errors.INTERNAL_ERROR, "内部错误，请查看 server 日志"))

    # —— v1.6 受控二进制写入（write_binary_file：扩展名白名单 + base64 + 10MB 预算；阻塞写盘走 to_thread）——
    @mcp.tool(name="write_binary_file", description=_DESC_WRITE_BINARY_FILE, annotations=_WRITE)
    async def _write_binary_file(project_id: str, path: str, content_b64: str,
                                 expected_absent: bool = False, content_sha256: str = ""):
        t0 = time.perf_counter()
        try:
            cfg = _config()
            # v1.7：write_roots 取自项目对象；未登记项目交由 writer 安全链 1 报 PROJECT_NOT_FOUND
            proj = cfg.registry.get(project_id)
            return _finish("write_binary_file", project_id, t0, await asyncio.to_thread(
                writer.write_binary_file, cfg, project_id, path, content_b64,
                expected_absent, content_sha256 or None,
                proj.write_roots if proj else ("docs",)))
        except McpError as e:
            return _finish("write_binary_file", project_id, t0, e.payload)
        except Exception:
            log.exception("tool=write_binary_file project_id=%s 未捕获异常", project_id)
            return _finish("write_binary_file", project_id, t0, error_payload(errors.INTERNAL_ERROR, "内部错误，请查看 server 日志"))

    # —— v1.3 LSP 四智能工具（line/character 1-based 校验在服务层 _pos_arg；阻塞 LSP 调用走 to_thread）——
    @mcp.tool(name="find_definition", description=_DESC_FIND_DEFINITION, annotations=_READ_ONLY)
    async def _find_definition(project_id: str, path: str, line: int, character: int):
        t0 = time.perf_counter()
        try:
            return _finish("find_definition", project_id, t0, await asyncio.to_thread(
                tools_services.find_definition, _config(), project_id, path, line, character))
        except McpError as e:
            return _finish("find_definition", project_id, t0, e.payload)
        except Exception:
            log.exception("tool=find_definition project_id=%s 未捕获异常", project_id)
            return _finish("find_definition", project_id, t0, error_payload(errors.INTERNAL_ERROR, "内部错误，请查看 server 日志"))

    @mcp.tool(name="find_references", description=_DESC_FIND_REFERENCES, annotations=_READ_ONLY)
    async def _find_references(project_id: str, path: str, line: int, character: int,
                               include_declaration: bool = False):
        t0 = time.perf_counter()
        try:
            return _finish("find_references", project_id, t0, await asyncio.to_thread(
                tools_services.find_references, _config(), project_id, path, line, character, include_declaration))
        except McpError as e:
            return _finish("find_references", project_id, t0, e.payload)
        except Exception:
            log.exception("tool=find_references project_id=%s 未捕获异常", project_id)
            return _finish("find_references", project_id, t0, error_payload(errors.INTERNAL_ERROR, "内部错误，请查看 server 日志"))

    @mcp.tool(name="workspace_symbols", description=_DESC_WORKSPACE_SYMBOLS, annotations=_READ_ONLY)
    async def _workspace_symbols(project_id: str, query: str):
        t0 = time.perf_counter()
        try:
            return _finish("workspace_symbols", project_id, t0, await asyncio.to_thread(
                tools_services.workspace_symbols, _config(), project_id, query))
        except McpError as e:
            return _finish("workspace_symbols", project_id, t0, e.payload)
        except Exception:
            log.exception("tool=workspace_symbols project_id=%s 未捕获异常", project_id)
            return _finish("workspace_symbols", project_id, t0, error_payload(errors.INTERNAL_ERROR, "内部错误，请查看 server 日志"))

    @mcp.tool(name="get_diagnostics", description=_DESC_GET_DIAGNOSTICS, annotations=_READ_ONLY)
    async def _get_diagnostics(project_id: str, path: str | None = None):
        t0 = time.perf_counter()
        try:
            return _finish("get_diagnostics", project_id, t0, await asyncio.to_thread(
                tools_services.get_diagnostics, _config(), project_id, path))
        except McpError as e:
            return _finish("get_diagnostics", project_id, t0, e.payload)
        except Exception:
            log.exception("tool=get_diagnostics project_id=%s 未捕获异常", project_id)
            return _finish("get_diagnostics", project_id, t0, error_payload(errors.INTERNAL_ERROR, "内部错误，请查看 server 日志"))

    # —— v1.4 workspace 重构两段式（rename_preview 只读；apply_workspace_edit 走写注解 + to_thread）——
    @mcp.tool(name="rename_preview", description=_DESC_RENAME_PREVIEW, annotations=_READ_ONLY)
    async def _rename_preview(project_id: str, path: str, line: int, character: int, new_name: str):
        t0 = time.perf_counter()
        try:
            return _finish("rename_preview", project_id, t0, await asyncio.to_thread(
                tools_services.rename_preview, _config(), project_id, path, line, character, new_name))
        except McpError as e:
            return _finish("rename_preview", project_id, t0, e.payload)
        except Exception:
            log.exception("tool=rename_preview project_id=%s 未捕获异常", project_id)
            return _finish("rename_preview", project_id, t0, error_payload(errors.INTERNAL_ERROR, "内部错误，请查看 server 日志"))

    @mcp.tool(name="apply_workspace_edit", description=_DESC_APPLY_WORKSPACE_EDIT, annotations=_WRITE)
    async def _apply_workspace_edit(project_id: str, files: list, expected_shas: dict):
        t0 = time.perf_counter()
        try:
            return _finish("apply_workspace_edit", project_id, t0, await asyncio.to_thread(
                tools_services.apply_workspace_edit, _config(), project_id, files, expected_shas))
        except McpError as e:
            return _finish("apply_workspace_edit", project_id, t0, e.payload)
        except Exception:
            log.exception("tool=apply_workspace_edit project_id=%s 未捕获异常", project_id)
            return _finish("apply_workspace_edit", project_id, t0, error_payload(errors.INTERNAL_ERROR, "内部错误，请查看 server 日志"))
