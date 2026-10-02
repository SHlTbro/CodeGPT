"""task15 GWT-2/3：真实 server.py 子进程 + fastmcp Client，开 write 演示项目全流程一次通。

ChatGPT 视角：tools/list（7 工具 + write_file 注解）→ read_file 取 content_sha256 →
write_file 新建 → 再 read 验证内容与 sha → 进程外手改 → 旧 sha 更新被 REVISION_CONFLICT 拒
→ 重读取新 sha → 更新成功。第二项目不配 write_enabled（默认关）→ WRITE_DISABLED 回归证明。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import PKG_DIR, rmrf


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
def write_e2e_server(tmp_path_factory):
    """module 级真实 server：开 write 演示项目 docpro + 未配 write_enabled（默认关）的 locked 项目。"""
    base = tmp_path_factory.mktemp("write-e2e")
    docpro = base / "docpro"
    (docpro / "docs").mkdir(parents=True)
    (docpro / "docs/quickstart.md").write_bytes(b"# quickstart\nhello docs\n")
    locked = base / "locked"
    (locked / "docs").mkdir(parents=True)
    port = _free_port()
    cfg_path = base / "config.yaml"
    cfg_path.write_text(
        "projects:\n"
        f"  - id: docpro\n    name: Doc Demo\n    path: {docpro.as_posix()}\n    write_enabled: true\n"
        f"  - id: locked\n    name: Locked\n    path: {locked.as_posix()}\n"
        "server:\n  host: 127.0.0.1\n"
        f"  port: {port}\n  path: /mcp\n",
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
        yield {"proc": proc, "port": port, "docpro": docpro}
    finally:
        # teardown 任何路径都杀进程，无残留（GWT-9）
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        rmrf(base)


def test_tools_list_eleven_with_write_annotations(write_e2e_server):
    """tools/list：15 工具（C-010 迁移 v1.6：14+write_binary_file）；写注解三工具，其余只读。"""
    from fastmcp import Client

    async def run():
        async with Client(f"http://127.0.0.1:{write_e2e_server['port']}/mcp") as c:
            return await c.list_tools()

    tools = asyncio.run(run())
    names = sorted(t.name for t in tools)
    # C-010 迁移 v1.6：14 → 15（+write_binary_file）
    assert names == ["apply_workspace_edit", "find_definition", "find_references", "get_diagnostics",
                     "git_info", "list_files", "list_projects", "project_info", "read_batch",
                     "read_file", "rename_preview", "search_code", "workspace_symbols",
                     "write_binary_file", "write_file"]
    ann = {t.name: t.annotations for t in tools}
    assert getattr(ann["write_file"], "readOnlyHint", None) is False
    assert getattr(ann["write_file"], "destructiveHint", None) is True
    assert getattr(ann["apply_workspace_edit"], "readOnlyHint", None) is False
    assert getattr(ann["apply_workspace_edit"], "destructiveHint", None) is True
    assert getattr(ann["write_binary_file"], "readOnlyHint", None) is False
    assert getattr(ann["write_binary_file"], "destructiveHint", None) is True
    for n in names:
        assert getattr(ann[n], "openWorldHint", None) is False
        if n not in ("write_file", "apply_workspace_edit", "write_binary_file"):
            assert getattr(ann[n], "readOnlyHint", None) is True


def test_write_full_flow_once(write_e2e_server):
    """全流程一次通：read→write 新建→再 read 验证→进程外手改→REVISION_CONFLICT→重读→更新成功。"""
    from fastmcp import Client
    port = write_e2e_server["port"]
    root = write_e2e_server["docpro"]

    async def run():
        async with Client(f"http://127.0.0.1:{port}/mcp") as c:
            rd = _call(await c.call_tool("read_file", {"project_id": "docpro", "path": "docs/quickstart.md"}))
            w1 = _call(await c.call_tool("write_file", {
                "project_id": "docpro", "path": "docs/xx.md",
                "content": "# demo\nfirst draft\n", "expected_absent": True}))
            rd2 = _call(await c.call_tool("read_file", {"project_id": "docpro", "path": "docs/xx.md"}))
            # 进程外手改（模拟用户/他人覆盖，AI 会话外发生）
            (root / "docs" / "xx.md").write_bytes("# demo\nexternally edited\n".encode("utf-8"))
            conflict = _call(await c.call_tool("write_file", {
                "project_id": "docpro", "path": "docs/xx.md",
                "content": "# demo\nstale overwrite\n", "content_sha256": rd2["content_sha256"]}))
            conflict_disk = (root / "docs" / "xx.md").read_text(encoding="utf-8")  # 冲突后磁盘未被覆盖
            rd3 = _call(await c.call_tool("read_file", {"project_id": "docpro", "path": "docs/xx.md"}))
            w2 = _call(await c.call_tool("write_file", {
                "project_id": "docpro", "path": "docs/xx.md",
                "content": "# demo\nsecond draft\n", "content_sha256": rd3["content_sha256"]}))
            rd4 = _call(await c.call_tool("read_file", {"project_id": "docpro", "path": "docs/xx.md"}))
            return rd, w1, rd2, conflict, conflict_disk, rd3, w2, rd4

    rd, w1, rd2, conflict, conflict_disk, rd3, w2, rd4 = asyncio.run(run())
    fresh = hashlib.sha256("# demo\nfirst draft\n".encode("utf-8")).hexdigest()
    assert rd["content_sha256"] == hashlib.sha256(b"# quickstart\nhello docs\n").hexdigest()
    assert w1["created"] is True and w1["path"] == "docs/xx.md" and w1["write_seq"] == 1
    assert w1["sha256"] == fresh and w1["bytes_written"] == len("# demo\nfirst draft\n".encode("utf-8"))
    assert rd2["content"] == "# demo\nfirst draft" and rd2["content_sha256"] == fresh
    # 旧 sha 更新 → REVISION_CONFLICT（recoverable），手改内容未被覆盖
    assert conflict["error"]["code"] == "REVISION_CONFLICT"
    assert conflict["error"]["recoverable"] is True and conflict["error"]["hint"]
    assert conflict_disk == "# demo\nexternally edited\n"
    edited = hashlib.sha256("# demo\nexternally edited\n".encode("utf-8")).hexdigest()
    assert rd3["content_sha256"] == edited
    # 重读取新 sha → 更新成功；最终内容与 sha 一致
    final = hashlib.sha256("# demo\nsecond draft\n".encode("utf-8")).hexdigest()
    assert w2["created"] is False and w2["write_seq"] == 2 and w2["sha256"] == final
    assert rd4["content"] == "# demo\nsecond draft" and rd4["content_sha256"] == final


def test_write_disabled_default_off(write_e2e_server):
    """GWT-3：未配 write_enabled 的项目（默认关）→ WRITE_DISABLED。"""
    from fastmcp import Client

    async def run():
        async with Client(f"http://127.0.0.1:{write_e2e_server['port']}/mcp") as c:
            return _call(await c.call_tool("write_file", {
                "project_id": "locked", "path": "docs/x.md", "content": "c", "expected_absent": True}))

    e = asyncio.run(run())
    assert e["error"]["code"] == "WRITE_DISABLED"
    assert e["error"]["recoverable"] is False and "write_enabled" in e["error"]["hint"]
    assert not (write_e2e_server["docpro"].parent / "locked" / "docs" / "x.md").exists()
