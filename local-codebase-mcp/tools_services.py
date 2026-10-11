"""MCP project, file, search, Git and language-server services.

Reuse Config, ProjectRegistry and shared path/visibility guards. Return facts;
raise McpError for the registration layer to encode. ripgrep argv is passed
directly without shell interpolation. Controlled workspace edits verify hashes
before writing and share writer's per-project lock.
"""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import logging
import os
import subprocess
import threading
from fnmatch import fnmatch
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname  # 仅 URI→路径换算（Windows 盘符语义），无网络出站

import errors
import intel
from config import Config
from errors import McpError
from projects import Project
from security import (
    DEFAULT_EXCLUDED_DIRS,
    MAX_CHARS,
    MAX_FILE_SIZE_BYTES,
    MAX_SEARCH_RESULTS,
    is_sensitive,
    sensitive_globs,
    is_too_large,
    resolve_path,
    truncate_search_line,
)

# 契约 project_info 探测的标志性文件
MARKER_FILES = (
    "README.md", "package.json", "pyproject.toml", "go.mod", "Cargo.toml",
    "Cargo.lock", "requirements.txt", "pnpm-lock.yaml", "package-lock.json", "yarn.lock",
)

_RG_HINT = "安装 ripgrep 后重启 server"


def _suggest(project_id: str, config: Config) -> str:
    """difflib 相似度 >=0.6 的最近 id → 'Did you mean: xxx?'；无相近 → ''。"""
    close = difflib.get_close_matches(project_id, [p.id for p in config.registry], n=1, cutoff=0.6)
    return f" Did you mean: {close[0]}?" if close else ""


def _project(config: Config, project_id: str) -> Project:
    """按 id 取项目；未登记 → PROJECT_NOT_FOUND，message 列全部可用 id + 相近建议。"""
    if not isinstance(project_id, str) or not project_id:
        raise McpError(errors.INVALID_ARGUMENT, "project_id 必填且为非空字符串", "按 schema 修正参数后重试")
    p = config.registry.get(project_id)
    if p is None:
        ids = ", ".join(pr.id for pr in config.registry)
        raise McpError(
            errors.PROJECT_NOT_FOUND,
            f"project_id 未在 config.toml 登记: {project_id!r}。可用项目: {ids or '（无）'}.{_suggest(project_id, config)}",
            "调用 list_projects 查看可用项目，换正确 project_id 重试",
        )
    return p


def _arg(ok: bool, message: str) -> None:
    if not ok:
        raise McpError(errors.INVALID_ARGUMENT, message, "按 schema 修正参数后重试")


def _rel_of(root: Path, target: Path) -> str:
    """target 相对项目根的 / 分隔路径（根本身 → ''）。前提：resolve_path 已放行。"""
    rel = target.relative_to(root).as_posix()
    return "" if rel == "." else rel


def _is_excluded(rel: str, excluded: tuple[str, ...] | list[str]) -> bool:
    return any(part in excluded for part in rel.split("/"))


def _ensure_visible(rel: str, patterns, excluded) -> None:
    """list/read 可见性同闸（契约 C5.5）：敏感文件或默认排除目录内 → FILE_NOT_VISIBLE。"""
    if rel and (is_sensitive(rel, patterns) or is_sensitive(rel + "/", patterns) or _is_excluded(rel, excluded)):
        raise McpError(errors.FILE_NOT_VISIBLE, f"该路径对 AI 不可见: {rel!r}（命中敏感过滤或默认排除目录）", "该文件对 AI 不可见")


def _rg_globs(excluded, patterns=()) -> list[str]:
    """把默认排除目录转成 rg --glob 排除参数（**/ 前缀：任意层级生效）。"""
    globs: list[str] = []
    for d in excluded:
        globs += ["--glob", f"!**/{d}/**"]
    for pattern in sensitive_globs(patterns):
        globs += ["--glob", "!" + pattern, "--glob", "!**/" + pattern]
    return globs


def _norm(p: str) -> str:
    return p.replace("\\", "/").removeprefix("./")


def _run_rg_files(project: Project, rel: str, security: dict) -> list[str]:
    """rg --files 一次遍历（cwd=项目根），返回相对路径列表；rg 缺失/失败 → SEARCH_BACKEND_ERROR。"""
    argv = ["rg", "--files", "--hidden", *_rg_globs(security.get("default_excluded_dirs", DEFAULT_EXCLUDED_DIRS), security.get("sensitive_patterns", ()))]
    if rel:
        argv.append(rel)
    try:
        proc = subprocess.run(argv, cwd=str(project.path), capture_output=True, encoding="utf-8", errors="replace", check=False, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise McpError(errors.SEARCH_BACKEND_ERROR, "ripgrep 未安装或不可执行", _RG_HINT)
    if proc.returncode not in (0, 1):
        raise McpError(errors.SEARCH_BACKEND_ERROR, f"ripgrep 执行失败（退出码 {proc.returncode}）: {proc.stderr.strip()[:1000]}", "检查报错路径是否存在、是否被其他程序占用；可限定 path 后重试")
    return [_norm(ln) for ln in proc.stdout.splitlines() if ln]


def _visible_files(entries: list[str], patterns, excluded) -> list[str]:
    """对 rg --files 结果做敏感 + 排除目录过滤（闸门在 Python 侧，与 read/search 同源）。"""
    return [e for e in entries if not is_sensitive(e, patterns) and not _is_excluded(e, excluded)]


def _limits(config: Config, key: str, default: int) -> int:
    try:
        return int(config.limits.get(key, default))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- Tool 1
def list_projects(config: Config) -> dict:
    """列出 config.toml 登记的全部可用项目。顺序按声明序；响应不带 project_id（契约 responseFields）。"""
    return {"projects": [{"id": p.id, "name": p.name} for p in config.registry]}


# ---------------------------------------------------------------- Tool 2
def project_info(config: Config, project_id: str) -> dict:
    """返回项目基础事实：顶层目录（默认排除+敏感过滤）、可见文件数（一次 rg --files）、标志性文件存在性。

    空项目返回 0/空数组（EMPTY 态正常返回）。rg 缺失时降级为本地遍历统计
    （契约未给 project_info 定义 SEARCH_BACKEND_ERROR，见完工报告上报）。
    """
    p = _project(config, project_id)
    root = p.path.resolve()
    patterns = tuple(config.security["sensitive_patterns"])
    excluded = tuple(config.security["default_excluded_dirs"])
    root_dirs = sorted(
        e.name for e in os.scandir(root)
        if e.is_dir() and e.name not in excluded and not is_sensitive(e.name + "/", patterns)
    )
    try:
        present = set(_visible_files(_run_rg_files(p, "", config.security), patterns, excluded))
        files_count, detected = len(present), [m for m in MARKER_FILES if m in present]
    except McpError:
        files_count, present = _walk_visible(root, patterns, excluded)
        detected = [m for m in MARKER_FILES if m in present]
    return {"project_id": p.id, "name": p.name, "root_dirs": root_dirs, "files_count": files_count, "detected_files": detected}


def _walk_visible(root: Path, patterns, excluded) -> tuple[int, set[str]]:
    """rg 不可用时的降级统计：os.walk 剪枝排除目录 + 敏感过滤。"""
    count, present = 0, set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in excluded and not is_sensitive(d + "/", patterns)]
        for f in filenames:
            rel = (Path(dirpath) / f).relative_to(root).as_posix()
            if not is_sensitive(rel, patterns):
                count += 1
                present.add(rel)
    return count, present


# ---------------------------------------------------------------- Tool 3
def _tree_view(name: str, paths: list[str], depth_left: int, budget: list) -> dict:
    """限深树节点：depth_left>1 才展开子目录；budget=[剩余配额,是否截断] 超额即停。"""
    node: dict = {"name": name, "dirs": [], "files": []}
    sub: dict[str, list[str]] = {}
    for pth in paths:
        head, _, tail = pth.partition("/")
        if tail:
            sub.setdefault(head, []).append(tail)
        elif budget[0] > 0:
            node["files"].append(pth)
            budget[0] -= 1
        else:
            budget[1] = True
    for d in sorted(sub):
        if budget[0] <= 0:
            budget[1] = True
            break
        budget[0] -= 1
        node["dirs"].append(
            _tree_view(d, sub[d], depth_left - 1, budget) if depth_left > 1
            else {"name": d, "dirs": [], "files": []}
        )
    return node


def list_files(config: Config, project_id: str, path: str = ".", depth: int = 0, paths: list | None = None) -> dict:
    """入口（契约 v1.5）：paths 给定（非空数组）→ 批量模式（paths 优先，忽略 path）；否则单目录双视图。"""
    p = _project(config, project_id)
    if paths is not None:
        _arg(isinstance(paths, list) and 1 <= len(paths) <= 20,
             "paths 必须是 1-20 个目录路径的字符串数组（与 path 二选一，paths 优先）")
        _arg(all(isinstance(x, str) and x for x in paths), "paths 每项必须是非空字符串")
        _arg(isinstance(depth, int) and not isinstance(depth, bool) and 0 <= depth <= 5,
             "depth 必须是 0-5 的整数（0=直接子项分组；>=1=限深树形）")
        groups = [_list_files_group(config, p, gp, depth) for gp in paths]
        result: dict = {"project_id": p.id, "groups": groups}
        if any(g.get("truncated") for g in groups):
            result["truncated"], result["next_hint"] = True, "缩小 paths 中触顶目录的范围或减小 depth"
        return result
    return _list_files_single(config, project_id, path, depth)


def _list_files_group(config: Config, p: Project, gp: str, depth: int) -> dict:
    """批量模式单组：复用单目录逻辑（含 depth）；任何 McpError → 组内 {error:{code,message}} 不整体失败。"""
    try:
        root = p.path.resolve()
        final = resolve_path(p, gp)
        rel = _rel_of(root, final)
        if rel and not final.exists():
            raise McpError(errors.FILE_NOT_FOUND, f"项目内路径不存在: {rel!r}",
                           "先调用 project_info 查看顶层目录确认实际路径")
        g = _list_files_single(config, p.id, gp, depth)
    except McpError as e:
        return {"path": gp, "error": {"code": e.payload["error"]["code"], "message": e.payload["error"]["message"]}}
    g.pop("project_id", None)
    g["path"] = gp
    return g


def _list_files_single(config: Config, project_id: str, path: str = ".", depth: int = 0) -> dict:
    """双视图列出目录内容（契约 v1.2，行为逐字不变）：depth=0 直接子项分组 {dirs,files}；depth>=1 限深树 {tree}。

    单次 rg --files 遍历后内存组装两视图，敏感/排除过滤在视图组装前生效。
    全树条目总数（目录+文件）超 list_files_max_entries=2000 截断置 truncated=true + next_hint；
    depth 非法 → INVALID_ARGUMENT；请求路径命中敏感/排除 → FILE_NOT_VISIBLE（与 read_file 同闸）。
    """
    p = _project(config, project_id)
    _arg(isinstance(path, str), "path 必须是项目内相对路径字符串")
    _arg(isinstance(depth, int) and not isinstance(depth, bool) and 0 <= depth <= 5,
         "depth 必须是 0-5 的整数（0=直接子项分组；>=1=限深树形）")
    root = p.path.resolve()
    rel = _rel_of(root, resolve_path(p, path))
    patterns = tuple(config.security["sensitive_patterns"])
    excluded = tuple(config.security["default_excluded_dirs"])
    _ensure_visible(rel, patterns, excluded)
    entries = _visible_files(_run_rg_files(p, rel, config.security), patterns, excluded)
    if rel:  # rg 输出带 rel/ 前缀（cwd=项目根），视图组装前剥回相对请求目录
        prefix = rel + "/"
        entries = [e[len(prefix):] for e in entries if e.startswith(prefix)]
    limit = _limits(config, "list_files_max_entries", 2000)
    result: dict = {"project_id": p.id}
    if depth == 0:
        dirs = sorted({e.split("/", 1)[0] for e in entries if "/" in e})
        files = sorted(e for e in entries if "/" not in e)
        truncated = len(dirs) + len(files) > limit
        if truncated:
            dirs = dirs[:limit]
            files = files[: max(limit - len(dirs), 0)]
        result.update(dirs=dirs, files=files, truncated=truncated)
    else:
        budget = [limit, False]
        result["tree"] = _tree_view(rel.rsplit("/", 1)[-1] if rel else ".", entries, depth, budget)
        result["truncated"] = budget[1]
    if result["truncated"]:
        result["next_hint"] = "缩小 path 或减小 depth"
    return result


# ---------------------------------------------------------------- Tool 5
def _read_file_impl(config: Config, p: Project, path: str, start_line: int,
                    end_line: int | None, max_chars: int) -> dict:
    """read_file/read_batch（v1.5）共用读取核心：校验链与截断语义与 read_file 逐字一致，max_chars 由调用方决定。"""
    root = p.path.resolve()
    final = resolve_path(p, path)
    rel = _rel_of(root, final)
    patterns = tuple(config.security["sensitive_patterns"])
    excluded = tuple(config.security["default_excluded_dirs"])
    _ensure_visible(rel, patterns, excluded)
    if not final.exists() or final.is_dir():
        raise McpError(
            errors.FILE_NOT_FOUND,
            f"项目内路径不存在或不是文件: {rel or path!r}",
            "先 list_files 查看该文件的父目录确认实际路径，再重试",
        )
    size_limit = _limits(config, "max_file_size_bytes", MAX_FILE_SIZE_BYTES)
    if is_too_large(final.stat().st_size, size_limit):
        raise McpError(errors.FILE_TOO_LARGE, f"文件 {final.stat().st_size} 字节超过上限 {size_limit}", "文件过大无法读取，考虑搜索定位片段")
    # content_sha256（契约 v1.2 恢复字段）：全文 hash，write_file 更新冲突检测的必要凭据
    content_sha256 = hashlib.sha256(final.read_bytes()).hexdigest()
    parts: list[str] = []
    consumed, last_line, truncated = 0, start_line - 1, False
    with final.open("rb") as fh:
        for lineno, raw in enumerate(fh, start=1):
            if lineno < start_line:
                continue
            if end_line is not None and lineno > end_line:
                break
            if b"\x00" in raw:
                raise McpError(errors.BINARY_FILE_UNSUPPORTED, f"二进制文件不支持文本读取: {rel}", "仅支持 UTF-8 文本")
            try:
                text = raw.decode("utf-8").rstrip("\r\n")
            except UnicodeDecodeError:
                raise McpError(errors.BINARY_FILE_UNSUPPORTED, f"文件不是有效的 UTF-8 文本: {rel}", "仅支持 UTF-8 文本")
            cost = len(text) + (1 if parts else 0)  # 与 "\n".join 的长度口径一致（含换行）
            if consumed + cost > max_chars:
                avail = max_chars - consumed - (1 if parts else 0)
                if avail > 0:
                    parts.append(text[:avail])
                    last_line = lineno
                truncated = True
                break
            consumed += cost
            parts.append(text)
            last_line = lineno
    return {"project_id": p.id, "path": path, "start_line": start_line, "end_line": last_line,
            "content": "\n".join(parts), "encoding": "utf-8", "content_sha256": content_sha256,
            "truncated": truncated}


def read_file(config: Config, project_id: str, path: str, start_line: int = 1, end_line: int | None = None) -> dict:
    """按行区间读取项目内 UTF-8 文本文件（含端点）。

    校验顺序：INVALID_ARGUMENT（不触发文件访问）→ PATH_OUTSIDE_WORKSPACE → FILE_NOT_VISIBLE
    → FILE_NOT_FOUND → FILE_TOO_LARGE（5MB 前置）→ 按行流式读。
    含 \\x00 判二进制、非 UTF-8 文本 → BINARY_FILE_UNSUPPORTED；
    输出含 content_sha256（全文 hash，契约 v1.2 恢复字段）；
    max_chars=50000 截断置 truncated=true；越界请求按实际末行返回不报错。
    """
    p = _project(config, project_id)
    _arg(isinstance(path, str) and bool(path), "path 必填且为非空字符串，且必须是项目内相对路径")
    _arg(isinstance(start_line, int) and not isinstance(start_line, bool) and start_line >= 1,
         f"start_line 必须是 >= 1 的整数（实际: {start_line!r}）")
    if end_line is not None:
        _arg(isinstance(end_line, int) and not isinstance(end_line, bool),
             f"end_line 必须是 >= 1 的整数（实际: {end_line!r}）")
        _arg(end_line >= start_line,
             f"end_line({end_line}) 不得小于 start_line({start_line})，要求 end_line >= start_line")
    return _read_file_impl(config, p, path, start_line, end_line, _limits(config, "max_chars", MAX_CHARS))


# ---------------------------------------------------------------- Tool 14（契约 v1.5）
_READ_BATCH_GLOB_MAX = 50
_BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"  # 组内软标注（契约 notes），不进 errorCodes


def _is_glob(entry: str) -> bool:
    """含 * / ?（含 **）即按 glob 处理。"""
    return "*" in entry or "?" in entry


def read_batch(config: Config, project_id: str, paths: list,
               max_chars_per_file: int = 50000, max_chars_total: int = 200000) -> dict:
    """批量读取多个文件建立项目认知（契约 v1.5）：paths 展开 → 逐文件读 → 双层预算兜底。

    展开：字面路径直接读；含 * ? ** 的条目在「rg --files 可见集合」（_run_rg_files 全量 +
    _visible_files 过滤）上 fnmatch 展开，单模式命中 >50 → INVALID_ARGUMENT（建议缩小模式）。
    逐文件复用 read_file 语义：敏感 FILE_NOT_VISIBLE / 二进制 / FILE_TOO_LARGE / 不存在 →
    组内 error（不整体失败，不占内容额度）；max_chars_per_file 独立截断；content_sha256 全文 hash。
    总额度 max_chars_total 按已产 content 累计，耗尽后剩余文件组内 BUDGET_EXHAUSTED +
    顶层 truncated:true + next_hint；顺序按输入展开序，展开结果去重保序。
    """
    p = _project(config, project_id)
    _arg(isinstance(paths, list) and bool(paths), "paths 必填且为至少 1 个条目的数组")
    _arg(len(paths) <= 20, "paths 最多 20 个条目（glob 展开另受单模式 50 命中上限约束）")
    _arg(all(isinstance(x, str) and x for x in paths), "paths 每项必须是非空字符串")
    _arg(isinstance(max_chars_per_file, int) and not isinstance(max_chars_per_file, bool) and max_chars_per_file >= 1000,
         f"max_chars_per_file 必须 >= 1000 的整数（实际: {max_chars_per_file!r}）")
    _arg(isinstance(max_chars_total, int) and not isinstance(max_chars_total, bool) and max_chars_total >= 10000,
         f"max_chars_total 必须 >= 10000 的整数（实际: {max_chars_total!r}）")
    patterns = tuple(config.security["sensitive_patterns"])
    excluded = tuple(config.security["default_excluded_dirs"])
    expanded: list[str] = []
    seen: set[str] = set()
    for entry in paths:
        if _is_glob(entry):
            hits = [e for e in _visible_files(_run_rg_files(p, "", config.security), patterns, excluded)
                    if fnmatch(e, entry)]
            if len(hits) > _READ_BATCH_GLOB_MAX:
                raise McpError(
                    errors.INVALID_ARGUMENT,
                    f"glob 模式 {entry!r} 命中 {len(hits)} 个文件，超过单模式 {_READ_BATCH_GLOB_MAX} 上限",
                    "缩小 glob 模式（加目录前缀或更具体的文件名）后重试")
            fresh = [h for h in hits if h not in seen]
        else:
            fresh = [entry] if entry not in seen else []
        seen.update(fresh)
        expanded.extend(fresh)
    files_out: list[dict] = []
    total, exhausted = 0, False
    for ep in expanded:
        if exhausted:
            files_out.append({"path": ep, "error": {
                "code": _BUDGET_EXHAUSTED,
                "message": f"读取总额度 max_chars_total={max_chars_total} 已耗尽（已产生 {total} 字符），本文件未读取"}})
            continue
        try:
            r = _read_file_impl(config, p, ep, 1, None, max_chars_per_file)
        except McpError as e:
            files_out.append({"path": ep, "error": {
                "code": e.payload["error"]["code"], "message": e.payload["error"]["message"]}})
            continue
        cost = len(r["content"])
        if total + cost > max_chars_total:
            exhausted = True
            files_out.append({"path": ep, "error": {
                "code": _BUDGET_EXHAUSTED,
                "message": f"读取该文件将超出总额度 max_chars_total={max_chars_total}（已产生 {total} 字符，需 {cost} 字符）"}})
            continue
        total += cost
        files_out.append({"path": ep, "content": r["content"],
                          "content_sha256": r["content_sha256"], "truncated": r["truncated"]})
    result: dict = {"project_id": p.id, "files": files_out, "total_chars": total, "truncated": exhausted}
    if exhausted:
        result["next_hint"] = "总额度已耗尽：提高 max_chars_total、减少 paths 或缩小 glob 模式后重试"
    return result


# ---------------------------------------------------------------- Tool 4
async def search_code(config: Config, project_id: str, query: str, path: str = ".") -> dict:
    """用 ripgrep 搜索代码，返回 {file, line, text} 匹配列表（相对路径）。

    argv 数组直传 asyncio.create_subprocess_exec（--fixed-strings：query 按字面处理），
    绝对禁止 shell 拼接。超 max_search_results=100 截断 + next_hint；
    单行 text 截断 500 字符；rg 退出码 1=无匹配=空列表非错误；rg 缺失 → SEARCH_BACKEND_ERROR。
    """
    p = _project(config, project_id)
    _arg(isinstance(query, str) and bool(query), "query 必填且为非空字符串")
    _arg(isinstance(path, str), "path 必须是项目内相对路径字符串")
    root = p.path.resolve()
    rel = _rel_of(root, resolve_path(p, path))
    patterns = tuple(config.security["sensitive_patterns"])
    excluded = tuple(config.security["default_excluded_dirs"])
    _ensure_visible(rel, patterns, excluded)
    argv = ["rg", "--line-number", "--with-filename", "--no-heading", "--fixed-strings", "--hidden",
            *_rg_globs(excluded, patterns), "--", query, rel or "."]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(root), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise McpError(errors.SEARCH_BACKEND_ERROR, "ripgrep 未安装或不可执行", _RG_HINT)
    out, err = await proc.communicate()
    if proc.returncode not in (0, 1):
        raise McpError(errors.SEARCH_BACKEND_ERROR, f"ripgrep 执行失败（退出码 {proc.returncode}）: {err.decode('utf-8', 'replace').strip()[:1000]}", "检查报错路径是否存在、是否被其他程序占用；可限定 path 后重试")
    max_results = _limits(config, "max_search_results", MAX_SEARCH_RESULTS)
    matches: list[dict] = []
    truncated = False
    for ln in out.decode("utf-8", "replace").splitlines():
        seg = ln.split(":", 2)
        if len(seg) < 3:
            continue
        fpath = _norm(seg[0])
        if is_sensitive(fpath, patterns) or _is_excluded(fpath, excluded):
            continue
        try:
            lineno = int(seg[1])
        except ValueError:
            continue
        matches.append({"file": fpath, "line": lineno, "text": truncate_search_line(seg[2])})
        if len(matches) > max_results:
            truncated = True
            break
    result = {"project_id": p.id, "matches": matches[:max_results], "truncated": truncated}
    if truncated:
        result["next_hint"] = "缩小 query 或限定 path"
    return result


# ---------------------------------------------------------------- Tool 6
_GIT_HINT = "确认 git 可用"
_DIFF_STAT_LIMIT = 50
_EMPTY_GIT = {"is_git_repo": False, "branch": None, "changed_files": [], "recent_commits": [], "diff_stat": []}
# porcelain XY 码 → 契约状态（其余码保守归为 modified）
_PORCELAIN_STATUS = {"M": "modified", "T": "modified", "A": "added", "D": "deleted", "R": "renamed", "C": "added"}


def _run_git(root: Path, args: list[str]) -> tuple[int, str, str]:
    """git -C root <args>，argv 直传禁 shell，stdin=DEVNULL（防管道挂起）。git 缺失 → GIT_ERROR。"""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, encoding="utf-8", errors="replace",
            check=False, stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise McpError(errors.GIT_ERROR, "git 未安装或不可执行", _GIT_HINT)
    return proc.returncode, proc.stdout, proc.stderr


def _git_ok(root: Path, args: list[str]) -> str:
    """要求成功的 git 调用；失败（非 git 仓库场景已在上游排除）→ GIT_ERROR。"""
    rc, out, err = _run_git(root, args)
    if rc != 0:
        raise McpError(errors.GIT_ERROR, f"git 命令失败（退出码 {rc}）: {err.strip()}", _GIT_HINT)
    return out


def _parse_porcelain(out: str) -> list[dict]:
    """status --porcelain=v1 → [{path, status}]；含未跟踪（??）；rename 取 -> 后新路径。"""
    files: list[dict] = []
    for ln in out.splitlines():
        if len(ln) < 4:
            continue
        x, y, path = ln[0], ln[1], ln[3:]
        if x == "?" and y == "?":
            files.append({"path": _norm(path), "status": "untracked"})
            continue
        code = x if x != " " else y
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        files.append({"path": _norm(path.strip()), "status": _PORCELAIN_STATUS.get(code, "modified")})
    return files


def _parse_numstat(out: str, acc: dict[str, list[int]]) -> None:
    """numstat → 合并进 acc{file: [add, del]}；二进制行（'-'）按 0 记入。"""
    for ln in out.splitlines():
        seg = ln.split("\t", 2)
        if len(seg) < 3:
            continue
        add = 0 if seg[0] == "-" else int(seg[0] or 0)
        dele = 0 if seg[1] == "-" else int(seg[1] or 0)
        slot = acc.setdefault(_norm(seg[2]), [0, 0])
        slot[0] += add
        slot[1] += dele


def git_info(config: Config, project_id: str) -> dict:
    """返回项目 Git 上下文事实：branch / changed_files（路径+状态明细）/ recent_commits / per-file diff_stat。

    非 git 目录（无 .git）→ is_git_repo=false 降级正常返回；空仓库（无 HEAD）→ branch=null、
    commits 空、diff_stat 仅工作区对比；diff_stat 超 50 文件截断置 truncated=true。
    不返回全文 diff；git 缺失或仓库损坏 → GIT_ERROR。
    """
    p = _project(config, project_id)
    root = p.path.resolve()
    if not (root / ".git").exists():
        return {"project_id": p.id, **_EMPTY_GIT}
    result: dict = {"project_id": p.id, "is_git_repo": True}
    has_head = _run_git(root, ["rev-parse", "--verify", "--quiet", "HEAD"])[0] == 0
    result["branch"] = _git_ok(root, ["rev-parse", "--abbrev-ref", "HEAD"]).strip() if has_head else None
    result["changed_files"] = _parse_porcelain(_git_ok(root, ["status", "--porcelain=v1"]))
    if has_head:
        commits: list[dict] = []
        for ln in _git_ok(root, ["log", "--pretty=format:%h%x1f%s%x1f%cs", "-n", "10"]).splitlines():
            seg = ln.split("\x1f")
            if len(seg) == 3:
                commits.append({"hash": seg[0], "subject": seg[1], "date": seg[2]})
        result["recent_commits"] = commits
        acc: dict[str, list[int]] = {}
        _parse_numstat(_git_ok(root, ["diff", "--numstat"]), acc)
        _parse_numstat(_git_ok(root, ["diff", "--cached", "--numstat"]), acc)
    else:  # 空仓库：无 HEAD，diff 仅工作区对 index
        result["recent_commits"] = []
        acc = {}
        _parse_numstat(_git_ok(root, ["diff", "--numstat"]), acc)
    stat = [{"file": f, "additions": a, "deletions": d} for f, (a, d) in acc.items()]
    truncated = len(stat) > _DIFF_STAT_LIMIT
    result["diff_stat"] = stat[:_DIFF_STAT_LIMIT]
    result["truncated"] = truncated
    return result


# ---------------------------------------------------------------- v1.3 LSP 智能工具（task18）
# IntelManager 单例：按 config 对象身份缓存（热重载整体替换 config → 旧会话关闭重建）
_INTEL: list = [None, None]
_INTEL_LOCK = threading.Lock()


def _intel_mgr(config: Config) -> intel.IntelManager:
    with _INTEL_LOCK:
        if _INTEL[0] is not config or _INTEL[1] is None:
            if _INTEL[1] is not None:
                _INTEL[1].close_all()
            _INTEL[0], _INTEL[1] = config, intel.IntelManager(config)
        return _INTEL[1]



def _pos_arg(line, character) -> None:
    """line/character 1-based 下限校验（<1 → INVALID_ARGUMENT，message 注明 1-based）。"""
    for name, v in (("line", line), ("character", character)):
        _arg(isinstance(v, int) and not isinstance(v, bool) and v >= 1,
             f"{name} 必须是 >= 1 的整数（1-based，实际: {v!r}）")


def _lsp_target(config: Config, project_id: str, path: str):
    """definition/references 公共前链：项目 → 路径校验/可见性/存在性 → 会话 → ensure_open。"""
    p = _project(config, project_id)
    _arg(isinstance(path, str) and bool(path), "path 必填且为项目内相对路径")
    root = p.path.resolve()
    final = resolve_path(p, path)
    rel = _rel_of(root, final)
    _ensure_visible(rel, tuple(config.security["sensitive_patterns"]),
                    tuple(config.security["default_excluded_dirs"]))
    if not final.is_file():
        raise McpError(errors.FILE_NOT_FOUND, f"项目内路径不存在或不是文件: {rel or path!r}",
                       "先 list_files 确认路径")
    sess = _intel_mgr(config).get_session(p)
    sess.ensure_open(final)
    return p, final, rel, sess


def _trim(result: dict, key: str, items: list, max_results: int) -> dict:
    """max_results 截断语义：truncated=true + next_hint='缩小 query 或限定 path'。"""
    truncated = len(items) > max_results
    result[key], result["truncated"] = items[:max_results], truncated
    if truncated:
        result["next_hint"] = "缩小 query 或限定 path"
    return result


def find_definition(config: Config, project_id: str, path: str, line: int, character: int) -> dict:
    """跳转定义：1-based 入参转 LSP 0-based，结果转回相对路径 + 1-based。"""
    _pos_arg(line, character)
    p, final, _, sess = _lsp_target(config, project_id, path)
    res = sess.request("textDocument/definition", {
        "textDocument": {"uri": final.as_uri()}, "position": intel.to_lsp_position(line, character)})
    raw = res if isinstance(res, list) else ([res] if res else [])
    locs = [l for l in (intel.normalize_location(p.path, r) for r in raw) if l]
    return _trim({"project_id": p.id}, "locations", locs,
                 _limits(config, "max_search_results", MAX_SEARCH_RESULTS))


def find_references(config: Config, project_id: str, path: str, line: int, character: int,
                    include_declaration: bool = False) -> dict:
    """查引用：include_declaration → LSP includeDeclaration；max_results=100 截断。"""
    _pos_arg(line, character)
    p, final, _, sess = _lsp_target(config, project_id, path)
    res = sess.request("textDocument/references", {
        "textDocument": {"uri": final.as_uri()}, "position": intel.to_lsp_position(line, character),
        "context": {"includeDeclaration": bool(include_declaration)}}) or []
    locs = [l for l in (intel.normalize_location(p.path, r) for r in res) if l]
    return _trim({"project_id": p.id}, "locations", locs,
                 _limits(config, "max_search_results", MAX_SEARCH_RESULTS))


def workspace_symbols(config: Config, project_id: str, query: str) -> dict:
    """按名称模糊搜索工作区符号；kind 数值转名称，位置 1-based；max_results=100 截断。"""
    p = _project(config, project_id)
    _arg(isinstance(query, str) and bool(query), "query 必填且为非空字符串")
    sess = _intel_mgr(config).get_session(p)
    # tsserver creates projects after didOpen; all visible source files also cover
    # inferred projects without tsconfig, rather than relying on earlier tool calls.
    sources = _visible_files(_run_rg_files(p, "", config.security),
                             tuple(config.security["sensitive_patterns"]),
                             tuple(config.security["default_excluded_dirs"]))
    for rel in sources:
        if Path(rel).suffix.lower() in intel.SOURCE_LANGUAGES:
            final = resolve_path(p, rel)
            if not is_too_large(final.stat().st_size):
                sess.ensure_open(final)
    res = sess.request("workspace/symbol", {"query": query}) or []
    syms = [s for s in (intel.normalize_symbol(p.path, r) for r in res if isinstance(r, dict)) if s]
    return _trim({"project_id": p.id}, "symbols", syms,
                 _limits(config, "max_search_results", MAX_SEARCH_RESULTS))


def get_diagnostics(config: Config, project_id: str, path: str | None = None) -> dict:
    """诊断（push 暂存为主）：给了 path 时先 ensure_open 再拉取 textDocument/diagnostic 刷新该文件，
    拉取失败退回暂存；severity 数值→字符串映射在 normalize_diagnostics。"""
    p = _project(config, project_id)
    rel = None
    if path not in (None, "."):
        _arg(isinstance(path, str) and bool(path), "path 必须是项目内相对路径字符串")
        root = p.path.resolve()
        final = resolve_path(p, path)
        rel = _rel_of(root, final)
        _ensure_visible(rel, tuple(config.security["sensitive_patterns"]),
                        tuple(config.security["default_excluded_dirs"]))
        if not final.is_file():
            raise McpError(errors.FILE_NOT_FOUND, f"项目内路径不存在或不是文件: {rel or path!r}",
                           "先 list_files 确认路径")
        sess = _intel_mgr(config).get_session(p)
        sess.ensure_open(final)
        try:
            pulled = sess.request("textDocument/diagnostic", {"textDocument": {"uri": final.as_uri()}})
            if isinstance(pulled, dict) and isinstance(pulled.get("items"), list):
                sess.diagnostics[final.as_uri()] = pulled["items"]
        except McpError as error:
            if getattr(error, "lsp_code", None) != -32601:
                raise
            # Servers such as tsls publish diagnostics asynchronously.
            # An empty cache does not establish that a file has no errors.
            sess.wait_diagnostics(final.as_uri())
    else:
        sess = _intel_mgr(config).get_session(p)
    diags: list[dict] = []
    for uri, items in list(sess.diagnostics.items()):
        diags.extend(intel.normalize_diagnostics(p.path, uri, items))
    if rel is not None:
        diags = [d for d in diags if d["path"] == rel]
    return _trim({"project_id": p.id}, "diagnostics", diags,
                 _limits(config, "max_search_results", MAX_SEARCH_RESULTS))


# ---------------------------------------------------------------- v1.4 workspace 重构（task21）
log = logging.getLogger("local-codebase-mcp.tools")

_EDIT_KEYS = ("line", "character", "end_line", "end_character")


def _valid_edit(e) -> bool:
    """契约编辑结构：四坐标均为 >=1 的 int（1-based 双闭区间）+ new_text 字符串。"""
    return (isinstance(e, dict) and isinstance(e.get("new_text"), str)
            and all(isinstance(e.get(k), int) and not isinstance(e[k], bool) and e[k] >= 1
                    for k in _EDIT_KEYS)
            and (e["end_line"], e["end_character"]) >= (e["line"], e["character"]))


def _apply_edits(text: str, edits: list[dict], rel: str) -> str:
    """行数组上自后向前拼接（同文件多编辑按 (start,end) 降序防错位）。

    区间口径（契约）：1-based 双闭——首字符 0-based 偏移 = character-1，
    末字符 0-based 偏移 = end_character-1（字符串切片 stop = end_character）。
    行数组 splitlines(keepends=True)，单行内偏移用字符串切片实现；
    跨行区间把 [i0..i1] 行整体折叠为一行（prefix + new_text + suffix）。
    """
    lines = text.splitlines(keepends=True)
    for e in sorted(edits, key=lambda x: (x["line"], x["character"]), reverse=True):
        i0, i1 = e["line"] - 1, e["end_line"] - 1
        if i1 >= len(lines):
            raise McpError(errors.INVALID_ARGUMENT,
                           f"编辑行区间越界: {rel} 行 {e['line']}-{e['end_line']}（文件共 {len(lines)} 行）",
                           "以 rename_preview 的最新编辑计划重试")
        prefix = lines[i0][: e["character"] - 1]
        suffix = lines[i1][e["end_character"]:]
        lines[i0:i1 + 1] = [prefix + e["new_text"] + suffix]
    return "".join(lines)


def _uri_rel_guarded(root: Path, uri) -> str:
    """WorkspaceEdit changes 键（file URI）→ 项目内相对路径；越界 URI → PATH_OUTSIDE_WORKSPACE。

    url2pathname 处理 Windows 盘符（intel.py 同款先例）；仅本地换算，无网络出站。
    """
    if not isinstance(uri, str) or not uri:
        raise McpError(errors.INVALID_ARGUMENT, "LSP rename 结果的 changes 键不是合法 file URI",
                       "重试 rename_preview，或改用 search_code 定位")
    abs_path = Path(url2pathname(unquote(urlparse(uri).path)) or "/")
    try:
        return abs_path.resolve().relative_to(root).as_posix()
    except ValueError:
        raise McpError(
            errors.PATH_OUTSIDE_WORKSPACE,
            f"重命名涉及项目根外路径，已拒绝: {abs_path.as_posix()!r}",
            "仅支持项目内 workspace 重构",
        )


def rename_preview(config: Config, project_id: str, path: str, line: int,
                   character: int, new_name: str) -> dict:
    """预览工作区级重命名（LSP textDocument/rename → 契约结构化，只读不写）。

    changes: uri→TextEdit[] 逐 URI 转相对路径 + 1-based 双闭区间；越界/敏感逐一路径守卫；
    rename 返回 null 或无 changes → files 空数组（EMPTY 态正常返回）。
    """
    _pos_arg(line, character)
    _arg(isinstance(new_name, str) and bool(new_name) and not any(c.isspace() for c in new_name),
         "new_name 必填且为不含空白字符的非空字符串")
    p, final, _, sess = _lsp_target(config, project_id, path)
    res = sess.request("textDocument/rename", {
        "textDocument": {"uri": final.as_uri()},
        "position": intel.to_lsp_position(line, character),
        "newName": new_name})
    changes = dict(res.get("changes") or {}) if isinstance(res, dict) else {}
    # Pyright returns TextDocumentEdit[]; tsls returns changes. Both are LSP.
    for document in (res.get("documentChanges") or []) if isinstance(res, dict) else []:
        if not isinstance(document, dict) or "textDocument" not in document:
            raise McpError(errors.INVALID_ARGUMENT, "语言服务器返回了文件创建、删除或移动操作。",
                           "当前仅支持已有文件的文本重命名，请在编辑器中处理文件操作。")
        uri = document["textDocument"].get("uri")
        _uri_rel_guarded(p.path.resolve(), uri)
        changes.setdefault(uri, []).extend(document.get("edits") or [])
    patterns = tuple(config.security["sensitive_patterns"])
    files_out: list[dict] = []
    edit_count = 0
    for uri, text_edits in sorted((changes or {}).items()):
        rel = _uri_rel_guarded(p.path.resolve(), uri)
        if is_sensitive(rel, patterns):
            raise McpError(errors.FILE_NOT_VISIBLE,
                           f"重命名涉及对 AI 不可见的敏感文件: {rel!r}", "该文件对 AI 不可见")
        edits = []
        for e in (text_edits or []):
            if not isinstance(e, dict):
                continue
            rng = e.get("range") or {}
            s, en = rng.get("start") or {}, rng.get("end") or {}
            el, ec = en.get("line", 0), en.get("character", 0)
            # LSP end 0-based 半开 → 契约 1-based 双闭：末覆盖字符 0-based ec-1 ↔ 1-based ec
            # （rename 编辑 range 恒非空，ec>=1；ec==0 兜底为同行 1，不改变实际重命名语义）
            end_line, end_char = (el + 1, ec) if ec > 0 else (max(el, 1), 1)
            edits.append({"line": s.get("line", 0) + 1, "character": s.get("character", 0) + 1,
                          "end_line": end_line, "end_character": end_char,
                          "new_text": e.get("newText", "")})
            edit_count += 1
        files_out.append({"path": rel, "edits": edits})
    return {"project_id": p.id, "files": files_out, "file_count": len(files_out),
            "edit_count": edit_count, "truncated": False}


def apply_workspace_edit(config: Config, project_id: str, files, expected_shas) -> dict:
    """多文件源码重构写：全 sha 预检通过才逐文件落盘（per-project 锁内，TOCTOU 防护）。

    安全链：write_enabled? → 逐文件 PathGuard（逃逸拒）+ 敏感过滤（绕过 docs 白名单但
    保留逃逸与敏感防护）→ 锁内全部 expected_shas 预检（缺失/不匹配 → REVISION_CONFLICT
    整体拒绝零落盘）+ 单文件结果 >1MB → PAYLOAD_TOO_LARGE → 逐文件 tmp+rename 原子写
    → 每文件审计（前后 sha，不含内容）。多文件非事务：预检全过后中途失败由 git 等外部手段恢复。
    """
    p = _project(config, project_id)
    if not p.write_enabled:
        raise McpError(
            errors.WRITE_DISABLED,
            f"项目 '{p.id}' 未开启受控写（write_enabled=false，默认关闭）",
            "告知用户在 config.toml 该项目下加 write_enabled: true",
        )
    _arg(isinstance(files, list) and bool(files), "files 必填且为非空数组")
    _arg(isinstance(expected_shas, dict), "expected_shas 必须是 {path: content_sha256} 对象")
    patterns = tuple(config.security["sensitive_patterns"])
    root = p.path.resolve()

    planned: list[tuple[Path, str, list[dict]]] = []
    seen: set[str] = set()
    for f in files:
        if (not isinstance(f, dict) or not isinstance(f.get("path"), str) or not f["path"]
                or not isinstance(f.get("edits"), list) or not f["edits"]
                or not all(_valid_edit(e) for e in f["edits"])):
            raise McpError(
                errors.INVALID_ARGUMENT,
                "files 每项必须是 {path, edits:[{line,character,end_line,end_character,new_text}]}，"
                "编辑坐标为 >=1 整数（1-based 双闭区间）且 end >= start",
                "按 schema 修正参数后重试")
        final = resolve_path(p, f["path"])          # 绝对/UNC/~ 直拒；.. 逃逸由 ∈root 兜底
        rel = final.relative_to(root).as_posix()
        if rel in seen:
            raise McpError(errors.INVALID_ARGUMENT, f"files 中路径重复: {rel!r}", "合并同文件编辑后重试")
        seen.add(rel)
        if is_sensitive(rel, patterns):
            raise McpError(errors.FILE_NOT_VISIBLE,
                           f"该路径对 AI 不可见（敏感过滤同样约束 workspace 重构）: {rel!r}", "该文件对 AI 不可见")
        planned.append((final, rel, f["edits"]))

    resolved_shas: dict[str, str] = {}
    for k, v in expected_shas.items():
        _arg(isinstance(k, str) and bool(k) and isinstance(v, str) and bool(v),
             "expected_shas 必须是 {path: 非空 content_sha256}（path 为项目内相对路径）")
        krel = resolve_path(p, k).relative_to(root).as_posix()   # 与 files 同一解析链（docs/../src 合法）
        if krel in resolved_shas:
            raise McpError(errors.INVALID_ARGUMENT, f"expected_shas 键解析后重复: {krel!r}", "去重后重试")
        resolved_shas[krel] = v
    _arg(seen == set(resolved_shas),
         "expected_shas 键集必须与 files 路径集完全一致（覆盖 files 中全部文件，不多不少）")

    from writer import MAX_WRITE_BYTES, _lock_for  # 局部导入避免环；复用 per-project 锁与 1MB 上限
    with _lock_for(p.id):                           # 预检+写入同一锁内（TOCTOU 防护）
        prepared: list[tuple[Path, str, bytes, str]] = []
        mismatched: list[str] = []
        for final, rel, edits in planned:
            sha_expected = resolved_shas[rel]
            _arg(isinstance(sha_expected, str) and bool(sha_expected),
                 f"expected_shas[{rel}] 必须是非空字符串")
            if not final.is_file():
                mismatched.append(rel)              # 文件不存在视作 sha 不匹配（预期哈希无法成立）
                continue
            raw = final.read_bytes()
            sha_now = hashlib.sha256(raw).hexdigest()
            if sha_now != sha_expected:
                mismatched.append(rel)
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise McpError(errors.INVALID_ARGUMENT,
                               f"文件不是有效的 UTF-8 文本，无法应用行区间编辑: {rel}", "仅支持 UTF-8 文本")
            payload = _apply_edits(text, edits, rel).encode("utf-8")
            if len(payload) > MAX_WRITE_BYTES:
                raise McpError(errors.PAYLOAD_TOO_LARGE,
                               f"应用编辑后 {rel} 结果超 1MB 上限（{len(payload)} 字节）", "拆分重构或手动处理")
            prepared.append((final, rel, payload, sha_now))
        if mismatched:
            raise McpError(
                errors.REVISION_CONFLICT,
                f"以下文件在 read 之后被修改（content_sha256 不匹配），整体拒绝零落盘: {', '.join(mismatched)}",
                "重新 read_file 获取各文件最新 content_sha256 后重试")
        applied: list[dict] = []
        for final, rel, payload, sha_before in prepared:
            final.parent.mkdir(parents=True, exist_ok=True)
            tmp = final.with_name(final.name + ".tmp-write")   # tmp + os.replace 原子写（writer 同款手法）
            try:
                tmp.write_bytes(payload)
                os.replace(tmp, final)
            finally:
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
            sha_after = hashlib.sha256(final.read_bytes()).hexdigest()
            log.info("tool=apply_workspace_edit project_id=%s path=%s bytes=%d sha_before=%s sha_after=%s result=%s",
                     p.id, rel, len(payload), sha_before, sha_after, "updated")
            applied.append({"path": rel, "bytes_written": len(payload), "sha256": sha_after})
    return {"project_id": p.id, "applied": applied, "files_changed": len(applied),
            "truncated": False}
