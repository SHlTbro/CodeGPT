"""task18 GWT：真实 server.py 子进程 + fastmcp Client，intel.servers 指向 fake_lsp.py 的全链 e2e。

空闲端口临时 config（intel.servers.python.command = python fake_lsp.py --refs 120）：
tools/list 11 工具 → 四工具 happy path → 无 LSP 项目 LSP_UNAVAILABLE → path 逃逸/非法行列。
teardown 杀 server 进程并清理临时目录（GWT-9 同款）。
"""
from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from conftest import PKG_DIR, rmrf

FAKE_LSP = PKG_DIR / "tests" / "fixtures" / "fake_lsp.py"
_CONTRACT_DESCS = {  # 契约 v1.3 四工具段 description（逐字，C-010 复验面）
    "find_definition": "跳转到光标处符号的定义位置（基于项目语言服务器）。语言服务器未安装时返回 LSP_UNAVAILABLE 并建议 search_code。",
    "find_references": "查找光标处符号的全部引用位置（含/不含声明可选）。",
    "workspace_symbols": "按名称模糊搜索项目工作区符号（类/函数/变量等，基于语言服务器）。",
    "get_diagnostics": "获取项目（或指定文件）的诊断信息（编译错误/警告，基于语言服务器推送+拉取）。",
}


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _call(result):
    data = getattr(result, "data", None)
    if data is None:
        data = getattr(result, "structured_content", None)
    if data is None and getattr(result, "content", None):
        data = json.loads(result.content[0].text)
    return data


@pytest.fixture(scope="module")
def intel_server(tmp_path_factory):
    base = tmp_path_factory.mktemp("intel-e2e")
    proj = base / "intel-proj"
    (proj / "src").mkdir(parents=True)
    (proj / "pyproject.toml").write_text("", encoding="utf-8")  # 探测标记：python
    (proj / "src" / "main.py").write_text("def main():\n    pass\n", encoding="utf-8")
    nolsp = base / "no-lsp"          # 无任何标记文件 → LSP_UNAVAILABLE
    nolsp.mkdir()
    (nolsp / "a.py").write_text("x = 1\n", encoding="utf-8")  # 目标文件存在，仅缺 LSP
    cfg = {
        "projects": [{"id": "intel-proj", "name": "Intel", "path": str(proj)},
                     {"id": "no-lsp", "name": "NoLsp", "path": str(nolsp)}],
        "server": {"host": "127.0.0.1", "port": _free_port(), "path": "/mcp"},
        "intel": {"servers": {"python": {"command": [sys.executable, str(FAKE_LSP), "--refs", "120"]}}},
    }
    cfg_path = base / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(PKG_DIR / "server.py"), "--config", str(cfg_path)],
        cwd=str(PKG_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    port = cfg["server"]["port"]
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            if proc.poll() is not None:
                out = proc.stdout.read() if proc.stdout else ""
                raise RuntimeError(f"server 提前退出 rc={proc.returncode}: {out[:400]}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    break
            except OSError:
                time.sleep(0.3)
        else:
            raise RuntimeError("server 30s 内未监听")
        yield {"port": port, "base": base}
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        rmrf(base)


def _tools(intel_server):
    from fastmcp import Client

    async def run():
        async with Client(f"http://127.0.0.1:{intel_server['port']}/mcp") as c:
            return await c.list_tools()

    return asyncio.run(run())


def test_tools_list_eleven_with_contract_descriptions(intel_server, contract):
    """GWT-1：恰 15 工具（C-010 迁移 v1.6：14+write_binary_file）；4 intel 工具 description 与契约逐字一致、readOnlyHint=true。"""
    tools = {t.name: t for t in _tools(intel_server)}
    assert set(tools) == {t["name"] for t in contract["tools"]} and len(tools) == 15
    contract_tools = {t["name"]: t for t in contract["tools"]}
    for name, desc in _CONTRACT_DESCS.items():
        assert tools[name].description == desc == contract_tools[name]["description"]
        assert getattr(tools[name].annotations, "readOnlyHint", None) is True
        assert getattr(tools[name].annotations, "openWorldHint", None) is False


def test_four_intel_tools_happy_path(intel_server):
    """GWT-2：definition 1-based；references 120 条截断前 100；symbols 模糊；diagnostics severity 映射。"""
    from fastmcp import Client

    async def run():
        async with Client(f"http://127.0.0.1:{intel_server['port']}/mcp") as c:
            return [
                _call(await c.call_tool("find_definition", {
                    "project_id": "intel-proj", "path": "src/main.py", "line": 3, "character": 5})),
                _call(await c.call_tool("find_references", {
                    "project_id": "intel-proj", "path": "src/main.py", "line": 3, "character": 5})),
                _call(await c.call_tool("workspace_symbols", {
                    "project_id": "intel-proj", "query": "auth"})),
                _call(await c.call_tool("get_diagnostics", {
                    "project_id": "intel-proj", "path": "src/main.py"})),
            ]

    d, r, s, g = asyncio.run(run())
    assert d["project_id"] == "intel-proj"
    assert d["locations"][0] == {"path": "src/main.py", "line": 10, "character": 3}  # mock (9,2) 0-based
    assert len(r["locations"]) == 100 and r["truncated"] is True
    assert r["next_hint"] == "缩小 query 或限定 path"
    assert [x["name"] for x in s["symbols"]] == ["Authenticator", "Authenticator.login"]
    assert s["symbols"][0]["kind"] == "Class"
    assert [(x["severity"], x["line"]) for x in g["diagnostics"]] == [("error", 4), ("warning", 8)]
    assert g["diagnostics"][0]["source"] == "fake-lsp"


def test_no_lsp_project_lsp_unavailable(intel_server):
    """GWT-3：未配 LSP 项目 → LSP_UNAVAILABLE，hint 含安装指引与 search_code 降级。"""
    from fastmcp import Client

    async def run():
        async with Client(f"http://127.0.0.1:{intel_server['port']}/mcp") as c:
            return _call(await c.call_tool("find_definition", {
                "project_id": "no-lsp", "path": "a.py", "line": 1, "character": 1}))

    err = asyncio.run(run())["error"]
    assert err["code"] == "LSP_UNAVAILABLE" and err["recoverable"] is False
    assert "search_code" in err["hint"] and "安装" in err["hint"]


def test_path_escape_and_invalid_position(intel_server):
    """GWT-4：path 逃逸 → PATH_OUTSIDE_WORKSPACE；line=0 → INVALID_ARGUMENT（1-based）。"""
    from fastmcp import Client

    async def run():
        async with Client(f"http://127.0.0.1:{intel_server['port']}/mcp") as c:
            return [
                _call(await c.call_tool("find_definition", {
                    "project_id": "intel-proj", "path": "../../etc/passwd", "line": 1, "character": 1})),
                _call(await c.call_tool("find_definition", {
                    "project_id": "intel-proj", "path": "src/main.py", "line": 0, "character": 1})),
            ]

    e1, e2 = asyncio.run(run())
    assert e1["error"]["code"] == "PATH_OUTSIDE_WORKSPACE"
    assert e2["error"]["code"] == "INVALID_ARGUMENT" and "1-based" in e2["error"]["message"]
