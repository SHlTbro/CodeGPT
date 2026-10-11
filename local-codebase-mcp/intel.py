"""stdio JSON-RPC language-server sessions used by MCP intelligence tools.

Public positions are 1-based; LSP positions are 0-based. Missing dependencies,
startup failures, timeouts and crashes return LSP_UNAVAILABLE. Runtime requests
never install software; setup.bat provisions the Python LSP dependency.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import os
import queue
import shutil
import sys
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname  # 仅 URI→路径换算（Windows 盘符语义），无网络出站

import errors
from errors import McpError

LSP_TIMEOUT_S = 30.0

# LSP_UNAVAILABLE 指引；工具调用不自动安装，允许 search_code 降级。
LSP_HINT = ("安装对应 language server（如 pyright-langserver / typescript-language-server）"
            "或改用 search_code")

# 探测表：标记文件（任一命中）→（语言键, 默认 argv）；config intel.servers[语言键] 覆盖优先
_DETECT = (
    (("pyproject.toml", "requirements.txt"), "python", ("pyright-langserver", "--stdio")),
    (("package.json",), "typescript", ("npx", "typescript-language-server", "--stdio")),
    (("go.mod",), "go", ("gopls",)),
    (("Cargo.toml",), "rust", ("rust-analyzer",)),
)

SOURCE_LANGUAGES = {".py": "python", ".pyi": "python", ".ts": "typescript",
                    ".tsx": "typescriptreact", ".js": "javascript",
                    ".jsx": "javascriptreact", ".mjs": "javascript", ".cjs": "javascript",
                    ".go": "go", ".rs": "rust"}

_SYMBOL_KINDS = {1: "File", 2: "Module", 3: "Namespace", 4: "Package", 5: "Class",
                 6: "Method", 7: "Property", 8: "Field", 9: "Constructor", 10: "Enum",
                 11: "Interface", 12: "Function", 13: "Variable", 14: "Constant"}
_SEVERITIES = {1: "error", 2: "warning", 3: "info", 4: "hint"}


def detect_server(project, config=None) -> list[str] | None:
    """按项目根标记文件探测语言服务器 argv；config.intel_servers 覆盖优先；未命中→None。"""
    overrides = getattr(config, "intel_servers", None) or {}
    for markers, lang, argv in _DETECT:
        if any((project.path / m).is_file() for m in markers):
            return list(overrides.get(lang) or argv)
    return None


def resolve_launch_argv(argv: list[str]) -> list[str]:
    """Use this interpreter's Pyright and resolve the Windows npx.cmd launcher.

    Custom command arrays remain unchanged; no install or shell command is exposed
    through MCP. setup.bat provisions the project-local Python LSP dependency.
    """
    if argv and argv[0] == "pyright-langserver":
        local = Path(sys.executable).with_name("pyright-langserver.exe" if os.name == "nt" else "pyright-langserver")
        if local.is_file():
            return [str(local), *argv[1:]]
    if os.name == "nt" and argv and argv[0] == "npx":
        resolved = shutil.which("npx")
        if resolved:
            return [resolved, *argv[1:]]
    return list(argv)


# ---- 1-based（对外）↔ 0-based（LSP）公共转换 ----

def to_lsp_position(line: int, character: int) -> dict:
    """契约 1-based 入参 → LSP position（0-based）。"""
    return {"line": line - 1, "character": character - 1}


def _uri_to_rel(project_root: Path, uri: str) -> str:
    # url2pathname 处理 Windows 盘符（file:///C:/x → C:\x），仅本地 URI 换算
    path = Path(url2pathname(unquote(urlparse(uri).path)))
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        return path.as_posix()  # 项目根外：保留绝对 posix 路径（越界拦截是 task18 职责）


def normalize_location(project_root: Path, loc) -> dict | None:
    """LSP location / locationLink（uri + 0-based）→ 契约输出（相对路径 + 1-based）。"""
    if not isinstance(loc, dict):
        return None
    uri = loc.get("uri") or loc.get("targetUri")
    if not isinstance(uri, str) or not uri:
        return None
    rng = loc.get("targetSelectionRange") or loc.get("range") or loc
    start = rng.get("start") or rng
    return {"path": _uri_to_rel(project_root, uri),
            "line": start.get("line", 0) + 1, "character": start.get("character", 0) + 1}


def normalize_symbol(project_root: Path, sym: dict) -> dict | None:
    """LSP workspace symbol → 契约输出（kind 数值转名称，位置 1-based）。"""
    loc = normalize_location(project_root, sym.get("location"))
    if loc is None:
        return None
    return {"name": sym.get("name", ""), "kind": _SYMBOL_KINDS.get(sym.get("kind"), "Unknown"), **loc}


def normalize_diagnostics(project_root: Path, uri: str, items) -> list[dict]:
    """LSP diagnostic items（0-based）→ 契约输出（severity 名称 + line 1-based）。"""
    rel = _uri_to_rel(project_root, uri)
    return [{"path": rel,
             "severity": _SEVERITIES.get(d.get("severity"), "info"),
             "line": ((d.get("range") or {}).get("start") or {}).get("line", 0) + 1,
             "message": d.get("message", ""), "source": d.get("source", "")}
            for d in (items or []) if isinstance(d, dict)]


class LspSession:
    """单个语言服务器子进程会话：同步 request + 独立守护读线程。"""

    def __init__(self, project, argv: list[str]):
        self._lock = threading.RLock()
        # Serialize frames and replies for this single stdio channel.
        # argv 数组直传（无 shell）；stderr 丢弃防阻塞；Windows npx 解析在唯一启动点统一生效
        self._proc = subprocess.Popen(resolve_launch_argv(argv), stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, cwd=str(project.path),
                                      creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self._msgs: queue.Queue = queue.Queue()
        self._ids = itertools.count(1)
        self._opened: dict[str, tuple[str, int]] = {}
        self.diagnostics: dict[str, list] = {}  # publishDiagnostics 暂存（task18 get_diagnostics 用）
        # 独立守护读线程做 Content-Length 帧解析：防服务器输出写满 stdout 管道导致双向死锁
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        try:
            self._handshake(project)
        except Exception:
            self.close()
            raise

    # ---- 内部 ----
    def _read_loop(self):
        f = self._proc.stdout
        try:
            while True:
                headers: dict[bytes, bytes] = {}
                while True:
                    line = f.readline()
                    if not line:
                        self._msgs.put(None)
                        return
                    if line in (b"\r\n", b"\n"):
                        break
                    k, _, v = line.partition(b":")
                    headers[k.strip().lower()] = v.strip()
                length = int(headers.get(b"content-length", b"0"))
                if length <= 0:
                    continue
                body = f.read(length)
                if len(body) < length:
                    self._msgs.put(None)
                    return
                self._msgs.put(json.loads(body.decode("utf-8")))
        except (OSError, ValueError):
            self._msgs.put(None)

    def _send(self, msg: dict):
        if self._proc.poll() is not None:
            raise McpError(errors.LSP_UNAVAILABLE, "语言服务器进程已退出", LSP_HINT)
        body = json.dumps(msg).encode("utf-8")
        try:
            self._proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
            self._proc.stdin.flush()
        except (OSError, ValueError):
            raise McpError(errors.LSP_UNAVAILABLE, "语言服务器管道已断开", LSP_HINT)

    def _handshake(self, project):
        self.request("initialize", {
            "processId": os.getpid(), "rootUri": project.path.as_uri(),
            "workspaceFolders": [{"uri": project.path.as_uri(), "name": project.name}],
            # tsls uses publishDiagnostics capability to enable asynchronous diagnostics.
            "capabilities": {"textDocument": {"publishDiagnostics": {}}},
        })
        self.notify("initialized", {})

    def _absorb(self, msg: dict):
        if msg.get("method") == "textDocument/publishDiagnostics":
            p = msg.get("params") or {}
            if isinstance(p.get("uri"), str):
                uri = self.document_uri(p["uri"])
                current = self._opened.get(uri)
                if current and isinstance(p.get("version"), int) and p["version"] < current[1]:
                    return
                self.diagnostics[uri] = p.get("diagnostics", [])

    # ---- 对 task18 的公共面 ----
    def request(self, method: str, params: dict, timeout: float = LSP_TIMEOUT_S):
        """同步请求：阻塞等同 id 响应；超时/进程崩溃/错误响应 → McpError(LSP_UNAVAILABLE)。"""
        with self._lock:
            return self._request(method, params, timeout)

    def _request(self, method, params, timeout):
        rid = next(self._ids)
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpError(errors.LSP_UNAVAILABLE,
                               f"语言服务器响应超时（{method} 超过 {timeout:.0f}s）", LSP_HINT)
            try:
                msg = self._msgs.get(timeout=remaining)
            except queue.Empty:
                continue
            if msg is None:
                raise McpError(errors.LSP_UNAVAILABLE, "语言服务器进程崩溃或意外退出", LSP_HINT)
            if msg.get("id") == rid and ("result" in msg or "error" in msg):
                if "error" in msg:
                    error = McpError(errors.LSP_UNAVAILABLE,
                                     "语言服务器返回错误: " + " ".join(msg["error"].get("message", "").splitlines()[:2])[:400], LSP_HINT)
                    error.lsp_code = msg["error"].get("code")
                    raise error
                return msg.get("result")
            self._absorb(msg)

    def notify(self, method: str, params: dict):
        with self._lock:
            self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def ensure_open(self, path: Path, language_id: str | None = None):
        """definition/references 前确保目标文件已 didOpen（LSP 语义要求），幂等。"""
        with self._lock:
            uri = path.resolve().as_uri()
            text = path.read_text(encoding="utf-8", errors="replace")
            stamp = hashlib.sha256(text.encode("utf-8")).hexdigest()
            previous = self._opened.get(uri)
            if previous and previous[0] == stamp:
                return
            version = previous[1] + 1 if previous else 1
            if previous:
                self.notify("textDocument/didChange", {"textDocument": {
                    "uri": uri, "version": version}, "contentChanges": [{"text": text}]})
            else:
                self.notify("textDocument/didOpen", {"textDocument": {
                    "uri": uri, "languageId": language_id or SOURCE_LANGUAGES.get(path.suffix.lower(), "plaintext"),
                    "version": version, "text": text}})
            self.diagnostics.pop(uri, None)
            self._opened[uri] = (stamp, version)

    def document_uri(self, uri: str) -> str:
        """Servers may encode drive colons or change Windows drive casing."""
        incoming = Path(url2pathname(unquote(urlparse(uri).path)))
        for opened in self._opened:
            if incoming == Path(url2pathname(unquote(urlparse(opened).path))):
                return opened
        return incoming.as_uri()

    def wait_diagnostics(self, uri: str, timeout: float = 10.0) -> list:
        """Wait for an actual publish, including a confirmed empty list."""
        with self._lock:
            uri = self.document_uri(uri)
            deadline = time.monotonic() + timeout
            while uri not in self.diagnostics:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise McpError(errors.LSP_UNAVAILABLE, "语言服务器尚未确认文件诊断。",
                                   "稍后重试 get_diagnostics；暂时不能把诊断未完成视为文件无错误。")
                try:
                    msg = self._msgs.get(timeout=remaining)
                except queue.Empty:
                    continue
                if msg is None:
                    raise McpError(errors.LSP_UNAVAILABLE, "语言服务器进程已退出。", LSP_HINT)
                self._absorb(msg)
            return list(self.diagnostics[uri])

    @property
    def pid(self) -> int | None:
        return self._proc.pid

    @property
    def healthy(self) -> bool:
        """进程存活且读线程未退出（EOF/解析失败都会终结读线程）。"""
        return self._proc.poll() is None and self._reader.is_alive()

    def close(self):
        """shutdown/exit 优雅收尾；残留则清理进程树。

        Windows 进程树清理方法：`taskkill /PID <pid> /T /F`（argv 数组直传不走 shell）——
        /T 连同整棵子树（如 npx→node 孙进程，proc.kill() 只杀直接子进程），/F 强制。
        """
        with self._lock:
            self._close()

    def _close(self):
        if self._proc.poll() is None:
            try:
                self._send({"jsonrpc": "2.0", "id": next(self._ids), "method": "shutdown"})
                self._send({"jsonrpc": "2.0", "method": "exit"})
                self._proc.wait(timeout=5)
            except (McpError, OSError, ValueError, subprocess.TimeoutExpired):
                pass
        if self._proc.poll() is None:
            subprocess.run(["taskkill", "/PID", str(self._proc.pid), "/T", "/F"],
                           capture_output=True, check=False, stdin=subprocess.DEVNULL)
        if self._proc.poll() is None:
            self._proc.kill()


class IntelManager:
    """per-project 会话缓存：lazy 首次启动、命中缓存复用不重启、崩溃后下次调用重启。

    空闲回收未做（MVP 会话常驻，随进程退出统一回收；如需可加 LRU+idle 关闭）。
    """

    def __init__(self, config):
        self._config = config
        self._sessions: dict[str, LspSession] = {}
        self._lock = threading.Lock()

    def get_session(self, project) -> LspSession:
        """取/启动项目会话；一切失败路径统一 McpError(LSP_UNAVAILABLE, hint=LSP_HINT)。"""
        with self._lock:
            sess = self._sessions.get(project.id)
            if sess is not None and sess.healthy:
                return sess
            if sess is not None:
                sess.close()
            argv = detect_server(project, self._config)
            if not argv:
                raise McpError(
                    errors.LSP_UNAVAILABLE,
                    f"项目 '{project.id}' 未命中语言服务器探测"
                    "（无 pyproject.toml/requirements.txt/package.json/go.mod/Cargo.toml 标记）",
                    LSP_HINT)
            try:
                sess = LspSession(project, argv)
            except McpError:
                raise
            except Exception as e:  # 命令不存在（FileNotFoundError/WinError 2）等启动失败统一降级
                raise McpError(errors.LSP_UNAVAILABLE,
                               f"项目 '{project.id}' 语言服务器启动失败: {argv[0]} ({e})",
                               LSP_HINT)
            self._sessions[project.id] = sess
            return sess

    def close_all(self):
        with self._lock:
            for sess in self._sessions.values():
                sess.close()
            self._sessions.clear()
