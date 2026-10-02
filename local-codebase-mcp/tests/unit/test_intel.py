"""task17 intel 单元测试：mock 语言服务器全链 + detect/会话管理/降级（≥15 项）。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from config import Config, ConfigError, load_config, validate_config
from errors import McpError
from intel import (IntelManager, detect_server, normalize_diagnostics,
                   normalize_location, normalize_symbol, to_lsp_position)
from projects import Project
from tools_services import (find_definition, find_references, get_diagnostics,
                            workspace_symbols)

PKG_DIR = Path(__file__).resolve().parents[2]
FAKE_LSP = PKG_DIR / "tests" / "fixtures" / "fake_lsp.py"
PY = sys.executable


def make_project(tmp_path: Path, marker: str | None = "pyproject.toml") -> Project:
    if marker:
        (tmp_path / marker).write_text("", encoding="utf-8")
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "main.py").write_text("def main():\n    pass\n", encoding="utf-8")
    return Project(id="p", name="P", path=tmp_path)


def make_config(**servers) -> Config:
    return Config(projects=(), intel_servers={k: list(v) for k, v in servers.items()})


def fake_argv(*extra) -> list[str]:
    return [PY, str(FAKE_LSP), *extra]


@pytest.fixture()
def mgr():
    m = IntelManager(make_config(python=fake_argv()))
    yield m
    m.close_all()


# ---- detect_server ----

@pytest.mark.parametrize("marker,expect", [
    ("pyproject.toml", ["pyright-langserver", "--stdio"]),
    ("requirements.txt", ["pyright-langserver", "--stdio"]),
    ("package.json", ["npx", "typescript-language-server", "--stdio"]),
    ("go.mod", ["gopls"]),
    ("Cargo.toml", ["rust-analyzer"]),
])
def test_detect_marker_files(tmp_path, marker, expect):
    assert detect_server(make_project(tmp_path, marker)) == expect


def test_detect_no_marker_returns_none(tmp_path):
    assert detect_server(make_project(tmp_path, marker=None)) is None


def test_detect_config_override_priority(tmp_path):
    p = make_project(tmp_path)
    assert detect_server(p, make_config(python=["my-lsp", "--stdio"])) == ["my-lsp", "--stdio"]


def test_detect_override_only_matches_detected_lang(tmp_path):
    p = make_project(tmp_path, marker="go.mod")
    assert detect_server(p, make_config(python=["my-lsp"])) == ["gopls"]


# ---- mock 全链（definition/references/symbols/diagnostics）----

def _uri(p: Project) -> str:
    return (p.path / "src" / "main.py").as_uri()


def test_definition_roundtrip_1based(mgr, tmp_path):
    """1-based 入参 → LSP 0-based → mock 固定位置 (9,2) → 转回 1-based (10,3)。"""
    p = make_project(tmp_path)
    sess = mgr.get_session(p)
    sess.ensure_open(p.path / "src" / "main.py")
    res = sess.request("textDocument/definition", {
        "textDocument": {"uri": _uri(p)}, "position": to_lsp_position(3, 5)})
    assert normalize_location(p.path, res) == {
        "path": "src/main.py", "line": 10, "character": 3}


def test_definition_requires_didopen(mgr, tmp_path):
    p = make_project(tmp_path)
    sess = mgr.get_session(p)
    params = {"textDocument": {"uri": _uri(p)}, "position": to_lsp_position(1, 1)}
    assert normalize_location(p.path, sess.request("textDocument/definition", params)) is None
    sess.ensure_open(p.path / "src" / "main.py")
    assert normalize_location(p.path, sess.request("textDocument/definition", params)) is not None


def test_references_1based_and_count(mgr, tmp_path):
    p = make_project(tmp_path)
    sess = mgr.get_session(p)
    sess.ensure_open(p.path / "src" / "main.py")
    res = sess.request("textDocument/references", {
        "textDocument": {"uri": _uri(p)}, "position": to_lsp_position(1, 1),
        "context": {"includeDeclaration": False}})
    locs = [normalize_location(p.path, r) for r in res]
    assert len(locs) == 3
    assert locs[0] == {"path": "src/main.py", "line": 5, "character": 7}  # mock 0-based (4,6)
    assert all(l["line"] >= 1 and l["character"] >= 1 for l in locs)


def test_workspace_symbols_fuzzy_1based(mgr, tmp_path):
    p = make_project(tmp_path)
    sess = mgr.get_session(p)
    res = sess.request("workspace/symbol", {"query": "auth"})
    syms = [normalize_symbol(p.path, s) for s in res]
    assert len(syms) == 2 and all("auth" in s["name"].lower() for s in syms)
    assert syms[0]["kind"] == "Class"
    assert (syms[0]["line"], syms[0]["character"]) == (5, 7)  # mock 0-based (4,6)


def test_diagnostics_pull_conversion(mgr, tmp_path):
    p = make_project(tmp_path)
    sess = mgr.get_session(p)
    sess.ensure_open(p.path / "src" / "main.py")
    res = sess.request("textDocument/diagnostic", {"textDocument": {"uri": _uri(p)}})
    items = normalize_diagnostics(p.path, _uri(p), res["items"])
    assert items[0]["severity"] == "error" and items[0]["line"] == 4
    assert items[1]["severity"] == "warning" and items[1]["line"] == 8
    assert items[1]["source"] == "fake-lsp" and items[0]["path"] == "src/main.py"


# ---- 会话管理：复用 / 崩溃重启 / 降级 ----

def test_session_reuse_no_restart(mgr, tmp_path):
    p = make_project(tmp_path)
    s1 = mgr.get_session(p)
    s2 = mgr.get_session(p)
    assert s1 is s2 and s1.pid == s2.pid  # 第二次调用命中缓存，未重启进程


def test_crash_then_restart(tmp_path):
    m = IntelManager(make_config(python=fake_argv("--die-on", "textDocument/definition")))
    try:
        p = make_project(tmp_path)
        s1 = m.get_session(p)
        s1.ensure_open(p.path / "src" / "main.py")
        with pytest.raises(McpError) as ei:
            s1.request("textDocument/definition",
                       {"textDocument": {"uri": _uri(p)}, "position": to_lsp_position(1, 1)})
        assert ei.value.payload["error"]["code"] == "LSP_UNAVAILABLE"
        s2 = m.get_session(p)  # 崩溃后下次调用重启新进程
        assert s2.pid != s1.pid
        assert s2.request("workspace/symbol", {"query": "x"}) is not None
    finally:
        m.close_all()


def test_command_not_found_lsp_unavailable(tmp_path):
    m = IntelManager(make_config(python=["no-such-lsp-binary-xyz"]))
    with pytest.raises(McpError) as ei:
        m.get_session(make_project(tmp_path))
    err = ei.value.payload["error"]
    assert err["code"] == "LSP_UNAVAILABLE" and err["recoverable"] is False
    assert "pyright-langserver" in err["hint"] and "search_code" in err["hint"]


def test_no_marker_project_lsp_unavailable(mgr, tmp_path):
    with pytest.raises(McpError) as ei:
        mgr.get_session(make_project(tmp_path, marker=None))
    err = ei.value.payload["error"]
    assert err["code"] == "LSP_UNAVAILABLE" and "search_code" in err["hint"]


def test_request_timeout_lsp_unavailable(tmp_path):
    m = IntelManager(make_config(python=fake_argv("--no-reply", "workspace/symbol")))
    try:
        sess = m.get_session(make_project(tmp_path))
        with pytest.raises(McpError) as ei:
            sess.request("workspace/symbol", {"query": "x"}, timeout=1)
        assert ei.value.payload["error"]["code"] == "LSP_UNAVAILABLE"
        assert "超时" in ei.value.payload["error"]["message"]
    finally:
        m.close_all()


# ---- config：intel.servers 解析 + C-007 ----

def _cfg_dict(tmp_path: Path, extra: dict) -> dict:
    return {"projects": [{"id": "a", "name": "A", "path": str(tmp_path)}], **extra}


def test_config_intel_servers_parsed(tmp_path):
    data = _cfg_dict(tmp_path, {"intel": {"servers": {
        "python": {"command": ["pyright-langserver", "--stdio"]}}}})
    assert validate_config(data) == []
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert load_config(p).intel_servers == {"python": ["pyright-langserver", "--stdio"]}


def test_config_intel_invalid_rejected(tmp_path):
    data = _cfg_dict(tmp_path, {"intel": {"servers": {"python": {"command": "not-a-list"}}}})
    problems = validate_config(data, tmp_path / "config.yaml")
    assert any("intel.servers.python.command" in s for s in problems)
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigError) as ei:
        load_config(p)
    assert "intel.servers.python.command" in str(ei.value)


def test_c007_server_validation_single_source(tmp_path):
    """C-007：validate_config 与 load_config 的 server 段问题来自同一权威（一次列全，行为不变）。"""
    data = _cfg_dict(tmp_path, {"server": {"host": "0.0.0.0", "port": 99999, "path": "mcp"}})
    problems = validate_config(data, tmp_path / "config.yaml")
    assert len(problems) == 3  # host / port / path 三问题一次列全
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigError) as ei:
        load_config(p)
    for msg in problems:
        assert msg in str(ei.value)


# ---- task18 服务层（tools_services 四智能工具）增补 ----

@pytest.fixture(scope="module")
def svc_cfg(tmp_path_factory):
    """服务层标本：--refs 120 的 fake_lsp（覆盖截断断言），module 级复用同一会话。"""
    tmp = tmp_path_factory.mktemp("intel-svc")
    yield Config(projects=(make_project(tmp),),
                 intel_servers={"python": [PY, str(FAKE_LSP), "--refs", "120"]})
    import tools_services as ts
    if ts._INTEL[1] is not None:
        ts._INTEL[1].close_all()
        ts._INTEL[0] = ts._INTEL[1] = None


def test_service_references_truncated_at_100(svc_cfg):
    """mock 返回 120 条 → 前 100 + truncated:true + next_hint。"""
    res = find_references(svc_cfg, "p", "src/main.py", 3, 5)
    assert len(res["locations"]) == 100 and res["truncated"] is True
    assert res["next_hint"] == "缩小 query 或限定 path"
    assert res["project_id"] == "p"
    assert all(l["line"] >= 1 and l["character"] >= 1 for l in res["locations"])


def test_service_invalid_line_character(svc_cfg):
    """line=0 / character=-1 → INVALID_ARGUMENT，message 注明 1-based。"""
    for kwargs in ({"line": 0, "character": 1}, {"line": 1, "character": -1}):
        with pytest.raises(McpError) as ei:
            find_definition(svc_cfg, "p", "src/main.py", **kwargs)
        err = ei.value.payload["error"]
        assert err["code"] == "INVALID_ARGUMENT" and "1-based" in err["message"]


def test_service_path_escape(svc_cfg):
    """服务层 path 逃逸（definition / diagnostics 的 path 过滤）→ PATH_OUTSIDE_WORKSPACE。"""
    for fn in (lambda: find_definition(svc_cfg, "p", "../escape.py", 1, 1),
               lambda: find_references(svc_cfg, "p", "../escape.py", 1, 1),
               lambda: get_diagnostics(svc_cfg, "p", "../escape.py")):
        with pytest.raises(McpError) as ei:
            fn()
        assert ei.value.payload["error"]["code"] == "PATH_OUTSIDE_WORKSPACE"


def test_service_symbols_and_diagnostics(svc_cfg):
    """workspace_symbols 模糊查询 + get_diagnostics path 过滤与 severity 映射（服务层全链）。"""
    syms = workspace_symbols(svc_cfg, "p", "auth")
    assert [s["name"] for s in syms["symbols"]] == ["Authenticator", "Authenticator.login"]
    assert syms["symbols"][0]["kind"] == "Class" and syms["truncated"] is False
    diags = get_diagnostics(svc_cfg, "p", "src/main.py")
    assert [d["severity"] for d in diags["diagnostics"]] == ["error", "warning"]
    assert [d["line"] for d in diags["diagnostics"]] == [4, 8]  # mock 0-based 3/7 → 1-based
    assert diags["diagnostics"][0]["path"] == "src/main.py"
