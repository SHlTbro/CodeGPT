"""受控写服务（契约 v1.2/v1.6/v1.7）：write_file（task13）+ write_binary_file（task24）全部逻辑。

安全链顺序不可变（proposal Delta 2 第八节）：
project_id → registry → write_enabled? → root.resolve() → normalize → resolve symlink/junction
→ final∈root（casefold）→ final relative ∈ 项目 write_roots（v1.7 前缀逐段匹配，默认 docs/**，
非 fnmatch 输入串）→ 非敏感（.env 等也写不进）→ 冲突检查（expected_absent / content_sha256）
→ 长度预算 → per-project 锁内预检 + 原子写（tmp+os.replace，崩溃残留不破坏既有文件）。
write_seq 为进程级 per-project 递增计数，重启归零可接受（sha256 才是权威新鲜度凭据）。
v1.6 write_binary_file 安全链同源，差异仅：扩展名白名单 + base64 解码 + 解码后 10MB 预算 + 二进制原子写。
v1.7 新增项目级 write_roots 可写目录白名单（config.toml projects[].write_roots，默认 ["docs"]）。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import itertools
import logging
import os
import threading
from pathlib import Path

import errors
from config import Config
from errors import McpError
from projects import Project
from security import is_sensitive, resolve_path
from tools_services import _project as _svc_project

# 契约 write_file input.content maxBytes
MAX_WRITE_BYTES = 1_048_576
# 契约 v1.6 write_binary_file notes：content_b64 解码后上限 10MB（口径=解码后字节数）
MAX_BINARY_BYTES = 10 * 1_048_576
# 契约 v1.6 扩展名白名单（大小写不敏感）
BINARY_EXTENSIONS = frozenset({".docx", ".pdf", ".xlsx", ".zip", ".pptx", ".doc", ".xls"})

# per-project 锁与写序号（进程内存态）
_LOCKS: dict[str, threading.Lock] = {}
_SEQS: dict[str, itertools.count] = {}
_SEQ_GUARD = threading.Lock()

log = logging.getLogger("local-codebase-mcp.tools")


def _lock_for(project_id: str) -> threading.Lock:
    with _SEQ_GUARD:
        return _LOCKS.setdefault(project_id, threading.Lock())


def _next_seq(project_id: str) -> int:
    with _SEQ_GUARD:
        return next(_SEQS.setdefault(project_id, itertools.count(1)))


def _arg(ok: bool, message: str) -> None:
    if not ok:
        raise McpError(errors.INVALID_ARGUMENT, message, "按 schema 修正参数后重试")


def _resolve_for_write(project: Project, relative: str) -> tuple[Path, str]:
    """写链路径解析：与读同源（security.resolve_path 同判定链），返回 (final, final rel / 分隔)。

    绝对路径/盘符/UNC/~ 拒绝；.. 逃逸与 symlink/junction 逃逸由 final∈root casefold 兜底。
    """
    final = resolve_path(project, relative)
    root = project.path.resolve()
    return final, final.relative_to(root).as_posix()


def _docs_whitelist(final_rel: str) -> None:
    """v1.2 固定 docs 白名单。v1.7 起调用点改走 _write_root_whitelist；本体保留向后兼容（不再被调用）。"""
    parts = final_rel.split("/")
    if not parts or parts[0].casefold() != "docs":
        raise McpError(
            errors.WRITE_PATH_FORBIDDEN,
            f"目标不在 docs/** 白名单: {final_rel!r}（白名单锚定项目根下顶层 docs/）",
            "放弃该写操作；可建议用户手动移动",
        )


def _write_root_whitelist(final_rel: str, write_roots: tuple[str, ...]) -> None:
    """v1.7 项目级可写目录白名单：final_rel 前缀路径逐段命中任一 root（casefold）即放行。

    write_roots 形如 ("docs", "30-cards", "wiki")，root 可为多级目录（docs/sub）；
    多 root 层级交叠时按声明序取最先命中（与 config 校验口径一致）。非 fnmatch 输入串。
    """
    parts = [seg.casefold() for seg in final_rel.split("/")]
    for root in write_roots:
        root_parts = [seg.casefold() for seg in root.split("/")]
        if parts[:len(root_parts)] == root_parts:
            return
    raise McpError(
        errors.WRITE_PATH_FORBIDDEN,
        f"目标不在项目可写目录白名单: {final_rel!r}（write_roots={list(write_roots)}）",
        "放弃该写操作；可在 config.toml 项目级 write_roots 扩展可写目录",
    )


def write_file(config: Config, project_id: str, path: str, content: str,
               expected_absent: bool = False, content_sha256: str | None = None,
               write_roots: tuple[str, ...] = ("docs",)) -> dict:
    """创建或更新项目可写目录（write_roots，默认 docs/）下 UTF-8 文本文件。
    返回 {project_id, path, bytes_written, sha256, created, write_seq}。"""
    p = _svc_project(config, project_id)                       # 安全链 1：registry
    if not p.write_enabled:                                    # 安全链 2：write_enabled?
        raise McpError(
            errors.WRITE_DISABLED,
            f"项目 '{p.id}' 未开启受控写（write_enabled=false，默认关闭）",
            "告知用户在 config.toml 该项目下加 write_enabled: true",
        )
    _arg(isinstance(path, str) and bool(path), "path 必填且为非空字符串（可写目录下相对路径）")
    _arg(isinstance(content, str), "content 必须是字符串")
    _arg(isinstance(expected_absent, bool), "expected_absent 必须是布尔值")
    _arg(content_sha256 is None or isinstance(content_sha256, str), "content_sha256 必须是字符串")
    payload = content.encode("utf-8")
    if len(payload) > MAX_WRITE_BYTES:                         # 安全链 8：长度预算（UTF-8 字节）
        raise McpError(errors.PAYLOAD_TOO_LARGE, f"content 超 1MB 上限（{len(payload)} 字节）", "缩小或拆分文档")

    final, final_rel = _resolve_for_write(p, path)             # 安全链 3-4：normalize + symlink/junction + ∈root
    _write_root_whitelist(final_rel, write_roots)              # 安全链 5：final rel ∈ write_roots/**（v1.7）
    if is_sensitive(final_rel, tuple(config.security["sensitive_patterns"])):
        raise McpError(errors.FILE_NOT_VISIBLE, f"该路径对 AI 不可见（敏感过滤同样约束写）: {final_rel!r}", "该文件对 AI 不可见")

    with _lock_for(p.id):                                      # 安全链 9：预检+写入在同一锁内，防 TOCTOU
        exists = final.is_file()
        sha_now = hashlib.sha256(final.read_bytes()).hexdigest() if exists else None
        if expected_absent and exists:                         # 安全链 7：冲突检查
            raise McpError(errors.FILE_ALREADY_EXISTS, f"文件已存在而 expected_absent=true: {final_rel}", "改为更新流程：read_file 取 content_sha256 后重写")
        if exists and content_sha256 is None:
            raise McpError(errors.INVALID_ARGUMENT, "更新已有文件必须带 content_sha256（read_file 返回的全文 hash）", "先 read_file 取 content_sha256 再重写")
        if content_sha256 is not None:
            _arg(bool(content_sha256), "content_sha256 必须为非空字符串")
            if sha_now != content_sha256:
                raise McpError(errors.REVISION_CONFLICT, f"文件在 read 之后被修改（content_sha256 不匹配）: {final_rel}", "重新 read_file 获取新 content_sha256 后重试")
        created = not exists
        final.parent.mkdir(parents=True, exist_ok=True)        # 安全链 10：原子写 tmp + os.replace
        tmp = final.with_name(final.name + ".tmp-write")
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
        seq = _next_seq(p.id)
    log.info("tool=write_file project_id=%s path=%s bytes=%d sha_before=%s sha_after=%s result=%s",
             p.id, final_rel, len(payload), sha_now or "-", sha_after, "created" if created else "updated")
    return {"project_id": p.id, "path": final_rel, "bytes_written": len(payload),
            "sha256": sha_after, "created": created, "write_seq": seq}


def _binary_extension_allowed(final_rel: str) -> None:
    """契约 v1.6 扩展名白名单：path 后缀（大小写不敏感）∈ {.docx .pdf .xlsx .zip .pptx .doc .xls}。"""
    ext = Path(final_rel).suffix.casefold()
    if ext not in BINARY_EXTENSIONS:
        raise McpError(
            errors.EXTENSION_NOT_ALLOWED,
            f"目标扩展名不在二进制白名单内: {final_rel!r}（仅支持 {'/'.join(sorted(BINARY_EXTENSIONS))}）",
            "仅支持 .docx/.pdf/.xlsx/.zip/.pptx/.doc/.xls；文本文件请用 write_file",
        )


def write_binary_file(config: Config, project_id: str, path: str, content_b64: str,
                      expected_absent: bool = False, content_sha256: str | None = None,
                      write_roots: tuple[str, ...] = ("docs",)) -> dict:
    """创建或更新项目可写目录（write_roots，默认 docs/）下白名单扩展名二进制文件（契约 v1.6/v1.7）。

    安全链与 write_file 同源（registry → write_enabled → resolve → write_roots 白名单 → 敏感过滤
    → 锁内冲突预检 → tmp+os.replace 原子写 → 审计），差异仅：
    扩展名白名单 / base64 严格解码 / 解码后 10MB 预算 / bytes 直写不经 text encoding。
    返回 {project_id, path, bytes_written(解码后), sha256(解码后 bytes hash), created, write_seq}。
    """
    p = _svc_project(config, project_id)                       # 安全链 1：registry
    if not p.write_enabled:                                    # 安全链 2：write_enabled?
        raise McpError(
            errors.WRITE_DISABLED,
            f"项目 '{p.id}' 未开启受控写（write_enabled=false，默认关闭）",
            "告知用户在 config.toml 该项目下加 write_enabled: true",
        )
    _arg(isinstance(path, str) and bool(path), "path 必填且为非空字符串（可写目录下相对路径）")
    _arg(isinstance(content_b64, str), "content_b64 必须是 base64 字符串")
    _arg(isinstance(expected_absent, bool), "expected_absent 必须是布尔值")
    _arg(content_sha256 is None or isinstance(content_sha256, str), "content_sha256 必须是字符串")
    try:                                                       # 安全链 0b：严格 base64 解码
        payload = base64.b64decode(content_b64, validate=True)
    except (binascii.Error, ValueError) as e:
        raise McpError(errors.INVALID_ARGUMENT, f"content_b64 不是合法 base64（{e}）", "按 schema 修正参数后重试") from e
    if len(payload) > MAX_BINARY_BYTES:                        # 安全链 8：解码后字节数预算
        raise McpError(errors.PAYLOAD_TOO_LARGE, f"解码后内容超 10MB 上限（{len(payload)} 字节）", "缩小或拆分文档")

    final, final_rel = _resolve_for_write(p, path)             # 安全链 3-4：normalize + symlink/junction + ∈root
    _write_root_whitelist(final_rel, write_roots)              # 安全链 5：final rel ∈ write_roots/**（v1.7）
    _binary_extension_allowed(final_rel)                       # 安全链 5b：扩展名白名单（v1.6）
    patterns = tuple(config.security["sensitive_patterns"])
    # 安全链 6：敏感过滤（同 write_file）+ 二进制防伪装：去掉白名单扩展名后的 stem 也不得命中
    #（默认 .env 为字面 pattern 不匹配 .env.pdf，白名单扩展名不得包裹敏感文件名绕过过滤）
    if is_sensitive(final_rel, patterns) or is_sensitive(Path(final_rel).stem, patterns):
        raise McpError(errors.FILE_NOT_VISIBLE, f"该路径对 AI 不可见（敏感过滤同样约束写）: {final_rel!r}", "该文件对 AI 不可见")

    with _lock_for(p.id):                                      # 安全链 9：预检+写入在同一锁内，防 TOCTOU
        exists = final.is_file()
        sha_now = hashlib.sha256(final.read_bytes()).hexdigest() if exists else None
        if expected_absent and exists:                         # 安全链 7：冲突检查（与 write_file 同语义）
            raise McpError(errors.FILE_ALREADY_EXISTS, f"文件已存在而 expected_absent=true: {final_rel}", "改为更新流程：read_file 取 content_sha256 后重写")
        if exists and not content_sha256:
            raise McpError(errors.INVALID_ARGUMENT, "更新已有文件必须带 content_sha256（read_file/write_binary_file 返回的二进制 hash）", "先 read_file 取 content_sha256 再重写")
        if content_sha256:
            if sha_now != content_sha256:
                raise McpError(errors.REVISION_CONFLICT, f"文件在 read 之后被修改（content_sha256 不匹配）: {final_rel}", "重新 read_file 获取新 content_sha256 后重试")
        created = not exists
        final.parent.mkdir(parents=True, exist_ok=True)        # 安全链 10：原子写 bytes + os.replace（不经 text encoding）
        tmp = final.with_name(final.name + ".tmp-write")
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
        seq = _next_seq(p.id)
    log.info("tool=write_binary_file project_id=%s path=%s bytes=%d sha_before=%s sha_after=%s result=%s",
             p.id, final_rel, len(payload), sha_now or "-", sha_after, "created" if created else "updated")
    return {"project_id": p.id, "path": final_rel, "bytes_written": len(payload),
            "sha256": sha_after, "created": created, "write_seq": seq}
