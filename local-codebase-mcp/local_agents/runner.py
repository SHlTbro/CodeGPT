"""Owned Windows task trees; trusted launch resolver only, no MCP command API.

CreateProcess JOB_LIST closes the create/assign crash gap. A noninherited
KILL_ON_JOB_CLOSE job owns descendants; cleanup requires native job count zero.
Job Objects are lifecycle protection, not filesystem or credential isolation.
The capability gate defaults closed; direct model-key env injection is refused.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
from contextlib import contextmanager
import codecs
import os
import queue
from pathlib import Path
import re
import sqlite3
import subprocess
import threading
import time
from types import MappingProxyType
import uuid


class RunnerError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)  # No argv, environment, native error or paths.


@dataclass(frozen=True)
class LaunchSpec:
    argv: tuple[str, ...]
    cwd: str
    env: dict[str, str]
    timeout_seconds: float
    redactions: tuple[str, ...] = ()
    interactive: bool = False

    def __post_init__(self):
        object.__setattr__(self, "argv", tuple(self.argv))
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))
        object.__setattr__(self, "redactions", tuple(self.redactions))


class _Startup(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("reserved", wintypes.LPWSTR), ("desktop", wintypes.LPWSTR),
        ("title", wintypes.LPWSTR), *[(name, wintypes.DWORD) for name in
        ("x", "y", "width", "height", "chars_x", "chars_y", "fill", "flags")],
        ("show", wintypes.WORD), ("reserved_size", wintypes.WORD), ("reserved_data", ctypes.c_void_p),
        ("stdin", wintypes.HANDLE), ("stdout", wintypes.HANDLE), ("stderr", wintypes.HANDLE)]


class _StartupEx(ctypes.Structure):
    _fields_ = [("startup", _Startup), ("attributes", ctypes.c_void_p)]


class _ProcessInfo(ctypes.Structure):
    _fields_ = [("process", wintypes.HANDLE), ("thread", wintypes.HANDLE),
                ("pid", wintypes.DWORD), ("tid", wintypes.DWORD)]


class _BasicLimit(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64), ("flags", wintypes.DWORD),
        ("min_working", ctypes.c_size_t), ("max_working", ctypes.c_size_t), ("active_limit", wintypes.DWORD),
        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]


class _ExtendedLimit(ctypes.Structure):
    _fields_ = [("basic", _BasicLimit), ("io", ctypes.c_uint64 * 6),
        *[(name, ctypes.c_size_t) for name in ("process_memory", "job_memory", "peak_process", "peak_job")]]


class _Accounting(ctypes.Structure):
    _fields_ = [("times", ctypes.c_int64 * 4), ("faults", wintypes.DWORD), ("total", wintypes.DWORD),
                ("active", wintypes.DWORD), ("terminated", wintypes.DWORD)]


def _kernel():
    if os.name != "nt":
        raise RunnerError("CAPABILITY_UNAVAILABLE")
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": (wintypes.HANDLE, [ctypes.c_void_p, wintypes.LPCWSTR]),
        "OpenJobObjectW": (wintypes.HANDLE, [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]),
        "SetInformationJobObject": (wintypes.BOOL, [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]),
        "QueryInformationJobObject": (wintypes.BOOL, [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]),
        "TerminateJobObject": (wintypes.BOOL, [wintypes.HANDLE, wintypes.UINT]),
        "CloseHandle": (wintypes.BOOL, [wintypes.HANDLE]),
        "OpenProcess": (wintypes.HANDLE, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]),
        "GetProcessTimes": (wintypes.BOOL, [wintypes.HANDLE, *[ctypes.POINTER(wintypes.FILETIME)] * 4]),
        "WaitForSingleObject": (wintypes.DWORD, [wintypes.HANDLE, wintypes.DWORD]),
        "GetExitCodeProcess": (wintypes.BOOL, [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]),
        "ResumeThread": (wintypes.DWORD, [wintypes.HANDLE]),
        "InitializeProcThreadAttributeList": (wintypes.BOOL, [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t)]),
        "UpdateProcThreadAttribute": (wintypes.BOOL, [ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t,
            ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]),
        "DeleteProcThreadAttributeList": (None, [ctypes.c_void_p]),
        "CreateProcessW": (wintypes.BOOL, [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p,
            ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
            ctypes.POINTER(_StartupEx), ctypes.POINTER(_ProcessInfo)]),
    }
    for name, (result, arguments) in signatures.items():
        function = getattr(k, name)
        function.restype, function.argtypes = result, arguments
    return k


def _creation(k, handle):
    values = [wintypes.FILETIME() for _ in range(4)]
    if not k.GetProcessTimes(handle, *[ctypes.byref(value) for value in values]):
        raise RunnerError("CLEANUP_FAILED")
    return (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime


def process_identity(pid):
    """Read-only private identity; never terminate by caller-supplied PID."""
    if type(pid) is not int or pid <= 0:
        raise RunnerError("INVALID_ARGUMENT")
    k = _kernel()
    handle = k.OpenProcess(0x101000, False, pid)  # query-limited + synchronize
    if not handle:
        if ctypes.get_last_error() in (87, 1168):
            return None
        raise RunnerError("CLEANUP_FAILED")
    try:
        wait = k.WaitForSingleObject(handle, 0)
        if wait not in (0, 258):
            raise RunnerError("CLEANUP_FAILED")
        return {"creation_time": _creation(k, handle), "alive": wait == 258}
    finally:
        k.CloseHandle(handle)


class _Job:
    def __init__(self, name):
        self.k = _kernel()
        ctypes.set_last_error(0)
        self.handle = self.k.CreateJobObjectW(None, name)
        if not self.handle or ctypes.get_last_error() == 183:
            self.close()
            raise RunnerError("EXECUTION_FAILED")
        info = _ExtendedLimit()
        info.basic.flags = 0x2000  # kill on last close, no breakaway flags
        if not self.k.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            self.close()
            raise RunnerError("EXECUTION_FAILED")

    def active_count(self):
        info = _Accounting()
        if not self.handle or not self.k.QueryInformationJobObject(self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
            raise RunnerError("CLEANUP_FAILED")
        return info.active

    def terminate(self):
        if not self.handle or not self.k.TerminateJobObject(self.handle, 1):
            raise RunnerError("CLEANUP_FAILED")

    def close(self):
        if getattr(self, "handle", None):
            self.k.CloseHandle(self.handle)
            self.handle = None

    def __del__(self):
        self.close()


class _Process:
    def __init__(self, job, spec):
        import msvcrt
        k = self.k = job.k
        fds, attributes, initialized = [], None, False
        info = _ProcessInfo()
        try:
            if spec.interactive:
                stdin, input_write = os.pipe()
                fds.extend((stdin,input_write))
            else:
                stdin = os.open(os.devnull, os.O_RDONLY)
                fds.append(stdin)
            out_r, out_w = os.pipe()
            fds.extend((out_r, out_w))
            err_r, err_w = os.pipe()
            fds.extend((err_r, err_w))
            for fd in (stdin, out_w, err_w):
                os.set_inheritable(fd, True)
            handles = (wintypes.HANDLE * 3)(*[msvcrt.get_osfhandle(fd) for fd in (stdin, out_w, err_w)])
            jobs = (wintypes.HANDLE * 1)(job.handle)
            size = ctypes.c_size_t()
            k.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
            attributes = ctypes.create_string_buffer(size.value)
            if not k.InitializeProcThreadAttributeList(attributes, 2, 0, ctypes.byref(size)):
                raise RunnerError("EXECUTION_FAILED")
            initialized = True
            for kind, value in ((0x20002, handles), (0x2000D, jobs)):
                if not k.UpdateProcThreadAttribute(attributes, 0, kind, value, ctypes.sizeof(value), None, None):
                    raise RunnerError("CAPABILITY_UNAVAILABLE")
            startup = _StartupEx()
            startup.startup.cb = ctypes.sizeof(startup)
            startup.startup.flags = 0x101  # stdio + hidden
            startup.startup.stdin, startup.startup.stdout, startup.startup.stderr = handles
            startup.attributes = ctypes.cast(attributes, ctypes.c_void_p)
            command = ctypes.create_unicode_buffer(subprocess.list2cmdline(spec.argv))
            environment = ctypes.create_unicode_buffer("\0".join(key + "=" + value for key, value in
                sorted(spec.env.items(), key=lambda item: item[0].upper())) + "\0\0")
            flags = 0x80000 | 0x400 | 0x4 | 0x8000000  # extended, unicode, suspended, hidden
            if not k.CreateProcessW(spec.argv[0], command, None, None, True, flags, environment, spec.cwd,
                                    ctypes.byref(startup), ctypes.byref(info)):
                raise RunnerError("EXECUTION_FAILED")
            self.handle, self.thread, self.pid = info.process, info.thread, info.pid
            self.creation_time = _creation(k, self.handle)
            self.stdout = os.fdopen(out_r, "rb", buffering=0)
            fds.remove(out_r)
            self.stderr = os.fdopen(err_r, "rb", buffering=0)
            fds.remove(err_r)
            self.stdin = os.fdopen(input_write,"wb",buffering=0) if spec.interactive else None
            if spec.interactive:fds.remove(input_write)
        except BaseException:
            for stream in ("stdin", "stdout", "stderr"):
                pipe = getattr(self, stream, None)
                if pipe is not None:
                    pipe.close()  # Transferred FDs no longer belong to fds.
            # Creation (if successful) already belongs to job; never a PID kill.
            if info.process:
                job.terminate()
                k.WaitForSingleObject(info.process, 5000)
                k.CloseHandle(info.process)
                k.CloseHandle(info.thread)
            raise
        finally:
            if initialized:
                k.DeleteProcThreadAttributeList(attributes)
            for fd in fds:
                os.close(fd)

    def resume(self):
        if self.k.ResumeThread(self.thread) != 1:
            raise RunnerError("EXECUTION_FAILED")
        self.k.CloseHandle(self.thread)
        self.thread = None

    def poll(self):
        wait = self.k.WaitForSingleObject(self.handle, 0)
        if wait == 258:
            return None
        if wait != 0:
            raise RunnerError("CLEANUP_FAILED")
        code = wintypes.DWORD()
        if not self.k.GetExitCodeProcess(self.handle, ctypes.byref(code)):
            raise RunnerError("CLEANUP_FAILED")
        return code.value

    def close(self):
        for stream in ("stdin", "stdout", "stderr"):
            pipe = getattr(self, stream, None)
            if pipe is not None:
                pipe.close()
        for name in ("thread", "handle"):
            value = getattr(self, name, None)
            if value:
                self.k.CloseHandle(value)
                setattr(self, name, None)


class _Redactor:
    def __init__(self, secrets):
        self.pattern = re.compile("|".join(re.escape(value) for value in sorted(secrets, key=len, reverse=True))) if secrets else None
        self.keep = max(map(len, secrets), default=1) - 1
        self.pending, self.redacted = "", False

    def feed(self, text, final=False):
        value = self.pending + text
        cut = len(value) if final else max(0, len(value) - self.keep)
        end, pieces = 0, []
        if self.pattern:
            for match in self.pattern.finditer(value):
                if match.start() >= cut:
                    break
                pieces.extend((value[end:match.start()], "[REDACTED]"))
                end, cut, self.redacted = match.end(), max(cut, match.end()), True
        pieces.append(value[end:cut])
        self.pending = value[cut:]
        return "".join(pieces)


@dataclass
class _Live:
    job: _Job
    process: _Process
    spec: LaunchSpec
    started: float = field(default_factory=time.monotonic)
    done: threading.Event = field(default_factory=threading.Event)
    readers: list = field(default_factory=list)
    tails: dict = field(default_factory=lambda: {"stdout": "", "stderr": ""})
    truncated: bool = False
    truncated_streams: set = field(default_factory=set)
    redacted: bool = False
    logs_error: bool = False
    observer_pending: int = 0
    observer_closed: bool = False
    observer_deadline: float | None = None
    stop_reason: str | None = None
    outcome: str | None = None
    exit_code: int | None = None


class Runner:
    """Scheduler executor seam. Resolver/gate are trusted service functions.

    start returns promptly; wait/result/identity are private local-service APIs.
    Recovery checks dead creator identity; it never terminates a reopened PID.
    on_exit may call Scheduler.finish only after the native cleanup proof.
    on_output is a trusted observer on one bounded dispatcher per Runner.
    Delivery failure/overflow/deadline forbids success, but cannot block native
    cleanup. Cancellation/close stop new delivery; an in-flight Python callback
    cannot be forcibly killed. This is neither durable logging nor isolation.
    """
    def __init__(self, state_dir, resolver, *, capability_gate=None, on_output=None, on_exit=None, on_started=None, fault_hook=None):
        self.state_dir = Path(state_dir).absolute()
        self.resolver, self.gate = resolver, capability_gate or (lambda task, spec: False)
        self.on_output, self.on_exit, self.fault_hook = on_output, on_exit, fault_hook
        self.on_started=on_started
        self.lock, self.live = threading.RLock(), {}
        self._observations = queue.Queue(maxsize=256)  # <= 256 * 4096 chars.
        self._dispatcher = None
        self._closed = False
        self.creator = process_identity(os.getpid())
        try:
            if self.state_dir.is_symlink():
                raise RunnerError("STORAGE_UNAVAILABLE")
            self.state_dir.mkdir(parents=True, exist_ok=True)
            self.database_path = self.state_dir / "ownership.sqlite"
            with self._connect() as connection:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not tables:
                    connection.execute("CREATE TABLE meta(version TEXT NOT NULL)")
                    connection.execute("INSERT INTO meta VALUES('1')")
                    connection.execute("""CREATE TABLE runs(task_id TEXT PRIMARY KEY,job_name TEXT NOT NULL,
                        creator_pid INTEGER NOT NULL,creator_time INTEGER NOT NULL,pid INTEGER,creation_time INTEGER,
                        phase TEXT NOT NULL,outcome TEXT,clean INTEGER NOT NULL)""")
                else:
                    self._check_layout(connection)
        except (OSError, sqlite3.Error):
            raise RunnerError("STORAGE_UNAVAILABLE") from None

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=10)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _check_layout(connection):
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables != {"meta", "runs"} or [tuple(row) for row in connection.execute("SELECT version FROM meta")] != [("1",)]:
            raise RunnerError("SCHEMA_UNSUPPORTED")
        expected = {"meta": ("version",), "runs": ("task_id", "job_name", "creator_pid", "creator_time",
                    "pid", "creation_time", "phase", "outcome", "clean")}
        for table, columns in expected.items():
            info = list(connection.execute(f"PRAGMA table_info({table})"))
            if tuple(row[1] for row in info) != columns or tuple(row[1] for row in info if row[5]) != (("task_id",) if table == "runs" else ()):
                raise RunnerError("SCHEMA_UNSUPPORTED")

    def _rows(self):
        try:
            with self._connect() as connection:
                self._check_layout(connection)
                connection.row_factory = sqlite3.Row
                rows = [dict(row) for row in connection.execute("SELECT * FROM runs")]
            for row in rows:
                if type(row["task_id"]) is not str or not 1 <= len(row["task_id"]) <= 128 or not re.fullmatch(r"Local\\CodeGPTAgent-[0-9a-f]{32}", row["job_name"]) or row["phase"] not in {"prepared", "owned", "finished"} or row["clean"] not in (0, 1) or any(
                        type(row[key]) is not int or row[key] <= 0 for key in ("creator_pid", "creator_time")) or (row["pid"] is None) != (row["creation_time"] is None) or any(
                        row[key] is not None and (type(row[key]) is not int or row[key] <= 0) for key in ("pid", "creation_time")) or (
                        row["phase"] == "finished") != bool(row["clean"]) or row["outcome"] not in {None, "succeeded", "failed", "cancelled", "timed_out", "interrupted"} or (
                        row["clean"] and row["outcome"] is None) or (
                        row["phase"] == "prepared" and (row["outcome"] is not None or row["pid"] is not None)) or (
                        row["phase"] == "owned" and row["outcome"] not in {None, "failed"}):
                    raise RunnerError("STORAGE_UNAVAILABLE")
            return {row["task_id"]: row for row in rows}
        except (sqlite3.Error, OSError, TypeError, KeyError):
            raise RunnerError("STORAGE_UNAVAILABLE") from None

    def _update(self, task_id, **values):
        try:
            with self._connect() as connection:
                self._check_layout(connection)
                connection.execute("UPDATE runs SET " + ",".join(key + "=?" for key in values) + " WHERE task_id=?",
                                   (*values.values(), task_id))  # Static internal callers only.
        except (OSError, sqlite3.Error):
            raise RunnerError("STORAGE_UNAVAILABLE") from None

    @staticmethod
    def _validate_spec(spec):
        if not isinstance(spec, LaunchSpec) or type(spec.interactive) is not bool or not spec.argv or any(type(value) is not str or not value or "\0" in value for value in spec.argv) or (
                not Path(spec.argv[0]).is_absolute() or Path(spec.argv[0]).suffix.lower() != ".exe") or not Path(spec.cwd).is_absolute() or not Path(spec.cwd).is_dir() or (
                type(spec.timeout_seconds) not in (int, float) or not 0 < spec.timeout_seconds <= 7200) or any(
                type(key) is not str or not key or "=" in key or "\0" in key or type(value) is not str or "\0" in value for key, value in spec.env.items()) or any(
                type(value) is not str or not 1 <= len(value) <= 4096 for value in spec.redactions):
            raise RunnerError("INVALID_ARGUMENT")
        if any(key.upper() in {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AZURE_OPENAI_API_KEY", "CODEX_API_KEY"} for key in spec.env):
            raise RunnerError("CREDENTIAL_UNAVAILABLE")
        if any(secret in argument for secret in spec.redactions for argument in spec.argv):
            raise RunnerError("CREDENTIAL_UNAVAILABLE")

    def start(self, task, task_input):
        task_id = task.get("task_id")
        if type(task_id) is not str or not 1 <= len(task_id) <= 128:
            raise RunnerError("INVALID_ARGUMENT")
        name = "Local\\CodeGPTAgent-" + uuid.uuid4().hex
        with self.lock:
            if self._closed:
                raise RunnerError("CAPABILITY_UNAVAILABLE")
            try:
                with self._connect() as connection:
                    self._check_layout(connection)
                    connection.execute("INSERT INTO runs VALUES(?,?,?,?,NULL,NULL,'prepared',NULL,0)",
                        (task_id, name, os.getpid(), self.creator["creation_time"]))
            except sqlite3.IntegrityError as error:
                raise RunnerError("REQUEST_CONFLICT" if error.sqlite_errorcode == sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY else "STORAGE_UNAVAILABLE") from None
            except (sqlite3.Error, OSError):
                raise RunnerError("STORAGE_UNAVAILABLE") from None
            job, process = None, None
            try:
                spec = self.resolver(task, task_input)
                self._validate_spec(spec)
                if self.gate(task, spec) is not True:
                    raise RunnerError("CAPABILITY_UNAVAILABLE")
                job = _Job(name)
                process = _Process(job, spec)
                identity = {"pid": process.pid, "creation_time": process.creation_time}
                if self.fault_hook:
                    self.fault_hook("after_create", identity)
                self._update(task_id, pid=process.pid, creation_time=process.creation_time, phase="owned")
                if self.fault_hook:
                    self.fault_hook("after_identity_commit", identity)
                live = _Live(job, process, spec)
                self.live[task_id] = live
                for stream in ("stdout", "stderr"):
                    reader = threading.Thread(target=self._drain, args=(task_id, live, stream), daemon=True)
                    live.readers.append(reader)
                    reader.start()
                process.resume()
                if self.on_started:self.on_started(task_id)
                if self.fault_hook:
                    self.fault_hook("after_resume", identity)
                threading.Thread(target=self._monitor, args=(task_id, live), daemon=True).start()
            except BaseException as error:
                clean = job is None
                if job is not None:
                    try:
                        job.terminate()
                        deadline = time.monotonic() + 5
                        while job.active_count() and time.monotonic() < deadline:
                            time.sleep(.01)
                        clean = job.active_count() == 0
                    except Exception:
                        clean = False
                    finally:
                        job.close()
                if process is not None:
                    process.close()
                self._update(task_id, phase="finished" if clean else "owned", outcome="failed", clean=int(clean))
                if clean and task_id in self.live:
                    self.live[task_id].outcome = "failed"
                    self.live[task_id].done.set()
                raise RunnerError(error.code if isinstance(error, RunnerError) else "EXECUTION_FAILED") from None

    def send_input(self,task_id,data):
        """Trusted worker-only pipe. Never exposed as an MCP/Setup command API."""
        if not isinstance(data,bytes) or not 1<=len(data)<=65536:raise RunnerError("INVALID_ARGUMENT")
        with self.lock:
            live=self.live.get(task_id)
            if not live or live.done.is_set() or live.stop_reason or not live.spec.interactive:
                raise RunnerError("CAPABILITY_UNAVAILABLE")
            pipe=live.process.stdin
            if pipe is None or pipe.closed:raise RunnerError("CAPABILITY_UNAVAILABLE")
        # Do not hold lifecycle lock during potentially blocking OS pipe IO;
        # owned Job cancellation/timeout still terminates the reader process.
        try:
            view=memoryview(data)
            while view:
                written=pipe.write(view)
                if not written:raise OSError
                view=view[written:]
        except (OSError,ValueError):raise RunnerError("EXECUTION_FAILED") from None

    def close_input(self,task_id):
        with self.lock:
            live=self.live.get(task_id)
            if not live:raise RunnerError("CAPABILITY_UNAVAILABLE")
            pipe=live.process.stdin
        if pipe is not None:pipe.close()

    def _drain(self, task_id, live, stream):
        redactor = _Redactor(live.spec.redactions)
        decoder = codecs.getincrementaldecoder("utf8")("replace")
        def emit(text):
            with self.lock:
                value = live.tails[stream] + text
                live.truncated |= len(value) > 32768
                if len(value) > 32768:
                    live.truncated_streams.add(stream)
                live.tails[stream] = value[-32768:]
                live.redacted |= redactor.redacted
            if self.on_output and text:
                self._observe(task_id, live, stream, text)
        try:
            with getattr(live.process, stream) as pipe:
                while chunk := pipe.read(4096):
                    emit(redactor.feed(decoder.decode(chunk)))
                emit(redactor.feed(decoder.decode(b"", final=True), final=True))
        except Exception:
            live.logs_error = True

    def _observe(self, task_id, live, stream, text):
        with self.lock:
            if self._closed or live.observer_closed:
                live.logs_error = True
                return
            if self._dispatcher is None:
                self._dispatcher = threading.Thread(target=self._dispatch_output, daemon=True)
                self._dispatcher.start()
            for offset in range(0, len(text), 4096):
                try:
                    self._observations.put_nowait((task_id, live, stream, text[offset:offset+4096]))
                    live.observer_pending += 1
                except queue.Full:
                    live.logs_error = True
                    live.observer_closed = True
                    break

    def _dispatch_output(self):
        while True:
            try:
                task_id, live, stream, text = self._observations.get(timeout=.1)
            except queue.Empty:
                if self._closed:
                    return
                continue
            with self.lock:
                deliver = not self._closed and not live.observer_closed
                # This claim is the delivery start. stop does not wait for an
                # already claimed callback; queued callbacks are never claimed.
            try:
                if deliver:
                    self.on_output(task_id, stream, text)
            except BaseException:
                with self.lock:
                    live.logs_error = True
                    live.observer_closed = True
            finally:
                with self.lock:
                    live.observer_pending -= 1
                    if not deliver:
                        live.logs_error = True
                self._observations.task_done()

    def _observer_finished(self, live):
        with self.lock:
            if not live.observer_pending:
                return True
            if live.observer_closed:
                live.logs_error = True
                return True
            now = time.monotonic()
            if live.observer_deadline is None:
                live.observer_deadline = now + 1.0
            if now < live.observer_deadline:
                return False
            live.logs_error = True
            live.observer_closed = True
            return True

    def _monitor(self, task_id, live):
        while True:
            if live.job.handle is None:
                # Explicit close after unknown cleanup stops this watcher;
                # the durable record stays unconfirmed for restart recovery.
                live.process.close()
                return
            try:
                code = live.process.poll()
                with self.lock:
                    if code is None and time.monotonic() - live.started >= live.spec.timeout_seconds and live.stop_reason is None:
                        live.stop_reason = "timed_out"
                        live.observer_closed = True
                        if live.observer_pending:
                            live.logs_error = True
                    if live.stop_reason is not None and code is None:
                        live.job.terminate()
                    if code is not None and live.job.active_count():
                        live.job.terminate()  # Parent exit does not prove descendants exited.
                if code is not None and live.job.active_count() == 0 and all(not reader.is_alive() for reader in live.readers):
                    if not self._observer_finished(live):
                        time.sleep(.02)
                        continue
                    live.exit_code = code
                    live.outcome = live.stop_reason or ("succeeded" if code == 0 and not live.logs_error else "failed")
                    self._update(task_id, phase="finished", outcome=live.outcome, clean=1)
                    live.job.close()
                    live.process.close()
                    live.done.set()
                    if self.on_exit:
                        try:
                            self.on_exit(task_id, live.outcome)
                        except Exception:
                            live.logs_error = True
                    return
            except Exception:
                # Unknown cleanup/storage remains unconfirmed; no success callback.
                pass
            time.sleep(.02)

    def request_stop(self, task_id):
        with self.lock:
            live = self.live.get(task_id)
            if live is not None and not live.done.is_set():
                live.observer_closed = True
                if live.observer_pending:
                    live.logs_error = True
                if live.stop_reason is None:
                    live.stop_reason = "cancelled"
                live.job.terminate()

    def cleanup_confirmed(self, task_id):
        row = self._rows().get(task_id)
        if row is None or not row["clean"]:
            return False
        live = self.live.get(task_id)
        if live is not None:
            return live.done.is_set()
        # Persisted clean is a record of proof, never a substitute for native
        # reconciliation after reopening. Unknown/reused live PID is not killed.
        identity = process_identity(row["pid"]) if row["pid"] else None
        if identity and identity["alive"]:
            return False
        k = _kernel()
        handle = k.OpenJobObjectW(4, False, row["job_name"])
        if handle:
            k.CloseHandle(handle)
            return False
        return ctypes.get_last_error() == 2

    def result(self, task_id):
        row = self._rows().get(task_id)
        if row is None:
            raise RunnerError("TASK_NOT_FOUND")
        with self.lock:
            live = self.live.get(task_id)
            clean = self.cleanup_confirmed(task_id)
            return {"outcome": row["outcome"] if clean else None, "cleanup_confirmed": clean,
                "exit_code": live.exit_code if live else None,
                **{stream + "_tail": ("[TRUNCATED]\n" if live and stream in live.truncated_streams else "") + (live.tails[stream] if live else "") for stream in ("stdout", "stderr")},
                "truncated": bool(live and live.truncated), "redacted": bool(live and live.redacted), "logs_error": bool(live and live.logs_error)}

    def wait(self, task_id, timeout=10):
        live = self.live.get(task_id)
        if live is not None and not live.done.wait(timeout):
            raise RunnerError("CLEANUP_FAILED")
        return self.result(task_id)

    def identity(self, task_id):
        row = self._rows().get(task_id)
        if row is None:
            raise RunnerError("TASK_NOT_FOUND")
        live = self.live.get(task_id)
        return {"pid": row["pid"], "creation_time": row["creation_time"], "job_name": row["job_name"],
                "job_active_processes": live.job.active_count() if live and not live.done.is_set() else 0}

    def recover(self):
        """Exclusive startup audit. Never kills by persisted PID or job name."""
        results = {}
        k = _kernel()
        for task_id, row in self._rows().items():
            if row["clean"]:
                results[task_id] = self.cleanup_confirmed(task_id)
                continue
            creator = process_identity(row["creator_pid"])
            if creator and creator["alive"] and creator["creation_time"] == row["creator_time"]:
                results[task_id] = False
                continue
            process = process_identity(row["pid"]) if row["pid"] else None
            if process and process["alive"]:
                results[task_id] = False  # Includes reused PID; never terminate it.
                continue
            handle = k.OpenJobObjectW(4, False, row["job_name"])
            if handle:
                k.CloseHandle(handle)
                results[task_id] = False
                continue
            if ctypes.get_last_error() != 2:
                results[task_id] = False
                continue
            # Named KILL_ON_CLOSE job destruction proves its associated tree is gone.
            self._update(task_id, phase="finished", outcome="interrupted", clean=1)
            results[task_id] = True
        return results

    def close(self):
        with self.lock:
            self._closed = True
        for task_id, live in list(self.live.items()):
            if not live.done.is_set():
                try:
                    self.request_stop(task_id)
                    live.done.wait(5)
                finally:
                    live.job.close()  # Only this Runner's handles, never public PIDs.
