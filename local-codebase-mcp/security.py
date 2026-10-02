"""安全基座：PathGuard 路径守卫 + 敏感文件过滤 + 返回预算。

定位为 application-level path confinement，不是 OS sandbox。
"""
from __future__ import annotations

import os
import re
from fnmatch import fnmatch
from pathlib import Path, PureWindowsPath

import errors
from errors import McpError
from projects import Project

# 预算常量（契约 configSchema.limits）
MAX_FILE_SIZE_BYTES = 5_242_880
MAX_CHARS = 50_000
MAX_SEARCH_RESULTS = 100
LIST_FILES_MAX_ENTRIES = 2000

DEFAULT_SENSITIVE_PATTERNS = (".env", "*.key", "*.pem", "credentials*", ".ssh/**", ".aws/**")
DEFAULT_EXCLUDED_DIRS = (".git", "node_modules", "dist", "build", "__pycache__")

_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_SEARCH_LINE_MAX = 500


def _reject(relative: str, why: str) -> McpError:
    return McpError(
        errors.PATH_OUTSIDE_WORKSPACE,
        f"路径逃逸项目根，已拒绝: {relative!r}（{why}）",
        "改用项目内相对路径",
    )


def resolve_path(project: Project, relative: str) -> Path:
    """解析链固定：root.resolve() → normalize → resolve symlink/junction → casefold 前缀包含判定。

    绝对路径/盘符/UNC/~ 一律拒绝，禁止 ~ 展开；`..` 逃逸由最终包含判定兜底。
    """
    rel = relative if isinstance(relative, str) else str(relative)
    rel = rel.strip()
    if rel.startswith("~"):
        raise _reject(rel, "~ 路径被禁止")
    if rel.startswith("\\\\") or rel.startswith("//"):
        raise _reject(rel, "UNC 路径被禁止")
    if _DRIVE_RE.match(rel):
        raise _reject(rel, "盘符路径被禁止")
    if PureWindowsPath(rel).is_absolute():
        raise _reject(rel, "绝对路径被禁止")

    # casefold 前缀比较，防盘符/目录名大小写差异误判
    root = project.path.resolve()
    final = (root / rel).resolve()
    root_cf, final_cf = str(root).casefold(), str(final).casefold()
    prefix = root_cf if root_cf.endswith(("/", "\\")) else root_cf + os.sep
    if final_cf != root_cf and not final_cf.startswith(prefix):
        raise _reject(rel, "解析后不在项目根内")

    return final


def is_sensitive(rel_path: str, patterns: tuple[str, ...] | list[str] = DEFAULT_SENSITIVE_PATTERNS) -> bool:
    """按契约 sensitive_patterns 判定；.env.example 不匹配 .env（按字面）。

    分隔符统一为 /；无 / 的 pattern 同时匹配 basename（子目录下的 .env 同样视为敏感）。
    含 / 的 pattern（如 .ssh/**）按任意目录深度匹配（子目录下的 .ssh/id_rsa 同样视为敏感）。
    """
    norm = str(rel_path).replace("\\", "/")
    if norm.startswith("./"):
        norm = norm[2:]
    base = norm.rsplit("/", 1)[-1]
    return any(
        fnmatch(norm, p) or fnmatch(norm, "**/" + p) or ("/" not in p and fnmatch(base, p))
        for p in patterns
    )


def is_too_large(size_bytes: int, max_file_size_bytes: int = MAX_FILE_SIZE_BYTES) -> bool:
    """read 前置检查（GWT: 6MB 文件 → FILE_TOO_LARGE，上限 5MB）。"""
    return size_bytes > max_file_size_bytes


def truncate(text: str, max_chars: int = MAX_CHARS) -> tuple[str, bool]:
    """返回 (content, truncated)；超预算截断，由调用方把 truncated 写进响应。"""
    if len(text) > max_chars:
        return text[:max_chars], True
    return text, False


def truncate_search_line(text: str) -> str:
    """search_code 单行命中截断至 500 字符（契约 output 定义）。"""
    return text[:_SEARCH_LINE_MAX]


__all__ = [
    "MAX_FILE_SIZE_BYTES", "MAX_CHARS", "MAX_SEARCH_RESULTS", "LIST_FILES_MAX_ENTRIES",
    "DEFAULT_SENSITIVE_PATTERNS", "DEFAULT_EXCLUDED_DIRS",
    "resolve_path", "is_sensitive", "is_too_large", "truncate", "truncate_search_line",
]
