"""task08 GWT-1/7 + task07 复验：真实 server.py 子进程 + fastmcp Client 端到端。

端口防冲突：config 注入空闲端口（先 bind :0 探测）。结束后杀进程并断言无残留（GWT-9）。
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

from conftest import PKG_DIR, rmrf

HERE = Path(__file__).resolve().parent


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _call(result):
    """fastmcp CallToolResult → dict（data / structured_content / content 文本兜底）。"""
    data = getattr(result, "data", None)
    if data is None:
        data = getattr(result, "structured_content", None)
    if data is None and getattr(result, "content", None):
        data = json.loads(result.content[0].text)
    return data


@pytest.fixture(scope="module")
def server(sample_repo, git_repos, tmp_path_factory):
    """module 级：一次起服，e2e 全部用例共用；teardown 终止进程并断言退出。"""
    cfg_dir = tmp_path_factory.mktemp("e2e-cfg")
    port = _free_port()
    cfg_path = cfg_dir / "config.yaml"
    cfg_path.write_text(
        "projects:\n"
        f"  - id: alpha\n    name: Alpha Specimen\n    path: {sample_repo.alpha.as_posix()}\n"
        f"  - id: empty-proj\n    name: Empty Project\n    path: {sample_repo.empty.as_posix()}\n"
        f"  - id: spec-clean\n    name: Clean Repo\n    path: {git_repos.clean.as_posix()}\n"
        f"  - id: spec-dirty\n    name: Dirty Repo\n    path: {git_repos.dirty.as_posix()}\n"
        f"server:\n  host: 127.0.0.1\n  port: {port}\n  path: /mcp\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, str(PKG_DIR / "server.py"), "--config", str(cfg_path)],
        cwd=str(PKG_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    deadline = time.time() + 30
    try:
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

        ns = {"proc": proc, "port": port, "base": cfg_dir}
        yield ns
    finally:
        # GWT-9：任何路径（含 setup 抛错）都保证杀进程，无残留
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        rmrf(cfg_dir)


def test_tools_list_exactly_eleven(server):
    from fastmcp import Client

    async def run():
        async with Client(f"http://127.0.0.1:{server['port']}/mcp") as c:
            return await c.list_tools()

    tools = asyncio.run(run())
    names = sorted(t.name for t in tools)
    # C-010 迁移 v1.6：14 → 15（+write_binary_file）
    assert names == ["apply_workspace_edit", "find_definition", "find_references", "get_diagnostics",
                     "git_info", "list_files", "list_projects", "project_info", "read_batch",
                     "read_file", "rename_preview", "search_code", "workspace_symbols",
                     "write_binary_file", "write_file"]
    # 契约 v1.2/v1.4/v1.6：write_file、apply_workspace_edit 与 write_binary_file
    # readOnlyHint=false + destructiveHint=true；其余只读
    ann = {t.name: t.annotations for t in tools}
    assert getattr(ann["write_file"], "readOnlyHint", None) is False
    assert getattr(ann["write_file"], "destructiveHint", None) is True
    assert getattr(ann["apply_workspace_edit"], "readOnlyHint", None) is False
    assert getattr(ann["apply_workspace_edit"], "destructiveHint", None) is True
    assert getattr(ann["write_binary_file"], "readOnlyHint", None) is False
    assert getattr(ann["write_binary_file"], "destructiveHint", None) is True
    for n in names:
        if n not in ("write_file", "apply_workspace_edit", "write_binary_file"):
            assert getattr(ann[n], "readOnlyHint", None) is True
        assert getattr(ann[n], "openWorldHint", None) is False


def test_all_six_tools_happy_path(server):
    from fastmcp import Client

    async def run():
        async with Client(f"http://127.0.0.1:{server['port']}/mcp") as c:
            return [
                _call(await c.call_tool("list_projects", {})),
                _call(await c.call_tool("project_info", {"project_id": "alpha"})),
                _call(await c.call_tool("list_files", {"project_id": "alpha", "path": "."})),
                _call(await c.call_tool("read_file", {"project_id": "alpha", "path": "notes.txt", "start_line": 1, "end_line": 2})),
                _call(await c.call_tool("search_code", {"project_id": "alpha", "query": "authentication"})),
                _call(await c.call_tool("git_info", {"project_id": "spec-dirty"})),
            ]

    p1, p2, p3, p4, p5, p6 = asyncio.run(run())
    assert [x["id"] for x in p1["projects"]] == ["alpha", "empty-proj", "spec-clean", "spec-dirty"]
    assert p2["project_id"] == "alpha" and "files_count" in p2
    assert p3["project_id"] == "alpha" and "notes.txt" in p3["files"]
    assert p4["content"].startswith("authentication here") and p4["end_line"] == 2
    assert p5["project_id"] == "alpha" and any("authentication" in m["text"] for m in p5["matches"])
    assert p6["is_git_repo"] is True and p6["branch"] == "main"
    assert {f["path"]: f["status"] for f in p6["changed_files"]}.get("untracked.txt") == "untracked"


def test_error_envelopes_no_traceback(server):
    from fastmcp import Client

    async def run():
        async with Client(f"http://127.0.0.1:{server['port']}/mcp") as c:
            return [
                _call(await c.call_tool("project_info", {"project_id": "no-such"})),
                _call(await c.call_tool("read_file", {"project_id": "alpha", "path": ".env"})),
                _call(await c.call_tool("read_file", {"project_id": "alpha", "path": "../../etc/passwd"})),
                _call(await c.call_tool("search_code", {"project_id": "alpha", "query": ""})),
            ]

    e1, e2, e3, e4 = asyncio.run(run())
    assert e1["error"]["code"] == "PROJECT_NOT_FOUND" and e1["error"]["recoverable"] is True
    assert e2["error"]["code"] == "FILE_NOT_VISIBLE" and e2["error"]["recoverable"] is False
    assert e3["error"]["code"] == "PATH_OUTSIDE_WORKSPACE" and e3["error"]["recoverable"] is True
    assert e4["error"]["code"] == "INVALID_ARGUMENT"
    for e in (e1, e2, e3, e4):
        assert set(e.keys()) == {"error"} and set(e["error"].keys()) == {"code", "message", "recoverable", "hint"}
        assert "Traceback" not in json.dumps(e)     # 禁止裸 traceback 出现在响应


def test_server_stops_cleanly(server):
    """GWT-9：显式停止后进程退出、端口释放、无残留子进程。

    Windows TerminateProcess 后内核 listen socket 清理存在亚秒级窗口，瞬时 connect 仍可
    成功（本机实测），故以 2s 为限轮询断言端口最终释放（GWT-9 语义：退出 + 端口释放）。
    """
    proc = server["proc"]
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    assert proc.poll() is not None
    deadline = time.time() + 2
    while True:
        try:
            with socket.create_connection(("127.0.0.1", server["port"]), timeout=2):
                pass
        except OSError:
            break                                    # 端口已释放
        if time.time() > deadline:
            pytest.fail(f"进程退出 2s 后端口 {server['port']} 仍未释放")
        time.sleep(0.2)
