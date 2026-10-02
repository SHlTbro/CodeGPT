"""config.yaml → Config（fail-fast 校验，CONFIG_ERROR 语义）。"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

import errors
from projects import Project, ProjectRegistry

EXAMPLE = """\
projects:
  - id: my-project
    name: My Project
    path: D:/path/to/my-project
server:
  host: 127.0.0.1
  port: 8168
  path: /mcp
"""

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

_DEFAULT_LIMITS = {
    "max_file_size_bytes": 5242880,
    "max_chars": 50000,
    "max_search_results": 100,
    "list_files_max_entries": 2000,
}
_DEFAULT_SECURITY = {
    "sensitive_patterns": [".env", "*.key", "*.pem", "credentials*", ".ssh/**", ".aws/**"],
    "default_excluded_dirs": [".git", "node_modules", "dist", "build", "__pycache__"],
    "write_paths": ["docs/**"],
}
# v1.3 intel.servers 允许的语言键（与 intel._DETECT 探测表对应）
_INTEL_LANGS = ("python", "typescript", "go", "rust")


class ConfigError(Exception):
    """CONFIG_ERROR 语义的配置错误，message 面向用户可直接打印。"""


@dataclass(frozen=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8000  # 契约 transport.port；部署脚本按需在 config.yaml 覆写（如 8168）
    path: str = "/mcp"


@dataclass(frozen=True)
class Config:
    projects: tuple[Project, ...]
    server: ServerConfig = field(default_factory=ServerConfig)
    limits: dict = field(default_factory=lambda: dict(_DEFAULT_LIMITS))
    security: dict = field(default_factory=lambda: dict(_DEFAULT_SECURITY))
    intel_servers: dict[str, list[str]] = field(default_factory=dict)  # 语言 → argv（覆盖自动探测）

    @property
    def registry(self) -> ProjectRegistry:
        return ProjectRegistry(list(self.projects))


def _fail(msg: str) -> "ConfigError":
    return ConfigError(f"[{errors.CONFIG_ERROR}] {msg}")


def _write_roots_problems(pid: str, raw, cfg_path: Path) -> list[str]:
    """v1.7 项目级 write_roots 校验（validate_config 与 _parse_projects 共用，行为一致）：
    可选非空字符串列表；每项为干净相对目录名（禁 ..、绝对路径/盘符、*、空段），
    允许多级（docs/sub）且多个 root 可层级交叠（匹配取最先命中）。"""
    if raw is None:
        return []
    if not isinstance(raw, list) or not raw:
        return [f"{cfg_path}: 项目 '{pid}' 的 write_roots 必须是非空字符串列表（缺省默认 [docs]）"]
    problems: list[str] = []
    for r in raw:
        if not isinstance(r, str) or not r.strip():
            problems.append(f"{cfg_path}: 项目 '{pid}' 的 write_roots 每项必须是非空字符串: {r!r}")
            continue
        norm = r.strip().replace("\\", "/")
        segs = norm.split("/")
        if any(s in ("", ".", "..") for s in segs) or "*" in norm or ":" in norm or norm.startswith("/"):
            problems.append(f"{cfg_path}: 项目 '{pid}' 的 write_roots 仅允许干净的相对目录名"
                            f"（禁 ..、绝对路径、*）: {r!r}")
    return problems


def _parse_write_roots(pid: str, entry: dict, cfg_path: Path) -> tuple[str, ...]:
    raw = entry.get("write_roots")
    if raw is None:
        return ("docs",)                                   # 缺省默认：仅 docs（v1.2 行为不变）
    problems = _write_roots_problems(pid, raw, cfg_path)
    if problems:
        raise _fail("\n".join(problems))
    return tuple(r.strip().replace("\\", "/") for r in raw)


def _parse_projects(data: dict, cfg_path: Path) -> list[Project]:
    raw = data.get("projects")
    if not isinstance(raw, list) or not raw:
        raise _fail(f"{cfg_path}: projects 必须是非空列表。示例：\n{EXAMPLE}")
    projects: list[Project] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise _fail(f"{cfg_path}: projects[{i}] 必须是含 id/name/path 的映射")
        pid, name, p = entry.get("id"), entry.get("name"), entry.get("path")
        for k, v in (("id", pid), ("name", name), ("path", p)):
            if not isinstance(v, str) or not v.strip():
                raise _fail(f"{cfg_path}: projects[{i}].{k} 必填且为非空字符串")
        if pid in seen:
            raise _fail(f"{cfg_path}: 项目 id 重复: '{pid}'（id 必须唯一）")
        seen.add(pid)
        path = Path(p)
        if not path.is_absolute():
            raise _fail(f"{cfg_path}: 项目 '{pid}' 的 path 必须是绝对路径: {p}")
        if not path.is_dir():
            raise _fail(f"{cfg_path}: 项目 '{pid}' 的 path 不存在: {p}")
        projects.append(Project(id=pid, name=name, path=path,
                                write_enabled=bool(entry.get("write_enabled", False)),
                                write_roots=_parse_write_roots(pid, entry, cfg_path)))
    return projects


def _server_problems(data: dict, cfg_path: Path) -> list[str]:
    """server 段校验唯一权威（C-007 去重）：validate_config 与 _parse_server 共用，行为不变。"""
    sraw = data.get("server") or {}
    if not isinstance(sraw, dict):
        return [f"{cfg_path}: server 必须是映射"]
    host, port, mpath = sraw.get("host", "127.0.0.1"), sraw.get("port", 8000), sraw.get("path", "/mcp")
    problems: list[str] = []
    if host not in _LOOPBACK_HOSTS:
        problems.append(f"{cfg_path}: server.host 仅允许 loopback（{host} 被禁止，会向局域网暴露源码）")
    if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
        problems.append(f"{cfg_path}: server.port 必须是 1-65535 的整数: {port!r}")
    if not (isinstance(mpath, str) and mpath.startswith("/")):
        problems.append(f"{cfg_path}: server.path 必须以 / 开头: {mpath!r}")
    return problems


def _parse_server(data: dict, cfg_path: Path) -> ServerConfig:
    problems = _server_problems(data, cfg_path)
    if problems:
        raise _fail("\n".join(problems))
    sraw = data.get("server") or {}
    return ServerConfig(host=sraw.get("host", "127.0.0.1"),
                        port=sraw.get("port", 8000), path=sraw.get("path", "/mcp"))


def _parse_section(data: dict, key: str, defaults: dict, cfg_path: Path) -> dict:
    raw = data.get(key) or {}
    if not isinstance(raw, dict):
        raise _fail(f"{cfg_path}: {key} 必须是映射")
    out = dict(defaults)
    for k, v in raw.items():
        if k not in defaults:
            raise _fail(f"{cfg_path}: {key}.{k} 是未知配置项（允许: {sorted(defaults)}）")
        out[k] = v
    return out


def _intel_problems(data: dict, cfg_path: Path) -> list[str]:
    """intel.servers 可选段校验（v1.3）：{<语言>: {command: [非空 argv]}}。"""
    raw = data.get("intel")
    if raw is None:
        return []
    if not isinstance(raw, dict):
        return [f"{cfg_path}: intel 必须是映射"]
    servers = raw.get("servers", {})
    if not isinstance(servers, dict):
        return [f"{cfg_path}: intel.servers 必须是映射"]
    problems: list[str] = []
    for lang, spec in servers.items():
        if lang not in _INTEL_LANGS:
            problems.append(f"{cfg_path}: intel.servers.{lang} 未知语言（允许: {list(_INTEL_LANGS)}）")
            continue
        cmd = spec.get("command") if isinstance(spec, dict) else None
        if not (isinstance(cmd, list) and cmd and all(isinstance(a, str) and a.strip() for a in cmd)):
            problems.append(f"{cfg_path}: intel.servers.{lang}.command 必须是非空 argv 字符串列表")
    return problems


def _parse_intel(data: dict, cfg_path: Path) -> dict[str, list[str]]:
    problems = _intel_problems(data, cfg_path)
    if problems:
        raise _fail("\n".join(problems))
    servers = (data.get("intel") or {}).get("servers") or {}
    return {lang: list(spec["command"]) for lang, spec in servers.items()}


def validate_config(data: dict, cfg_path: Path | str = "config.yaml") -> list[str]:
    """校验配置返回全部问题（供 load_config 与 server.py --check 共用）；无问题返回空列表。"""
    cfg_path = Path(cfg_path)
    problems: list[str] = []
    if not isinstance(data, dict):
        return [f"{cfg_path}: 配置顶层必须是映射"]
    raw = data.get("projects")
    if not isinstance(raw, list) or not raw:
        problems.append(f"{cfg_path}: projects 必须是非空列表。示例：\n{EXAMPLE}")
        raw = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            problems.append(f"{cfg_path}: projects[{i}] 必须是含 id/name/path 的映射")
            continue
        pid, name, p = entry.get("id"), entry.get("name"), entry.get("path")
        for k, v in (("id", pid), ("name", name), ("path", p)):
            if not isinstance(v, str) or not v.strip():
                problems.append(f"{cfg_path}: projects[{i}].{k} 必填且为非空字符串")
        if not (isinstance(pid, str) and pid.strip()):
            continue
        if pid in seen:
            problems.append(f"{cfg_path}: 项目 id 重复: '{pid}'（id 必须唯一）")
        seen.add(pid)
        path = Path(p) if isinstance(p, str) else None
        if path is None or not path.is_absolute():
            problems.append(f"{cfg_path}: 项目 '{pid}' 的 path 必须是绝对路径: {p}")
        elif not path.is_dir():
            problems.append(f"{cfg_path}: 项目 '{pid}' 的 path 不存在: {p}")
        problems.extend(_write_roots_problems(pid, entry.get("write_roots"), cfg_path))
    problems.extend(_server_problems(data, cfg_path))
    problems.extend(_intel_problems(data, cfg_path))
    try:
        _parse_section(data, "limits", _DEFAULT_LIMITS, cfg_path)
        _parse_section(data, "security", _DEFAULT_SECURITY, cfg_path)
    except ConfigError as e:
        problems.append(str(e))
    return problems


def load_config(path: Path | str) -> Config:
    """加载并校验配置；任何失败抛 ConfigError（CONFIG_ERROR），由调用方决定退出。"""
    cfg_path = Path(path)
    if not cfg_path.is_file():
        raise _fail(f"配置文件缺失: {cfg_path}\n请在 {cfg_path.parent} 下创建 config.yaml，示例：\n{EXAMPLE}")
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        line = getattr(getattr(e, "problem_mark", None), "line", None)
        loc = f"（第 {line + 1} 行）" if line is not None else ""
        raise _fail(f"{cfg_path}: YAML 语法错误{loc}: {e}") from e
    if not isinstance(data, dict):
        raise _fail(f"{cfg_path}: 配置顶层必须是映射")
    problems = validate_config(data, cfg_path)
    if problems:
        raise _fail("\n".join(problems))
    return Config(
        projects=tuple(_parse_projects(data, cfg_path)),
        server=_parse_server(data, cfg_path),
        limits=_parse_section(data, "limits", _DEFAULT_LIMITS, cfg_path),
        security=_parse_section(data, "security", _DEFAULT_SECURITY, cfg_path),
        intel_servers=_parse_intel(data, cfg_path),
    )


def load_config_or_exit(path: Path | str) -> Config:
    """启动入口用：加载失败时打印错误并以非零码退出。"""
    try:
        return load_config(path)
    except ConfigError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
