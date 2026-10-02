"""统一错误模型：{error:{code,message,recoverable,hint}}（契约 errorCodes 段唯一权威）。"""
from __future__ import annotations

from typing import Any

# 契约 errorCodes 全 18 个（v1.6 起，EXTENSION_NOT_ALLOWED 由 task24 落地）
PROJECT_NOT_FOUND = "PROJECT_NOT_FOUND"
INVALID_ARGUMENT = "INVALID_ARGUMENT"
PATH_OUTSIDE_WORKSPACE = "PATH_OUTSIDE_WORKSPACE"
FILE_NOT_VISIBLE = "FILE_NOT_VISIBLE"
FILE_NOT_FOUND = "FILE_NOT_FOUND"
FILE_TOO_LARGE = "FILE_TOO_LARGE"
BINARY_FILE_UNSUPPORTED = "BINARY_FILE_UNSUPPORTED"
SEARCH_BACKEND_ERROR = "SEARCH_BACKEND_ERROR"
GIT_ERROR = "GIT_ERROR"
CONFIG_ERROR = "CONFIG_ERROR"
INTERNAL_ERROR = "INTERNAL_ERROR"
# v1.2 新增（task13 受控写）
WRITE_PATH_FORBIDDEN = "WRITE_PATH_FORBIDDEN"
REVISION_CONFLICT = "REVISION_CONFLICT"
FILE_ALREADY_EXISTS = "FILE_ALREADY_EXISTS"
WRITE_DISABLED = "WRITE_DISABLED"
PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
# v1.3 新增（task17：LSP 基础设施）
LSP_UNAVAILABLE = "LSP_UNAVAILABLE"
# v1.6 新增（task24：write_binary_file 受控二进制写入）
EXTENSION_NOT_ALLOWED = "EXTENSION_NOT_ALLOWED"

# 契约规定的可恢复性，构造错误时不可自选
_RECOVERABLE = {
    PROJECT_NOT_FOUND: True,
    INVALID_ARGUMENT: True,
    PATH_OUTSIDE_WORKSPACE: True,
    FILE_NOT_VISIBLE: False,
    FILE_NOT_FOUND: True,
    FILE_TOO_LARGE: False,
    BINARY_FILE_UNSUPPORTED: False,
    SEARCH_BACKEND_ERROR: False,
    GIT_ERROR: False,
    CONFIG_ERROR: True,
    INTERNAL_ERROR: False,
    WRITE_PATH_FORBIDDEN: False,
    REVISION_CONFLICT: True,
    FILE_ALREADY_EXISTS: True,
    WRITE_DISABLED: False,
    PAYLOAD_TOO_LARGE: False,
    LSP_UNAVAILABLE: False,
    EXTENSION_NOT_ALLOWED: False,
}


def error_payload(code: str, message: str, hint: str | None = None) -> dict[str, Any]:
    """按契约形状构造错误响应体；code 必须是契约 errorCodes 中的合法错误码。"""
    if code not in _RECOVERABLE:
        raise ValueError(f"未知错误码: {code}")
    return {
        "error": {
            "code": code,
            "message": message or "（无错误描述）",
            "recoverable": _RECOVERABLE[code],
            "hint": hint or "",
        }
    }


class McpError(Exception):
    """工具层可抛出的契约错误，携带完整 error payload。"""

    def __init__(self, code: str, message: str, hint: str | None = None):
        super().__init__(message)
        self.payload = error_payload(code, message, hint)
