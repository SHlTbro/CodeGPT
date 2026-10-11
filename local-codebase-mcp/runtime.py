"""Owned MCP/tunnel processes and protocol-level network monitoring for local Setup.

The canonical file remains the input to server.py. This module never accepts commands,
process IDs or destinations from the browser and never stops an unrelated process.
"""
from __future__ import annotations

import asyncio
import builtins
import copy
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

from config import Config, ConfigStore, config_revision, config_from_data, load_config, _parse_bytes

BASE_DIR = Path(__file__).resolve().parent


class RuntimeError(builtins.RuntimeError):
    """An actionable failure suitable for the local Setup UI."""


def valid_ngrok_token(value: str) -> bool:
    """One format check for write-only Setup input and manually edited credential files."""
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{20,512}", value) is not None


def _local_url(config: Config) -> str:
    host = config.server.host
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{config.server.port}{config.server.path}"


def _ngrok_public_url(record: dict, config: Config) -> str:
    """Accept a started endpoint only when its observed upstream is our MCP origin.

    ngrok 3.39 logs addr as a Go net/url.URL object; older agents log a string.
    Both forms describe the same fixed origin and may carry no routing extras.
    """
    if record.get("msg") != "started tunnel":
        return ""
    origin = _local_url(config).removesuffix(config.server.path)
    addr = record.get("addr")
    if isinstance(addr, str):
        matches = addr == origin
    elif isinstance(addr, dict):
        expected = urlsplit(origin)
        matches = (addr.get("Scheme") == expected.scheme and addr.get("Host") == expected.netloc
                   and addr.get("Path", "") in {"", "/"}
                   and all(addr.get(key) in (None, "") for key in
                           ("Opaque", "User", "Fragment", "RawQuery", "RawPath", "RawFragment"))
                   and addr.get("ForceQuery", False) is False
                   and addr.get("OmitHost", False) is False)
    else:
        matches = False
    raw_url = record.get("url", "")
    if not matches or not isinstance(raw_url, str):
        return ""
    try:
        url = urlsplit(raw_url)
        valid = (url.scheme == "https" and bool(url.hostname)
                 and not url.username and not url.password and not url.query and not url.fragment
                 and url.path in {"", "/"} and not any(ord(c) < 32 for c in raw_url))
    except ValueError:
        return ""
    return f"https://{url.netloc}{config.server.path}" if valid else ""


def _tool_data(result) -> dict:
    data = getattr(result, "data", None)
    if data is None:
        data = getattr(result, "structured_content", None)
    if data is None and getattr(result, "content", None):
        data = json.loads(result.content[0].text)
    if not isinstance(data, dict) or "error" in data:
        raise RuntimeError("MCP list_projects 调用失败；检查本地 MCP 日志和配置后重试。")
    return data


async def _mcp_probe(url: str, *, projects: bool = False, timeout: float = 4) -> dict:
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    import httpx2
    started = time.perf_counter()
    async def capture_denial(response):
        if response.status_code != 403:
            return
        # MCP 2.x otherwise replaces this response with a generic protocol error.
        # Inspect only a bounded prefix; never publish or persist the HTML body.
        prefix = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=4096):
            prefix.extend(chunk[:16384 - len(prefix)])
            if len(prefix) >= 16384:
                break
        codes = re.findall(rb"\bERR_NGROK_(?:725|105|106|107|4018)\b", bytes(prefix))
        if codes:
            response.extensions["codegpt_ngrok_error"] = codes[-1].decode("ascii")
        raise httpx2.HTTPStatusError("MCP endpoint refused access.", request=response.request, response=response)

    # A VPN's proxy environment must not redirect our owned loopback listener.
    # Public probes continue to follow the user's existing network configuration.
    def factory(**kwargs):
        kwargs.setdefault("timeout", httpx2.Timeout(timeout))
        return httpx2.AsyncClient(**kwargs, event_hooks={"response": [capture_denial]},
                                 trust_env=urlsplit(url).hostname not in {"localhost", "127.0.0.1", "::1"})
    transport = StreamableHttpTransport(url, headers={"X-Setup-Probe": "1",
                                                     "ngrok-skip-browser-warning": "1"},
                                       httpx_client_factory=factory)
    async with Client(transport, timeout=timeout, init_timeout=timeout, mode="legacy") as client:
        initialized = client.initialize_result
        if initialized is None or initialized.server_info.name not in {"CodeGPT", "Code-GPT", "local-codebase-mcp"}:
            raise RuntimeError("该地址没有连接到 CodeGPT MCP；请检查 Tunnel 的目标端口和 MCP 路径。")
        if not await client.ping():
            raise RuntimeError("MCP ping 未通过；请检查后端进程和 Tunnel 连接。")
        result = {"latency_ms": round((time.perf_counter() - started) * 1000, 1)}
        if projects:
            result["projects"] = _tool_data(await client.call_tool("list_projects", {}))["projects"]
        return result


def probe_mcp(url: str, *, projects: bool = False, timeout: float = 4) -> dict:
    """Initialize and ping MCP; an arbitrary HTTP response is never considered online."""
    return asyncio.run(asyncio.wait_for(_mcp_probe(url, projects=projects, timeout=timeout), timeout * 2 + 1))


def _ngrok_http_failure(error: BaseException) -> str:
    """Find known provider denials without displaying exceptions, bodies or secrets."""
    import httpx2
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, httpx2.HTTPStatusError) and current.response.status_code == 403:
            code = current.response.extensions.get("codegpt_ngrok_error")
            if code in {"ERR_NGROK_725", "ERR_NGROK_105", "ERR_NGROK_106", "ERR_NGROK_107", "ERR_NGROK_4018"}:
                return Runtime._ngrok_failure(code)
        pending.extend(e for e in (current.__cause__, current.__context__) if e is not None)
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
    return ""


class _WindowsJob:
    """Kernel-owned cleanup: closing/crashing Setup terminates only its child process tree."""
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        class Basic(ctypes.Structure):
            _fields_ = [("ProcessTime", ctypes.c_int64), ("JobTime", ctypes.c_int64),
                        ("Flags", wintypes.DWORD), ("MinWorkingSet", ctypes.c_size_t),
                        ("MaxWorkingSet", ctypes.c_size_t), ("ActiveLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("Priority", wintypes.DWORD),
                        ("Scheduling", wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")]
        class Extended(ctypes.Structure):
            _fields_ = [("Basic", Basic), ("IO", IO), ("ProcessMemory", ctypes.c_size_t),
                        ("JobMemory", ctypes.c_size_t), ("PeakProcessMemory", ctypes.c_size_t),
                        ("PeakJobMemory", ctypes.c_size_t)]
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self._kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self._kernel.SetInformationJobObject.restype = wintypes.BOOL
        self._kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self._kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel.CloseHandle.restype = wintypes.BOOL
        self._handle = self._kernel.CreateJobObjectW(None, None)
        if not self._handle:
            raise RuntimeError("无法建立 MCP 进程生命周期保护；请重启 Setup 后重试。")
        info = Extended()
        info.Basic.Flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._kernel.SetInformationJobObject(self._handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            self.close()
            raise RuntimeError("无法配置 MCP 进程生命周期保护；请重启 Setup 后重试。")

    def assign(self, proc: subprocess.Popen) -> None:
        if not self._kernel.AssignProcessToJobObject(self._handle, int(proc._handle)):
            raise RuntimeError("无法把 MCP/Tunnel 加入 Setup 生命周期；为避免后台残留，启动已取消。")

    def abort(self, proc: subprocess.Popen) -> None:
        """Clean up this exact suspended process even if Popen cleanup failed."""
        from ctypes import wintypes
        kernel = self._kernel
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateProcess.restype = wintypes.BOOL
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        try:
            # A previous terminate may already be in progress even when poll has not
            # observed exit yet. The native wait below confirms the actual outcome.
            kernel.TerminateProcess(int(proc._handle), 1)
        finally:
            self.close()
        if kernel.WaitForSingleObject(int(proc._handle), 5000) != 0:
            raise RuntimeError("本次 MCP/Tunnel 启动进程尚未退出；请关闭 Setup 后重试。")

    def resume(self, proc: subprocess.Popen) -> None:
        """Run the initially suspended process only after it belongs to this Job."""
        import ctypes
        from ctypes import wintypes
        class ThreadEntry(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD),
                        ("thread_id", wintypes.DWORD), ("owner_pid", wintypes.DWORD),
                        ("base_priority", wintypes.LONG), ("delta_priority", wintypes.LONG),
                        ("flags", wintypes.DWORD)]
        kernel = self._kernel
        kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        kernel.Thread32First.restype = wintypes.BOOL
        kernel.Thread32Next.argtypes = kernel.Thread32First.argtypes
        kernel.Thread32Next.restype = wintypes.BOOL
        kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenThread.restype = wintypes.HANDLE
        kernel.GetProcessIdOfThread.argtypes = [wintypes.HANDLE]
        kernel.GetProcessIdOfThread.restype = wintypes.DWORD
        kernel.ResumeThread.argtypes = [wintypes.HANDLE]
        kernel.ResumeThread.restype = wintypes.DWORD
        snapshot = kernel.CreateToolhelp32Snapshot(0x4, 0)  # TH32CS_SNAPTHREAD
        if snapshot == ctypes.c_void_p(-1).value:
            raise RuntimeError("无法读取 MCP/Tunnel 启动线程；启动已取消，请重新打开 Setup。")
        try:
            entry = ThreadEntry()
            entry.size = ctypes.sizeof(entry)
            found = kernel.Thread32First(snapshot, ctypes.byref(entry))
            thread_ids = []
            while found:
                if entry.owner_pid == proc.pid:
                    thread_ids.append(entry.thread_id)
                entry.size = ctypes.sizeof(entry)
                found = kernel.Thread32Next(snapshot, ctypes.byref(entry))
            # CREATE_SUSPENDED has not run any process code; only its initial thread exists.
            if len(thread_ids) != 1:
                raise RuntimeError("MCP/Tunnel 启动线程状态不正确；启动已取消，请重新打开 Setup。")
            thread = kernel.OpenThread(0x802, False, thread_ids[0])  # SUSPEND_RESUME | QUERY_LIMITED_INFORMATION
            if not thread:
                raise RuntimeError("无法打开 MCP/Tunnel 启动线程；检查本机执行权限后重试。")
            try:
                if (kernel.GetProcessIdOfThread(thread) != proc.pid
                        or not self.contains(proc.pid) or kernel.ResumeThread(thread) != 1):
                    raise RuntimeError("无法安全恢复 MCP/Tunnel 启动；启动已取消，请重新打开 Setup。")
            finally:
                kernel.CloseHandle(thread)
        finally:
            kernel.CloseHandle(snapshot)

    def contains(self, pid: int) -> bool:
        import ctypes
        from ctypes import wintypes
        self._kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self._kernel.OpenProcess.restype = wintypes.HANDLE
        self._kernel.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
        self._kernel.IsProcessInJob.restype = wintypes.BOOL
        process = self._kernel.OpenProcess(0x1000, False, pid)
        if not process:
            return False
        try:
            included = wintypes.BOOL()
            return bool(self._kernel.IsProcessInJob(process, self._handle, ctypes.byref(included)) and included.value)
        finally:
            self._kernel.CloseHandle(process)

    def close(self, wait_pid: int | None = None) -> None:
        if getattr(self, "_handle", None):
            process = self._kernel.OpenProcess(0x100000, False, wait_pid) if isinstance(wait_pid, int) and wait_pid > 0 and self.contains(wait_pid) else None
            self._kernel.CloseHandle(self._handle)
            self._handle = None
            if process:
                from ctypes import wintypes
                self._kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
                self._kernel.WaitForSingleObject(process, 5000)
                self._kernel.CloseHandle(process)

    def __del__(self):
        self.close()


class Runtime:
    def __init__(self, config_path: Path | str, state_dir: Path | str,
                 probe_interval: float = 5.0, startup_timeout: float = 30.0, startup=None):
        self.config_path = Path(config_path).resolve()
        self.state_dir = Path(state_dir).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, _WindowsJob] = {}
        self.probe_interval = max(0.2, probe_interval)
        self.startup_timeout = startup_timeout
        # Setup injects the fixed OS integration; isolated runtimes never alter startup.
        self.startup = startup
        self._operation_lock = threading.RLock()
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._config: Config | None = None
        self._running_server = None
        self._server: subprocess.Popen | None = None
        self._tunnel: subprocess.Popen | None = None
        self._tunnel_config = None
        self._discovered_url = ""
        self._tunnel_connections: set[str] = set()
        self._last_tunnel_retry = 0.0
        self._external_error: str | None = None
        self._recovered_revision: str | None = None
        self._logs: dict[str, object] = {}
        self._log_threads: dict[str, threading.Thread] = {}
        self._history = deque(maxlen=1500)
        self._events = deque(maxlen=100)
        self._status = {"local": self._network_state(),
                        "public": self._network_state(),
                        "tunnel": {"mode": "off", "state": "disabled", "pid": None, "hint": "选择 Tunnel 后可获取公网 MCP URL。"},
                        "config": {"error": None, "applied_revision": None},
                        "activity": {"last_call": None, "count": 0, "recent": []}}

    @staticmethod
    def _network_state() -> dict:
        return {"state": "unconfigured", "url": "", "latency_ms": None,
                "last_disconnect": None, "last_reconnect": None, "hint": ""}

    def _event(self, kind: str, message: str) -> None:
        with self._lock:
            self._events.append({"time": time.time(), "kind": kind, "message": message})

    def _set_error(self, message: str | None) -> None:
        with self._lock:
            self._status["config"]["error"] = message

    def _spawn(self, key: str, argv: list[str], env: dict | None = None, redact: str = "") -> subprocess.Popen:
        log_file = (self.state_dir / f"{key}.log").open("w", encoding="utf-8")
        self._logs[key] = log_file
        kwargs = {"cwd": str(BASE_DIR), "stdin": subprocess.DEVNULL,
                  "stdout": subprocess.PIPE if redact else log_file, "stderr": subprocess.STDOUT, "env": env}
        if redact:
            kwargs.update(text=True, encoding="utf-8", errors="replace")
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | 0x4  # CREATE_SUSPENDED: assign Job before children can run.
        job = None
        try:
            job = _WindowsJob() if os.name == "nt" else None
            proc = subprocess.Popen(argv, **kwargs)
            try:
                if job is not None:
                    job.assign(proc)
                    job.resume(proc)
                    self._jobs[key] = job
            except Exception:
                try:
                    try:
                        proc.terminate()
                        proc.wait(timeout=5)
                    except Exception:
                        if job is None:
                            proc.kill()
                            proc.wait(timeout=5)
                        # Windows abort below uses the exact native process handle;
                        # it also handles failed assignment before Job ownership.
                finally:
                    try:
                        if job is not None:
                            job.abort(proc)
                    finally:
                        if proc.stdout is not None:
                            proc.stdout.close()
                        log_file.close()
                        self._logs.pop(key, None)
                raise
            if redact:
                def copy_redacted_log():
                    try:
                        for line in proc.stdout:
                            log_file.write(line.replace(redact, "[REDACTED]"))
                            log_file.flush()
                    except (OSError, ValueError):
                        pass
                    finally:
                        proc.stdout.close()
                reader = threading.Thread(target=copy_redacted_log, name=f"setup-{key}-log", daemon=True)
                self._log_threads[key] = reader
                reader.start()
            return proc
        except OSError:
            if job is not None and key not in self._jobs:
                job.close()
            log_file.close()
            self._logs.pop(key, None)
            raise RuntimeError(f"无法启动 {key}；检查安装文件和执行权限后重试。") from None

    def _stop_owned(self, key: str) -> None:
        proc = self._server if key == "server" else self._tunnel
        old_server = self._running_server if key == "server" else None
        actual_pid = self._read_json("runtime-state.json").get("pid") if key == "server" else None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        if key == "server":
            self._server = None
            self._running_server = None
        else:
            self._tunnel = None
        job = self._jobs.pop(key, None)
        if job is not None:
            job.close(actual_pid)  # Wait for the real worker, including a venv launcher descendant.
        if old_server is not None:
            # Windows can retain a listener briefly after process termination.
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try:
                    with socket.create_connection((old_server.host, old_server.port), timeout=0.2):
                        pass
                except OSError:
                    break
                time.sleep(0.05)
        reader = self._log_threads.pop(key, None)
        if reader:
            reader.join(timeout=3)
        stream = self._logs.pop(key, None)
        if stream:
            stream.close()

    @staticmethod
    def _ensure_port_available(config: Config) -> None:
        # Occupied ports belong to someone else. Do not adopt or terminate them.
        try:
            with socket.create_connection((config.server.host, config.server.port), timeout=0.3):
                raise RuntimeError(f"MCP 端口 {config.server.port} 已被占用；停止原服务或在配置中选择空闲端口。Setup 不会关闭外部进程。")
        except OSError:
            pass

    def _start_server(self, config: Config) -> None:
        self._ensure_port_available(config)
        env = dict(os.environ)
        if (BASE_DIR / "bin").is_dir():
            env["PATH"] = str(BASE_DIR / "bin") + os.pathsep + env.get("PATH", "")
        self._server = self._spawn("server", [sys.executable, str(BASE_DIR / "server.py"),
                                           "--config", str(self.config_path), "--state-dir", str(self.state_dir)], env)
        self._running_server = config.server
        self._event("server_start", f"Local MCP 已启动，进程 {self._server.pid}；正在验证协议。")

    def _read_json(self, name: str) -> dict:
        try:
            data = json.loads((self.state_dir / name).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _verify(self, config: Config) -> dict:
        expected = config_revision(config)
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if self._stop_event.is_set():
                raise RuntimeError("Setup 正在停止；配置应用已取消。")
            if self._server is None or self._server.poll() is not None:
                raise RuntimeError("Local MCP 启动失败；检查端口是否被占用、Python 依赖是否完整，然后重试。详情见本地 Setup 状态目录 server.log。")
            actual = self._read_json("runtime-state.json")
            actual_pid = actual.get("pid")
            job = self._jobs.get("server")
            owns_pid = actual_pid == self._server.pid or (job is not None and isinstance(actual_pid, int) and job.contains(actual_pid))
            if owns_pid and actual.get("revision") == expected:
                try:
                    result = probe_mcp(_local_url(config), projects=True)
                    expected_projects = [{"id": p.id, "name": p.name} for p in config.projects]
                    if result["projects"] == expected_projects:
                        return {"verified": True, "revision": expected, "projects": result["projects"]}
                except Exception:
                    pass
            self._stop_event.wait(0.15)
        raise RuntimeError("配置尚未在 MCP 中生效，保存失败；原配置会恢复。请检查 server.log，并确认仅有一套 Setup 管理此配置。")

    @staticmethod
    def _executable(name: str) -> str:
        candidate = BASE_DIR / (name + (".exe" if os.name == "nt" else ""))
        found = str(candidate) if candidate.is_file() else shutil.which(name)
        if not found:
            raise RuntimeError(f"找不到 {name}；把官方程序放入项目目录或安装到 PATH 后重试。")
        return found

    def _start_tunnel(self, config: Config) -> None:
        tunnel = config.tunnel
        self._last_tunnel_retry = time.monotonic()
        self._tunnel_config = tunnel
        self._discovered_url = ""
        self._tunnel_connections.clear()
        with self._lock:
            previous = self._status["tunnel"]
            self._status["tunnel"] = {"last_disconnect": previous.get("last_disconnect"), "last_reconnect": previous.get("last_reconnect"), "mode": tunnel.mode, "state": "disabled" if tunnel.mode == "off" else "starting",
                                      "pid": None, "hint": "Tunnel 未启用。" if tunnel.mode == "off" else "正在连接 Tunnel；公网 MCP 会单独探测。"}
        if tunnel.mode == "off":
            return
        if tunnel.mode == "named":
            raise RuntimeError("Named Tunnel 的远端 ingress 不能在此验证是否暴露 Setup。请改用 Cloudflare 临时 Tunnel 或 ngrok；两者由 Setup 固定指向 MCP 端口。")
        env = {key: value for key, value in os.environ.items()
               if not key.upper().startswith("TUNNEL_") and not key.upper().startswith("NGROK_")}
        if tunnel.mode == "cloudflare":
            # Explicit empty process config prevents inheriting ~/.cloudflared ingress.
            agent_config = self.state_dir / "cloudflared-agent.conf"
            agent_config.write_text("{}\n", encoding="utf-8")
            argv = [self._executable("cloudflared"), "tunnel", "--config", str(agent_config),
                    "--no-autoupdate", "--url",
                    _local_url(config).removesuffix(config.server.path), "--protocol", "http2", "--loglevel", "info"]
        elif tunnel.mode == "ngrok":
            token_path = self.config_path.parent / "ngrok-token.txt"
            if token_path.is_symlink():
                raise RuntimeError("ngrok 凭据文件不能是链接；移除本机 ngrok-token.txt 链接后，在配置页面重新启用。")
            try:
                token = token_path.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError):
                token = ""
            if not token:
                raise RuntimeError("缺少 ngrok 凭证；注册 ngrok 后复制 authtoken，在本机配置页面粘贴并启用。")
            if not valid_ngrok_token(token):
                raise RuntimeError("ngrok Authtoken 格式不正确；从账户页面复制完整凭据，在本机配置页面重新启用，不要粘贴命令或引号。")
            env["NGROK_AUTHTOKEN"] = token
            agent_config = self.state_dir / "ngrok-agent.conf"
            agent_config.write_text('version: "3"\nagent:\n  web_addr: false\n', encoding="utf-8")
            argv = [self._executable("ngrok"), "http", "--config", str(agent_config),
                    "--log", "stdout", "--log-format", "json", "--log-level", "info", "--inspect=false"]
            if tunnel.public_url:
                argv.append(f"--url=https://{urlsplit(tunnel.public_url).netloc}")
            # An omitted --url asks the account for its assigned domain. Only this
            # fixed MCP origin is forwarded; global ngrok config is never merged.
            argv.append(_local_url(config).removesuffix(config.server.path))
        else:
            raise RuntimeError("不支持的 Tunnel 类型；选择关闭、Cloudflare 或 ngrok。")
        self._tunnel = self._spawn("tunnel", argv, env, redact=token if tunnel.mode == "ngrok" else "")
        self._last_tunnel_retry = time.monotonic()
        # Reject immediate executable/credential failure while preserving local MCP.
        self._stop_event.wait(0.25)
        if self._tunnel.poll() is not None:
            raise RuntimeError(self._ngrok_failure(self._tunnel_log_tail()) if tunnel.mode == "ngrok"
                               else "Cloudflare 启动失败；检查本机 tunnel.log、外网连接和防火墙后重试。")
        self._event("tunnel_start", f"{tunnel.mode} Tunnel 已启动；目标仅为 Local MCP 端口 {config.server.port}。")

    def apply(self, config: Config, force_tunnel_restart: bool = False) -> dict:
        """Called after atomic canonical-file replacement; return only after real MCP verification."""
        with self._operation_lock:
            if self._stop_event.is_set():
                raise RuntimeError("Setup 正在停止；重新打开 setup.bat 后重试。")
            try:
                # A caller cannot apply an in-memory config without writing canonical TOML.
                if load_config(self.config_path) != config:
                    raise RuntimeError("配置文件已被另一处修改；刷新页面后重新保存。")
                restart = self._server is None or self._server.poll() is not None or self._running_server != config.server
                action = "start" if self._server is None else "restart" if restart else "hot_reload"
                tunnel_changed = (force_tunnel_restart or self._tunnel_config != config.tunnel
                                  or (self._running_server is not None and self._running_server != config.server)
                                  or (self._config and self._config.server != config.server)
                                  or (config.tunnel.mode != "off" and (self._tunnel is None or self._tunnel.poll() is not None)))
                if restart:
                    if (self._server is not None and self._server.poll() is None
                            and self._running_server is not None
                            and self._running_server.port != config.server.port):
                        # Reject a deterministically unusable listener before disturbing last-good.
                        self._ensure_port_available(config)
                    # An MCP crash does not change its listener. Keep a live
                    # tunnel to that origin so its public address/session survives.
                    if tunnel_changed:
                        self._stop_owned("tunnel")
                    self._stop_owned("server")
                    self._start_server(config)
                result = self._verify(config)
                if tunnel_changed:
                    self._stop_owned("tunnel")
                    self._start_tunnel(config)
                if self.startup is not None:
                    startup_result = self.startup.apply(config.startup.enabled)
                    if not isinstance(startup_result, dict) or startup_result.get("verified") is not True:
                        raise RuntimeError("Windows 开机启动设置未通过验证；检查当前用户权限和任务管理器中的启动应用后重试。")
                # Commit only after all lifecycle integrations succeed, so rollback sees last-good.
                self._config = config
                self._external_error = None
                self._recovered_revision = None
                self._set_error(None)
                self._event("config_applied", "配置已通过 MCP list_projects 验证。" if action == "hot_reload" else "Local MCP 已受控启动并验证配置。")
                self.probe()  # Return current URLs and real online state with the verified save.
                with self._lock:
                    self._status["config"]["applied_revision"] = result["revision"]
                return {"action": action, **result}
            except Exception as exc:
                self._set_error(str(exc))
                raise

    def start(self) -> dict:
        with self._operation_lock:
            try:
                config = load_config(self.config_path)
                result = self.apply(config)
            finally:
                if self._thread is None:
                    self._thread = threading.Thread(target=self._monitor, name="setup-runtime", daemon=True)
                    self._thread.start()
        # Never acquire ConfigStore's file lock while holding the lifecycle lock.
        ConfigStore(self.config_path).mark_verified(config)
        self.probe()
        return result

    def restart(self) -> dict:
        with self._operation_lock:
            config = load_config(self.config_path)
            self._stop_owned("server")
            result = self.apply(config)
            result["action"] = "restart"
        self.probe()
        return result

    def _monitor(self) -> None:
        pending_path = self.config_path.with_name(self.config_path.name + ".pending")
        while not self._stop_event.wait(self.probe_interval):
            # UI transactions own their candidate until synchronous apply/rollback finishes.
            if pending_path.exists():
                continue
            verified = None
            recovery = None
            with self._operation_lock:
                if pending_path.exists():
                    continue
                previous = self._config
                desired = None
                revision = None
                try:
                    # Parse exactly these bytes: the CAS recovery version must describe the
                    # same candidate we attempted, even when an external editor saves again.
                    raw = self.config_path.read_bytes()
                    # The writer creates pending before replacing canonical bytes. Recheck
                    # after reading to cover a transaction beginning after lock acquisition.
                    if pending_path.exists():
                        continue
                    revision = hashlib.sha256(raw).hexdigest()
                    desired = config_from_data(_parse_bytes(raw, self.config_path), self.config_path)
                    if desired != self._config or self._server is None or self._server.poll() is not None:
                        self.apply(desired)
                    elif desired.tunnel.mode != "off" and (self._tunnel is None or self._tunnel.poll() is not None):
                        if time.monotonic() - self._last_tunnel_retry >= 15:
                            self._stop_owned("tunnel")
                            self._start_tunnel(desired)
                        else:
                            raise RuntimeError(self._status["config"]["error"] or "Tunnel 未运行；等待自动重试，或检查 tunnel.log 后手动重启。")
                    if self._external_error and revision == self._recovered_revision:
                        self._set_error(self._external_error)
                    else:
                        self._external_error = None
                        self._recovered_revision = None
                        self._set_error(None)
                    verified = desired
                except Exception as exc:
                    self._set_error(str(exc))
                    if previous is not None and desired is not None and desired != previous:
                        recovery = (previous, revision, str(exc))
                self.probe()
            if recovery is not None:
                previous, failed_revision, error = recovery
                # ConfigStore -> Runtime is the same lock order used by UI transactions.
                # CAS avoids overwriting a newer edit or an in-progress UI save.
                try:
                    restored = ConfigStore(self.config_path).restore_verified(previous, failed_revision, self.apply)
                    if restored.get("restored"):
                        with self._operation_lock:
                            if (self._config == previous and hashlib.sha256(self.config_path.read_bytes()).hexdigest() == restored["revision"]):
                                self._recovered_revision = restored["revision"]
                                rejected = ConfigStore(self.config_path).rejected_path.name
                                self._external_error = f"外部配置未生效，已恢复 last-good：{error}。问题配置已保存在 {rejected}；修正配置后重试。"
                                self._set_error(self._external_error)
                        self._event("config_recovered", "外部配置应用失败，已恢复并验证 last-good MCP。")
                except Exception as exc:
                    self._set_error(f"外部配置应用失败：{error}；恢复 last-good 失败：{exc}")
            elif verified is not None:
                ConfigStore(self.config_path).mark_verified(verified)

    def _sample_network(self, key: str, url: str, verified: dict | None = None) -> None:
        old = self._status[key]
        if not url:
            result = {"state": "unconfigured", "url": "", "latency_ms": None, "hint": "尚未取得公网 MCP URL；检查 Tunnel 配置和启动状态。"}
        else:
            try:
                observed = verified if verified is not None else probe_mcp(url, timeout=3)
                result = {"state": "online", "url": url, "latency_ms": observed["latency_ms"], "hint": "MCP 初始化和 ping 已通过。"}
            except Exception as exc:
                hint = "Local MCP 协议不可达；检查本地进程、端口和配置。" if key == "local" else "公网 MCP 协议不可达；检查 Tunnel、域名/DNS 和外网连接。此结果是当前电脑的探测，ChatGPT 所在网络可能不同。"
                if key == "public":
                    hint = _ngrok_http_failure(exc) or hint
                result = {"state": "offline", "url": url, "latency_ms": None, "hint": hint}
        now = time.time()
        result["last_disconnect"] = old.get("last_disconnect")
        result["last_reconnect"] = old.get("last_reconnect")
        if old["state"] == "online" and result["state"] != "online":
            result["last_disconnect"] = now
            self._event(f"{key}_disconnect", f"{'Local MCP' if key == 'local' else 'Public URL'} 断连。")
        elif old["state"] != "online" and result["state"] == "online":
            result["last_reconnect"] = now
            self._event(f"{key}_reconnect", f"{'Local MCP' if key == 'local' else 'Public URL'} 已连接。")
        with self._lock:
            self._status[key] = result

    def _tunnel_log_tail(self) -> str:
        try:
            with (self.state_dir / "tunnel.log").open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                stream.seek(max(0, stream.tell() - 131072))
                return stream.read().decode("utf-8", errors="replace")
        except OSError:
            return ""

    @staticmethod
    def _ngrok_failure(text: str) -> str:
        """Publish only known codes and our own advice: ngrok may echo an authtoken."""
        codes = re.findall(r"ERR_NGROK_[0-9]+", text)
        code = codes[-1] if codes else ""
        if code == "ERR_NGROK_725":
            advice = "ngrok 本月数据传输额度已耗尽；在 ngrok Dashboard 检查用量，等待额度重置或调整套餐。保留现有域名与凭据；重启、换 URL 或重新粘贴 token 无法解除此限制。"
        elif code in {"ERR_NGROK_105", "ERR_NGROK_106", "ERR_NGROK_107", "ERR_NGROK_4018"} or (not code and "authentication failed" in text.lower()):
            advice = "ngrok 认证失败；从 ngrok Dashboard 重新复制完整 authtoken，在本机配置页面重新启用，并确认账号已完成验证。"
        elif code == "ERR_NGROK_108":
            advice = "ngrok 账号的同时在线 agent 数量已达上限；在 ngrok Dashboard 停止不用的 agent，再重新启用。Setup 不会停止外部进程。"
        elif code == "ERR_NGROK_334":
            advice = "ngrok 域名已有在线 endpoint；在 ngrok Dashboard 停止不用的 endpoint，或指定账号中可用的域名后重试。"
        else:
            advice = "ngrok Tunnel 未能连接；检查外网连接和 ngrok Dashboard 的账号限制后重新启用。自动分配地址不可用时，可填写账号中可用的完整 HTTPS MCP URL。"
        return f"{advice}（{code}）" if code else advice

    def verify_public(self, config: Config, timeout: float = 30) -> dict:
        """Onboarding succeeds only for this new owned tunnel and real public MCP tools."""
        with self._operation_lock:
            if self._config != config or load_config(self.config_path) != config:
                raise RuntimeError("配置在公网验证前已改变；刷新配置页面后重试。")
            if config.tunnel.mode == "off":
                raise RuntimeError("Tunnel 未启用；选择 ngrok 或 Cloudflare 后重试。")
            deadline = time.monotonic() + max(0, timeout)
            expected = [{"id": p.id, "name": p.name} for p in config.projects]
            while time.monotonic() < deadline:
                if self._stop_event.is_set():
                    raise RuntimeError("Setup 正在停止；公网验证已取消。")
                text = self._tunnel_log_tail()
                self._sample_tunnel(config, text)
                if self._tunnel is None or self._tunnel.poll() is not None:
                    raise RuntimeError(self._ngrok_failure(text) if config.tunnel.mode == "ngrok"
                                       else "Tunnel 已停止；检查外网连接后重新启用。")
                public_url = self._discovered_url
                if config.tunnel.mode == "cloudflare":
                    matches = re.findall(r"https://(?!api\.|www\.)[a-z0-9-]+\.trycloudflare\.com", text)
                    if matches:
                        public_url = matches[-1] + config.server.path
                        self._discovered_url = public_url
                if public_url and self._tunnel_connections:
                    try:
                        remaining = deadline - time.monotonic()
                        observed = probe_mcp(public_url, projects=True, timeout=min(3, max(0.1, remaining / 2)))
                    except Exception as exc:
                        denial = _ngrok_http_failure(exc)
                        if denial:
                            # Account quota/authentication cannot be healed by
                            # repeated requests or generating another public URL.
                            raise RuntimeError(denial) from None
                    else:
                        if observed["projects"] != expected:
                            if config.tunnel.mode == "cloudflare":
                                raise RuntimeError("Cloudflare 公网 MCP 的项目与本机配置不一致；重新启用临时隧道并使用本次生成的 URL，检查本机 MCP 端口和外网连接后重试。")
                            raise RuntimeError("公网 MCP 的项目与本机配置不一致；检查 ngrok 域名是否指向其他 endpoint，再重新启用。")
                        self._sample_network("public", public_url, verified=observed)
                        self._event("public_verified", "公网 MCP 初始化、ping 和 list_projects 已验证。")
                        return {"public_verified": True, "public_url": public_url,
                                "public_latency_ms": observed["latency_ms"]}
                self._stop_event.wait(min(0.25, max(0, deadline - time.monotonic())))
            # Refresh final observable state, without returning raw process output.
            self.probe()
            if config.tunnel.mode == "cloudflare":
                raise RuntimeError("Cloudflare 公网 MCP 验证未通过，启用失败；检查外网连接和本机 tunnel.log 后重试。临时隧道重新启用会生成新 URL，验证成功后重新复制到 ChatGPT。")
            raise RuntimeError("公网 MCP 验证未通过，启用失败；检查外网连接和 ngrok Dashboard 中的 endpoint。自动地址不可用时，可填写账号中可用的完整 HTTPS MCP URL 后重试。")

    def _sample_tunnel(self, config: Config, text: str) -> None:
        if config.tunnel.mode == "off":
            return
        alive = self._tunnel is not None and self._tunnel.poll() is None
        if config.tunnel.mode == "cloudflare":
            for line in text.splitlines():
                connector = re.search(r"connIndex=(\d+)", line)
                if connector:
                    if "Registered tunnel connection" in line:
                        self._tunnel_connections.add(connector[1])
                    elif any(message in line for message in ("Unregistered tunnel connection", "Connection terminated", "failed to serve tunnel connection", "Serve tunnel error")):
                        self._tunnel_connections.discard(connector[1])
        elif config.tunnel.mode == "ngrok":
            for line in text.splitlines():
                try:
                    record = json.loads(line)
                except ValueError:
                    continue  # Managed agents explicitly emit JSON; ignore diagnostics.
                if not isinstance(record, dict):
                    continue
                message = str(record.get("msg", "")).lower()
                if message == "started tunnel":
                    public_url = _ngrok_public_url(record, config)
                    if public_url:
                        self._discovered_url = public_url
                        self._tunnel_connections.add("ngrok")
                elif message in {"client session established", "tunnel session started"}:
                    self._tunnel_connections.add("ngrok")
                elif any(reason in message for reason in ("session closed", "failed to reconnect session", "connection closed")):
                    self._tunnel_connections.clear()
        with self._lock:
            tunnel = self._status["tunnel"]
            previous = tunnel["state"]
            tunnel["pid"] = self._tunnel.pid if alive else None
            tunnel["state"] = "connected" if alive and self._tunnel_connections else "running" if alive else "stopped"
            tunnel["hint"] = "Tunnel 已向服务商建立连接；公网 MCP 可达性仍单独探测。" if tunnel["state"] == "connected" else "Tunnel 进程在运行，尚未从真实日志确认服务商连接；检查 tunnel.log。" if alive else "Tunnel 进程未运行；检查 tunnel.log 后重试。"
            if config.tunnel.mode == "ngrok" and not alive:
                tunnel["hint"] = self._ngrok_failure(text)
            if previous == "connected" and tunnel["state"] != "connected":
                tunnel["last_disconnect"] = time.time()
                self._event("tunnel_disconnect", "Tunnel 服务商连接已断开。")
            elif previous != "connected" and tunnel["state"] == "connected":
                tunnel["last_reconnect"] = time.time()
                self._event("tunnel_reconnect", "Tunnel 已通过日志确认服务商连接。")

    def probe(self) -> dict:
        """Force one actual sample. Probes and configuration/process operations serialize."""
        with self._operation_lock:
            config = self._config
            if config is not None:
                self._sample_network("local", _local_url(config))
                text = self._tunnel_log_tail()
                if config.tunnel.mode == "cloudflare":
                    matches = re.findall(r"https://(?!api\.|www\.)[a-z0-9-]+\.trycloudflare\.com", text)
                    if matches:
                        self._discovered_url = matches[-1] + config.server.path
                self._sample_tunnel(config, text)
                # Quick Tunnel URLs belong to this owned generation. A configured
                # URL may be an old ngrok endpoint or a retired Cloudflare session.
                public_url = self._discovered_url
                if config.tunnel.mode != "cloudflare":
                    public_url = public_url or config.tunnel.public_url
                self._sample_network("public", public_url)
            activity = self._read_json("activity.json")
            with self._lock:
                if activity:
                    activity["recent"] = [e for e in activity.get("recent", []) if e.get("time", 0) >= time.time() - 300]
                    self._status["activity"] = activity
                now = time.time()
                self._history.append({"time": now, "local": self._status["local"]["state"],
                                      "public": self._status["public"]["state"],
                                      "local_latency_ms": self._status["local"]["latency_ms"],
                                      "public_latency_ms": self._status["public"]["latency_ms"]})
                while self._history and self._history[0]["time"] < now - 300:
                    self._history.popleft()
                return self.status()

    def status(self) -> dict:
        with self._lock:
            result = copy.deepcopy(self._status)
            alive = self._server is not None and self._server.poll() is None
            process_state = self._read_json("runtime-state.json") if alive else {}
            result["owner_pid"] = os.getpid()
            state_pid = process_state.get("pid")
            state_owned = alive and self._server is not None and state_pid == self._server.pid
            job = self._jobs.get("server")
            if not state_owned and job is not None and isinstance(state_pid, int):
                state_owned = job.contains(state_pid)
            result["local"]["pid"] = state_pid if alive and state_owned else (self._server.pid if alive else None)
            result["local"]["launcher_pid"] = self._server.pid if alive else None
            result["mcp"] = {
                "component": "mcp",
                "pid": state_pid if alive and state_owned else (self._server.pid if alive else None),
                "started_at": process_state.get("started_at") if alive and state_owned else None,
                "build_id": process_state.get("build_id") if alive and state_owned else None,
                "manifest_state": process_state.get("manifest_state") if alive and state_owned else None,
                "effective_config_version": process_state.get("revision") if alive and state_owned else None,
            }
            result["history"] = copy.deepcopy(list(self._history))
            result["events"] = copy.deepcopy(list(self._events))
            return result

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=10)
        with self._operation_lock:
            self._stop_owned("tunnel")
            self._stop_owned("server")
            with self._lock:
                self._status["local"]["state"] = "offline"
                self._status["tunnel"]["state"] = "stopped"
            self._event("stop", "Setup 管理的 MCP 和 Tunnel 已停止。")

