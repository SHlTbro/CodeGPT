"""Canonical TOML, strict validation and recoverable configuration transactions.

YAML is accepted only at an explicitly requested legacy path or by ensure_config's
one-time migration. Production callers use config.toml after migration.
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import tomllib
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import errors
from projects import Project, ProjectRegistry
from local_agents.config import (
    AgentConfigError, default_agent_config, normalize_agent_config,
    preserve_agent_settings, project_agent_from_data, project_agent_to_dict,
)

EXAMPLE = '''[[projects]]
id = "my-project"
name = "My Project"
path = "D:/path/to/my-project"
write_enabled = false

[server]
host = "127.0.0.1"
port = 8168
path = "/mcp"
'''
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_DEFAULT_LIMITS = {
    "max_file_size_bytes": 5242880, "max_chars": 50000,
    "max_search_results": 100, "list_files_max_entries": 2000,
}
_DEFAULT_SECURITY = {
    "sensitive_patterns": [".env", "*.key", "*.pem", "credentials*", ".ssh/**", ".aws/**"],
    "default_excluded_dirs": [".git", "node_modules", "dist", "build", "__pycache__"],
    "write_paths": ["docs/**"],
}
_INTEL_LANGS = ("python", "typescript", "go", "rust")
_TUNNEL_MODES = {"off", "cloudflare", "ngrok", "named"}


class ConfigError(Exception):
    """User-facing CONFIG_ERROR; callers decide whether a process must stop."""


class ConfigConflictError(ConfigError):
    """The file changed since the client read it; reload before retrying."""


class ConfigApplyError(ConfigError):
    """A valid configuration could not be applied and verified."""


@dataclass(frozen=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8000  # Existing Config callers retain the transport contract.
    path: str = "/mcp"


@dataclass(frozen=True)
class TunnelConfig:
    mode: str = "off"
    public_url: str = ""


@dataclass(frozen=True)
class StartupConfig:
    enabled: bool = False


@dataclass(frozen=True)
class Config:
    projects: tuple[Project, ...]
    server: ServerConfig = field(default_factory=ServerConfig)
    limits: dict = field(default_factory=lambda: dict(_DEFAULT_LIMITS))
    security: dict = field(default_factory=lambda: copy.deepcopy(_DEFAULT_SECURITY))
    intel_servers: dict[str, list[str]] = field(default_factory=dict)
    tunnel: TunnelConfig = field(default_factory=TunnelConfig)
    startup: StartupConfig = field(default_factory=StartupConfig)
    agent: dict = field(default_factory=default_agent_config)

    @property
    def registry(self) -> ProjectRegistry:
        return ProjectRegistry(list(self.projects))


def _fail(msg: str) -> ConfigError:
    return ConfigError(f"[{errors.CONFIG_ERROR}] {msg}")


def _unknown(raw: dict, allowed, label: str, cfg_path: Path) -> list[str]:
    return [f"{cfg_path}: {label}{key} 是未知配置项（允许: {sorted(allowed)}）"
            for key in raw if key not in allowed]


def _write_roots_problems(pid: str, raw, cfg_path: Path) -> list[str]:
    if not isinstance(raw, list) or not raw:
        return [f"{cfg_path}: 项目 '{pid}' 的 write_roots 必须是非空字符串列表（缺省默认 [docs]）"]
    problems = []
    for root in raw:
        if not isinstance(root, str) or not root.strip():
            problems.append(f"{cfg_path}: 项目 '{pid}' 的 write_roots 每项必须是非空字符串: {root!r}")
            continue
        norm = root.strip().replace("\\", "/")
        if (any(s in ("", ".", "..") for s in norm.split("/"))
                or any(c in norm for c in ("*", ":", "\x00")) or norm.startswith("/")):
            problems.append(f"{cfg_path}: 项目 '{pid}' 的 write_roots 仅允许干净的相对目录名"
                            f"（禁 ..、绝对路径、*）: {root!r}")
    return problems


def _section(data: dict, key: str, cfg_path: Path, problems: list[str]) -> dict:
    raw = data.get(key, {})
    if not isinstance(raw, dict):
        problems.append(f"{cfg_path}: {key} 必须是映射")
        return {}
    return raw


def validate_config(data: dict, cfg_path: Path | str = "config.toml") -> list[str]:
    """Collect every schema problem. Empty projects is a valid first installation."""
    cfg_path = Path(cfg_path)
    if not isinstance(data, dict):
        return [f"{cfg_path}: 配置顶层必须是映射"]
    problems = _unknown(data, {"projects", "server", "limits", "security", "intel", "tunnel", "startup", "agent"}, "", cfg_path)
    agent = None
    try:
        agent = normalize_agent_config(data.get("agent", {}))
    except AgentConfigError as error:
        problems.append(f"{cfg_path}: agent 配置无效: {error}")
    raw = data.get("projects")
    if not isinstance(raw, list):
        problems.append(f"{cfg_path}: projects 必须是列表。首次安装可为空。示例：\n{EXAMPLE}")
        raw = []
    seen = set()
    for i, entry in enumerate(raw):
        label = f"projects[{i}]."
        if not isinstance(entry, dict):
            problems.append(f"{cfg_path}: projects[{i}] 必须是含 id/name/path 的映射")
            continue
        problems.extend(_unknown(entry, {"id", "name", "path", "write_enabled", "write_roots", "agent"}, label, cfg_path))
        try:
            project_agent = project_agent_from_data(entry.get("agent", {}))
            if agent is not None and any(p not in agent["profiles"] for p in project_agent.allowed_profiles):
                problems.append(f"{cfg_path}: {label}agent.allowed_profiles 必须引用已配置的阅读档案")
        except AgentConfigError as error:
            problems.append(f"{cfg_path}: {label}agent 配置无效: {error}")
        pid, name, value = entry.get("id"), entry.get("name"), entry.get("path")
        for key, val in (("id", pid), ("name", name), ("path", value)):
            if not isinstance(val, str) or not val.strip() or "\x00" in val:
                problems.append(f"{cfg_path}: {label}{key} 必填且为非空字符串")
        if isinstance(pid, str) and pid.strip():
            if pid in seen:
                problems.append(f"{cfg_path}: 项目 id 重复: '{pid}'（id 必须唯一）")
            seen.add(pid)
        if isinstance(value, str) and value.strip() and "\x00" not in value:
            path = Path(value)
            if not path.is_absolute():
                problems.append(f"{cfg_path}: 项目 '{pid}' 的 path 必须是绝对路径: {value}")
            elif not path.is_dir():
                problems.append(f"{cfg_path}: 项目 '{pid}' 的 path 不存在: {value}。请选择已存在的本地目录。")
        if "write_enabled" in entry and type(entry["write_enabled"]) is not bool:
            problems.append(f"{cfg_path}: {label}write_enabled 必须是 true 或 false，字符串不能作为权限开关")
        if "write_roots" in entry:
            problems.extend(_write_roots_problems(str(pid), entry["write_roots"], cfg_path))

    server = _section(data, "server", cfg_path, problems)
    problems.extend(_unknown(server, {"host", "port", "path"}, "server.", cfg_path))
    host, port, mpath = server.get("host", "127.0.0.1"), server.get("port", 8000), server.get("path", "/mcp")
    if not isinstance(host, str) or host not in _LOOPBACK_HOSTS:
        problems.append(f"{cfg_path}: server.host 仅允许 loopback（{host!r} 被禁止，会向局域网暴露源码）")
    if type(port) is not int or not 1 <= port <= 65535:
        problems.append(f"{cfg_path}: server.port 必须是 1-65535 的整数: {port!r}")
    if not isinstance(mpath, str) or not mpath.startswith("/"):
        problems.append(f"{cfg_path}: server.path 必须以 / 开头: {mpath!r}")
    elif any(ord(c) < 33 for c in mpath) or any(c in mpath for c in ("?", "#", "\\")):
        problems.append(f"{cfg_path}: server.path 必须是没有空格、查询参数或反斜线的 URL 路径")

    for section, defaults in (("limits", _DEFAULT_LIMITS), ("security", _DEFAULT_SECURITY)):
        values = _section(data, section, cfg_path, problems)
        problems.extend(_unknown(values, defaults, f"{section}.", cfg_path))
        for key, value in values.items():
            if key not in defaults:
                continue
            if section == "limits" and (type(value) is not int or value <= 0):
                problems.append(f"{cfg_path}: limits.{key} 必须是正整数（不能使用布尔值）")
            if section == "security" and not (isinstance(value, list) and all(
                    isinstance(v, str) and v.strip() and "\x00" not in v for v in value)):
                problems.append(f"{cfg_path}: security.{key} 必须是非空字符串组成的列表")

    intel = _section(data, "intel", cfg_path, problems)
    problems.extend(_unknown(intel, {"servers"}, "intel.", cfg_path))
    servers = intel.get("servers", {})
    if not isinstance(servers, dict):
        problems.append(f"{cfg_path}: intel.servers 必须是映射")
    else:
        for lang, spec in servers.items():
            if lang not in _INTEL_LANGS:
                problems.append(f"{cfg_path}: intel.servers.{lang} 未知语言（允许: {list(_INTEL_LANGS)}）")
            if not isinstance(spec, dict):
                problems.append(f"{cfg_path}: intel.servers.{lang} 必须是映射")
                continue
            problems.extend(_unknown(spec, {"command"}, f"intel.servers.{lang}.", cfg_path))
            cmd = spec.get("command")
            if not (isinstance(cmd, list) and cmd and all(isinstance(a, str) and a.strip() and "\x00" not in a for a in cmd)):
                problems.append(f"{cfg_path}: intel.servers.{lang}.command 必须是非空 argv 字符串列表")

    tunnel = _section(data, "tunnel", cfg_path, problems)
    problems.extend(_unknown(tunnel, {"mode", "public_url"}, "tunnel.", cfg_path))
    mode, public_url = tunnel.get("mode", "off"), tunnel.get("public_url", "")
    if not isinstance(mode, str) or mode not in _TUNNEL_MODES:
        problems.append(f"{cfg_path}: tunnel.mode 必须是 off / cloudflare / ngrok / named")
    if not isinstance(public_url, str):
        problems.append(f"{cfg_path}: tunnel.public_url 必须是字符串")
    elif public_url:
        try:
            parsed = urlsplit(public_url)
            valid = (parsed.scheme in {"http", "https"} and bool(parsed.hostname)
                     and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
                     and (parsed.scheme == "https" or parsed.hostname in _LOOPBACK_HOSTS)
                     and not any(ord(c) < 33 for c in public_url) and parsed.port != 0)
        except ValueError:
            valid = False
        if not valid:
            problems.append(f"{cfg_path}: tunnel.public_url 必须是 HTTPS 地址，不能含账号、密钥或查询参数")
    startup = _section(data, "startup", cfg_path, problems)
    problems.extend(_unknown(startup, {"enabled"}, "startup.", cfg_path))
    if "enabled" in startup and type(startup["enabled"]) is not bool:
        problems.append(f"{cfg_path}: startup.enabled 必须是 true 或 false，不能使用字符串或整数")
    return problems


def config_from_data(data: dict, path: Path | str = "config.toml") -> Config:
    cfg_path = Path(path)
    problems = validate_config(data, cfg_path)
    if problems:
        raise _fail("\n".join(problems))
    return Config(
        projects=tuple(Project(id=p["id"], name=p["name"], path=Path(p["path"]),
                               write_enabled=p.get("write_enabled", False),
                               write_roots=tuple(r.strip().replace("\\", "/") for r in p.get("write_roots", ["docs"])),
                               agent=project_agent_from_data(p.get("agent", {})))
                       for p in data["projects"]),
        server=ServerConfig(**data.get("server", {})),
        limits={**_DEFAULT_LIMITS, **data.get("limits", {})},
        security=copy.deepcopy({**_DEFAULT_SECURITY, **data.get("security", {})}),
        intel_servers={lang: list(spec["command"]) for lang, spec in data.get("intel", {}).get("servers", {}).items()},
        tunnel=TunnelConfig(**data.get("tunnel", {})),
        startup=StartupConfig(**data.get("startup", {})),
        agent=normalize_agent_config(data.get("agent", {})),
    )


def config_to_dict(config: Config) -> dict:
    """Complete, deterministic user configuration; credentials never enter this schema."""
    return {
        "projects": [{"id": p.id, "name": p.name, "path": p.path.as_posix(),
                      "write_enabled": p.write_enabled, "write_roots": list(p.write_roots),
                      "agent": project_agent_to_dict(p.agent)} for p in config.projects],
        "server": {"host": config.server.host, "port": config.server.port, "path": config.server.path},
        "limits": dict(config.limits), "security": copy.deepcopy(config.security),
        "intel": {"servers": {lang: {"command": list(argv)} for lang, argv in config.intel_servers.items()}},
        "tunnel": {"mode": config.tunnel.mode, "public_url": config.tunnel.public_url},
        "startup": {"enabled": config.startup.enabled},
        "agent": copy.deepcopy(config.agent),
    }


def config_revision(config: Config) -> str:
    payload = json.dumps(config_to_dict(config), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _parse_bytes(raw: bytes, cfg_path: Path) -> dict:
    try:
        if cfg_path.suffix.lower() in {".yaml", ".yml"}:
            import yaml
            warnings.warn("YAML 配置已弃用；启动入口会一次性迁移为 config.toml。", DeprecationWarning, stacklevel=3)
            try:
                data = yaml.safe_load(raw.decode("utf-8-sig"))
            except yaml.YAMLError as e:
                line = getattr(getattr(e, "problem_mark", None), "line", None)
                loc = f"（第 {line + 1} 行）" if line is not None else ""
                raise _fail(f"{cfg_path}: YAML 语法错误{loc}: {e}") from e
        else:
            data = tomllib.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, tomllib.TOMLDecodeError) as e:
        raise _fail(f"{cfg_path}: TOML 语法错误: {e}。请检查引号、括号和字段类型。") from e
    if not isinstance(data, dict):
        raise _fail(f"{cfg_path}: 配置顶层必须是映射")
    return data


def read_config_data(path: Path | str) -> dict:
    cfg_path = Path(path)
    try:
        raw = cfg_path.read_bytes()
    except FileNotFoundError as e:
        legacy_example = "\n旧配置迁移格式 projects:" if cfg_path.suffix.lower() in {".yaml", ".yml"} else ""
        raise _fail(f"配置文件缺失: {cfg_path}\n请运行 setup.bat 创建 config.toml。示例：\n{EXAMPLE}{legacy_example}") from e
    except OSError as e:
        raise _fail(f"无法读取配置 {cfg_path}: {e}。请检查文件权限。") from e
    return _parse_bytes(raw, cfg_path)


def load_config(path: Path | str) -> Config:
    return config_from_data(read_config_data(path), path)


def load_config_or_exit(path: Path | str) -> Config:
    try:
        return load_config(path)
    except ConfigError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)


def dump_toml(data: dict) -> str:
    """Serialize only this schema's strings, booleans, integers, arrays and tables.

    Parse with stdlib tomllib to verify escaping and round-trip losslessness.
    No arbitrary Python values or executable extension types are supported.
    """
    def value(item):
        if isinstance(item, str):
            return json.dumps(item, ensure_ascii=False)
        if type(item) is bool:
            return "true" if item else "false"
        if type(item) is int:
            return str(item)
        if isinstance(item, list) and not any(isinstance(v, dict) for v in item):
            return "[" + ", ".join(value(v) for v in item) + "]"
        raise _fail(f"不能写入 TOML 的字段类型: {type(item).__name__}")

    lines = ["# Canonical user configuration. Edit here or use setup.bat."]
    def table(raw, keys=(), array=False):
        if keys:
            name = ".".join(json.dumps(k, ensure_ascii=False) for k in keys)
            lines.extend(["", f"[[{name}]]" if array else f"[{name}]"])
        for key, item in raw.items():
            if isinstance(item, dict) or (isinstance(item, list) and item and isinstance(item[0], dict)):
                continue
            lines.append(f"{json.dumps(key, ensure_ascii=False)} = {value(item)}")
        for key, item in raw.items():
            if isinstance(item, dict):
                table(item, (*keys, key))
            elif isinstance(item, list) and item and isinstance(item[0], dict):
                for entry in item:
                    if not isinstance(entry, dict):
                        raise _fail("TOML 表数组不能混合普通值")
                    table(entry, (*keys, key), array=True)
    table(data)
    rendered = "\n".join(lines) + "\n"
    try:
        if tomllib.loads(rendered) != data:
            raise _fail("TOML 序列化无法保持配置内容")
    except tomllib.TOMLDecodeError as e:
        raise _fail(f"TOML 序列化失败: {e}") from e
    return rendered


def _fsync_directory(path: Path) -> None:
    if os.name != "nt":  # Windows cannot open directories; files are flushed before replacement.
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            Path(temporary).unlink(missing_ok=True)
        except OSError:
            pass


_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


@contextlib.contextmanager
def _file_lock(path: Path, timeout: float = 90):
    key = str(path.resolve()).casefold() if os.name == "nt" else str(path.resolve())
    with _LOCKS_GUARD:
        thread_lock = _LOCKS.setdefault(key, threading.RLock())
    deadline = time.monotonic() + max(0, timeout)
    if not thread_lock.acquire(timeout=max(0, timeout)):
        raise _fail("配置正在被另一个进程更新。请稍后刷新页面重试。")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as lock:
            lock.seek(0, os.SEEK_END)
            if lock.tell() == 0:
                lock.write(b"0")
                lock.flush()
            acquired = False
            while not acquired:
                try:
                    lock.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except OSError:
                    if time.monotonic() >= deadline:
                        raise _fail("配置正在被另一个进程更新。请稍后刷新页面重试。")
                    time.sleep(min(0.05, max(0, deadline - time.monotonic())))
            try:
                yield
            finally:
                lock.seek(0)
                if os.name == "nt":
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    finally:
        thread_lock.release()


class ConfigStore:
    """One TOML file with optimistic concurrency and verified application.

    apply_callback synchronously applies AND verifies the running backend. Returning
    False, None, or a mapping without verified=True is a failed transaction.
    A callback exception rolls back the file and re-applies the old Config.
    An optional rollback_callback separates recovery from candidate-only checks;
    by default the same apply_callback handles both, preserving existing callers.
    """
    def __init__(self, path: Path | str):
        self.path = Path(path).resolve()
        if self.path.suffix.lower() != ".toml":
            raise _fail("ConfigStore 仅允许 canonical .toml 文件。请先迁移旧 YAML。")
        self.last_good_path = self.path.with_name(self.path.stem + ".last-good.toml")
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.pending_path = self.path.with_name(self.path.name + ".pending")
        self.rejected_path = self.path.with_name(self.path.name + ".rejected")
        self.last_error: str | None = None

    def _read_unlocked(self) -> dict:
        recovered = False
        raw = b""
        try:
            raw = self.path.read_bytes()
            if self.pending_path.exists():
                raise _fail("上次配置更新未完成后端验证")
            data = _parse_bytes(raw, self.path)
            config_from_data(data, self.path)
        except (ConfigError, OSError) as error:
            try:
                good = self.last_good_path.read_bytes()
                data = _parse_bytes(good, self.last_good_path)
                config_from_data(data, self.last_good_path)
            except (ConfigError, OSError) as backup_error:
                raise _fail(f"当前配置不可用，且没有可恢复的 last-good。请检查 config.toml。原因: {error}") from backup_error
            if raw and raw != good:
                _atomic_write(self.rejected_path, raw)
            _atomic_write(self.path, good)
            self.pending_path.unlink(missing_ok=True)
            _fsync_directory(self.path.parent)
            raw, recovered = good, True
            self.last_error = f"{error}。已恢复最后一次有效配置；问题内容保存在 {self.rejected_path.name}。"
        result = {"revision": hashlib.sha256(raw).hexdigest(), "data": data}
        if recovered:
            result.update(recovered=True, warning=self.last_error)
        return result

    def read(self) -> dict:
        with _file_lock(self.lock_path):
            return self._read_unlocked()

    def mark_verified(self, config: Config, lock_timeout: float = 0.1) -> bool:
        """Persist an externally edited configuration AFTER real backend verification.

        Call outside Runtime lifecycle locks. A pending UI transaction or occupied file
        lock is skipped quickly, preventing lock-order inversion with ConfigStore.save.
        This never changes canonical TOML and never accepts a stale verification.
        """
        if self.pending_path.exists():
            return False
        try:
            with _file_lock(self.lock_path, timeout=lock_timeout):
                if self.pending_path.exists():
                    return False
                content = self.path.read_bytes()
                current = config_from_data(_parse_bytes(content, self.path), self.path)
                if current != config:
                    return False
                _atomic_write(self.last_good_path, content)
                return True
        except (ConfigError, OSError):
            return False

    def restore_verified(self, previous: Config, expected_revision: str,
                         apply_callback: Callable[[Config], object], lock_timeout: float = 0.1) -> dict:
        """Recover a failed external edit only if it is still the current file.

        Runtime calls this after releasing its lifecycle lock. Both this recovery and
        UI save acquire locks in file -> Runtime order. Concurrent edits and pending
        UI transactions are skipped, preserving the newer user's intent.
        """
        if self.pending_path.exists():
            return {"restored": False, "reason": "pending"}
        try:
            with _file_lock(self.lock_path, timeout=lock_timeout):
                if self.pending_path.exists():
                    return {"restored": False, "reason": "pending"}
                current = self.path.read_bytes()
                if hashlib.sha256(current).hexdigest() != expected_revision:
                    return {"restored": False, "reason": "revision_changed"}
                good = self.last_good_path.read_bytes()
                recovered = config_from_data(_parse_bytes(good, self.last_good_path), self.last_good_path)
                if recovered != previous:
                    return {"restored": False, "reason": "last_good_changed"}
                if current != good:
                    _atomic_write(self.rejected_path, current)
                _atomic_write(self.path, good)
                try:
                    applied = apply_callback(recovered)
                    if not self._accepted(applied):
                        raise _fail("后端未确认 last-good 已恢复")
                except Exception as error:
                    self.last_error = f"已恢复 last-good TOML，但后端恢复未完成: {error}。请检查端口和进程后受控重启。"
                    raise ConfigApplyError(f"[{errors.CONFIG_ERROR}] {self.last_error}") from error
                return {"restored": True, "verified": True,
                        "revision": hashlib.sha256(good).hexdigest(), "applied": applied}
        except ConfigApplyError:
            raise
        except ConfigError:
            return {"restored": False, "reason": "busy_or_invalid"}
        except OSError as error:
            self.last_error = f"无法恢复 last-good 配置: {error}。请检查配置目录的写权限。"
            raise ConfigApplyError(f"[{errors.CONFIG_ERROR}] {self.last_error}") from error

    @staticmethod
    def _accepted(result) -> bool:
        if result is None or result is False:
            return False
        if isinstance(result, dict):
            return (result.get("verified") is True and result.get("ok", True) is not False
                    and result.get("applied", True) is not False)
        return result is True

    def save(self, data: dict, expected_revision: str, apply_callback: Callable[[Config], object],
             *, rollback_callback: Callable[[Config], object] | None = None) -> dict:
        if not isinstance(expected_revision, str) or not expected_revision:
            raise ConfigConflictError(f"[{errors.CONFIG_ERROR}] 缺少配置版本。请刷新页面后重试。")
        if not callable(apply_callback):
            raise _fail("保存配置必须提供后端应用与验证回调")
        restore_callback = apply_callback if rollback_callback is None else rollback_callback
        if not callable(restore_callback):
            raise _fail("恢复配置必须提供后端应用与验证回调")
        with _file_lock(self.lock_path):
            previous = self._read_unlocked()
            if expected_revision != previous["revision"]:
                raise ConfigConflictError(f"[{errors.CONFIG_ERROR}] 配置已被其他窗口或进程修改。请刷新页面后重新保存。")
            candidate_data = preserve_agent_settings(data, previous["data"]) if isinstance(data, dict) else data
            candidate = config_from_data(candidate_data, self.path)
            candidate_bytes = dump_toml(candidate_data).encode("utf-8")
            previous_disk = self.path.read_bytes()
            if hashlib.sha256(previous_disk).hexdigest() != previous["revision"]:
                raise ConfigConflictError(f"[{errors.CONFIG_ERROR}] 配置文件正在被外部程序修改。请刷新后重试。")
            # A valid external TOML may still have failed runtime verification.
            # Keep the persisted last-good instead of promoting that unverified file.
            if self.last_good_path.exists():
                old_bytes = self.last_good_path.read_bytes()
                old_config = config_from_data(_parse_bytes(old_bytes, self.last_good_path), self.last_good_path)
            else:
                old_bytes = previous_disk
                old_config = config_from_data(previous["data"], self.path)
                _atomic_write(self.last_good_path, old_bytes)
            _atomic_write(self.pending_path, previous["revision"].encode("ascii"))
            try:
                _atomic_write(self.path, candidate_bytes)
                applied = apply_callback(candidate)
                if not self._accepted(applied):
                    raise _fail("后端没有确认新配置已实际生效")
                if self.path.read_bytes() != candidate_bytes:
                    raise ConfigConflictError(f"[{errors.CONFIG_ERROR}] 验证期间配置被外部程序修改")
                _atomic_write(self.last_good_path, candidate_bytes)
                self.pending_path.unlink(missing_ok=True)
                _fsync_directory(self.path.parent)
            except Exception as error:
                recovery = ""
                try:
                    external = self.path.read_bytes() if self.path.exists() else b""
                    rejected = external if external and external not in (candidate_bytes, old_bytes) else previous_disk
                    if rejected != old_bytes:
                        _atomic_write(self.rejected_path, rejected)
                    _atomic_write(self.path, old_bytes)
                    _atomic_write(self.last_good_path, old_bytes)
                    restored = restore_callback(old_config)
                    if not self._accepted(restored):
                        raise _fail("后端未确认 last-good 已恢复")
                    self.pending_path.unlink(missing_ok=True)
                    _fsync_directory(self.path.parent)
                except Exception as rollback_error:
                    recovery = f"；恢复后端失败: {rollback_error}。请检查端口/进程状态并重新启动 setup.bat"
                self.last_error = f"配置未生效，已回退 last-good: {error}{recovery}"
                raise ConfigApplyError(f"[{errors.CONFIG_ERROR}] {self.last_error}") from error
            self.last_error = None
            return {"revision": hashlib.sha256(candidate_bytes).hexdigest(), "data": candidate_data, "applied": applied}


def ensure_config(path: Path | str, legacy_dirs=None) -> Path:
    """Create TOML once; config.yaml precedes the old derived runtime file.

    Existing TOML always wins, including over newer YAML edits. Credentials remain
    in fixed token files. Invalid legacy input never creates a partial TOML.
    """
    target = Path(path).resolve()
    store = ConfigStore(target)
    with _file_lock(store.lock_path):
        if target.exists() or store.last_good_path.exists():
            store._read_unlocked()
            if not store.last_good_path.exists():
                # This is validated disk data; backend verification still belongs to Runtime.
                _atomic_write(store.last_good_path, target.read_bytes())
            return target
        roots = [target.parent]
        if legacy_dirs:
            if isinstance(legacy_dirs, (str, Path)):
                legacy_dirs = [legacy_dirs]
            roots.extend(Path(d).resolve() for d in legacy_dirs if Path(d).resolve() not in roots)
        legacy = next((root / name for name in ("config.yaml", "config.yml", "config.runtime.yaml")
                       for root in roots if (root / name).is_file()), None)
        if legacy:
            data = config_to_dict(load_config(legacy))
        else:
            data = config_to_dict(Config(projects=(), server=ServerConfig(port=8168)))
        content = dump_toml(data).encode("utf-8")
        config_from_data(data, target)
        _atomic_write(target, content)
        _atomic_write(store.last_good_path, content)
        return target

