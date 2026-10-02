"""task17 LSP 基础设施（Option A 薄适配器）：stdio JSON-RPC 薄客户端。

供 task18 四智能工具消费；本模块不注册任何 MCP 工具。
行号口径：对外 1-based（契约），LSP 协议 0-based——转换只经本模块公共函数。
降级语义：未探测到/启动失败/请求超时/进程崩溃 → McpError(LSP_UNAVAILABLE)，
不抛 500、不影响既有 7 工具；hint 固定指引安装 + search_code（禁自动安装）。
"""
from __future__ import annotations

import itertools
import json
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname  # 仅 URI→路径换算（Windows 盘符语义），无网络出站

import errors
from errors import McpError

LSP_TIMEOUT_S = 30.0

# 契约 LSP_UNAVAILABLE 的固定 hint（禁自动安装；降级 search_code）
LSP_HINT = ("安装对应 language server（如 pyright-langserver / typescript-language-server）"
            "或改用 search_code")

# 探测表：标记文件（任一命中）→（语言键, 默认 argv）；config intel.servers[语言键] 覆盖优先
_DETECT = (
    (("pyproject.toml", "requirements.txt"), "python", ("pyright-langserver", "--stdio")),
    (("package.json",), "typescript", ("npx", "typescript-language-server", "--stdio")),
    (("go.mod",), "go", ("gopls",)),
    (("Cargo.toml",), "rust", ("rust-analyzer",)),
)

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
    """task21 缺陷 1 修复（task20 实测）：Windows CreateProcess 只补 .exe，npx 实为 npx.cmd，
    裸 "npx" 直调必败（WinError 2）。os.name=='nt' 且 argv 首元素为裸 "npx" 时替换为
    shutil.which("npx") 的解析结果（.cmd 绝对路径）；解析不到保持原样（交由启动失败统一降级）。
    非 Windows 一律原样返回。在唯一启动点 LspSession 应用，探测/配置覆盖两条路都受益。
    """
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
        # argv 数组直传（无 shell）；stderr 丢弃防阻塞；Windows npx 解析在唯一启动点统一生效
        self._proc = subprocess.Popen(resolve_launch_argv(argv), stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, cwd=str(project.path))
        self._msgs: queue.Queue = queue.Queue()
        self._ids = itertools.count(1)
        self._opened: set[str] = set()
        self.diagnostics: dict[str, list] = {}  # publishDiagnostics 暂存（task18 get_diagnostics 用）
        # 独立守护读线程做 Content-Length 帧解析：防服务器输出写满 stdout 管道导致双向死锁
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._handshake(project)

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
            "processId": None, "rootUri": project.path.as_uri(),
            # task21 缺陷 2 修复（task20 实测）：tsls v6 以 textDocument.publishDiagnostics
            # 客户端能力为诊断推送开关，声明最小集（对不支持的服务器无害）
            "capabilities": {"textDocument": {"publishDiagnostics": {}}},
        })
        self.notify("initialized", {})

    def _absorb(self, msg: dict):
        if msg.get("method") == "textDocument/publishDiagnostics":
            p = msg.get("params") or {}
            if isinstance(p.get("uri"), str):
                self.diagnostics[p["uri"]] = p.get("diagnostics", [])

    # ---- 对 task18 的公共面 ----
    def request(self, method: str, params: dict, timeout: float = LSP_TIMEOUT_S):
        """同步请求：阻塞等同 id 响应；超时/进程崩溃/错误响应 → McpError(LSP_UNAVAILABLE)。"""
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
                    raise McpError(errors.LSP_UNAVAILABLE,
                                   f"语言服务器返回错误: {msg['error'].get('message', '')}", LSP_HINT)
                return msg.get("result")
            self._absorb(msg)

    def notify(self, method: str, params: dict):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def ensure_open(self, path: Path, language_id: str = "python"):
        """definition/references 前确保目标文件已 didOpen（LSP 语义要求），幂等。"""
        uri = path.resolve().as_uri()
        if uri in self._opened:
            return
        text = path.read_text(encoding="utf-8", errors="replace")
        self.notify("textDocument/didOpen", {"textDocument": {
            "uri": uri, "languageId": language_id, "version": 1, "text": text}})
        self._opened.add(uri)

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
