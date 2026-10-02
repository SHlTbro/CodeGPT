"""task24 e2e：真实 server.py 子进程 + fastmcp Client，write_binary_file 全链路一次通。

ChatGPT 视角：tools/list（恰 15 工具，write_binary_file 写注解）→ base64 写入 .docx 标本
→ read_file 同文件得 BINARY_FILE_UNSUPPORTED（二进制不进文本读）
→ 带首次写入返回的 sha256 更新成功 → 非白名单扩展名 EXTENSION_NOT_ALLOWED 回归。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import PKG_DIR, rmrf

# 真实 .docx 形态标本：ZIP magic（PK\x03\x04）开头 + 局部文件头，解码后逐字节可比
DOCX_SPECIMEN = (
    b"PK\x03\x04\x14\x00\x06\x00\x08\x00\x00\x00!\x00" + b"\x9c\xb1\x1cT" + b"\x00" * 4 +
    b"\x0f\x00\x00\x00\x08\x00\x00\x00[Content_Types].xml"
    b"<?xml version=\"1.0\"?><Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\"/>"
    b"PK\x05\x06" + b"\x00" * 18
)


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
def bin_server(tmp_path_factory):
    """module 级真实 server：开 write 项目 binpro。"""
    base = tmp_path_factory.mktemp("write-bin-e2e")
    binpro = base / "binpro"
    (binpro / "docs").mkdir(parents=True)
    port = _free_port()
    cfg_path = base / "config.yaml"
    cfg_path.write_text(
        "projects:\n"
        f"  - id: binpro\n    name: Binary Demo\n    path: {binpro.as_posix()}\n    write_enabled: true\n"
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
        yield {"proc": proc, "port": port, "binpro": binpro}
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        rmrf(base)


def test_tools_list_fifteen_with_write_binary(bin_server):
    """tools/list：恰 15 工具（C-010 迁移 v1.6：14+write_binary_file）；三写工具，其余只读。"""
    from fastmcp import Client

    async def run():
        async with Client(f"http://127.0.0.1:{bin_server['port']}/mcp") as c:
            return await c.list_tools()

    tools = asyncio.run(run())
    names = sorted(t.name for t in tools)
    # C-010 迁移 v1.6：14 → 15（+write_binary_file）
    assert names == ["apply_workspace_edit", "find_definition", "find_references", "get_diagnostics",
                     "git_info", "list_files", "list_projects", "project_info", "read_batch",
                     "read_file", "rename_preview", "search_code", "workspace_symbols",
                     "write_binary_file", "write_file"]
    assert len(names) == 15
    ann = {t.name: t.annotations for t in tools}
    for n in ("write_binary_file", "write_file", "apply_workspace_edit"):
        assert getattr(ann[n], "readOnlyHint", None) is False, n
        assert getattr(ann[n], "destructiveHint", None) is True, n
    for n in names:
        assert getattr(ann[n], "openWorldHint", None) is False, n
        if n not in ("write_binary_file", "write_file", "apply_workspace_edit"):
            assert getattr(ann[n], "readOnlyHint", None) is True, n


def test_write_binary_full_flow_once(bin_server):
    """全流程一次通：base64 写 .docx 标本 → 落盘逐字节一致 → read_file BINARY_FILE_UNSUPPORTED
    → 带首次返回 sha 更新成功（created=False、sha 与新内容一致）。"""
    from fastmcp import Client
    port, root = bin_server["port"], bin_server["binpro"]
    disk_path = root / "docs" / "秋招岗位" / "09-29-JD雷达.docx"
    b64_docx = base64.b64encode(DOCX_SPECIMEN).decode("ascii")
    b64_v2 = base64.b64encode(DOCX_SPECIMEN + b"v2").decode("ascii")

    async def run():
        async with Client(f"http://127.0.0.1:{port}/mcp") as c:
            w1 = _call(await c.call_tool("write_binary_file", {
                "project_id": "binpro", "path": "docs/秋招岗位/09-29-JD雷达.docx",
                "content_b64": b64_docx, "expected_absent": True}))
            disk_v1 = disk_path.read_bytes()          # 首次写入后立即取证（更新前）
            rd = _call(await c.call_tool("read_file", {
                "project_id": "binpro", "path": "docs/秋招岗位/09-29-JD雷达.docx"}))
            w2 = _call(await c.call_tool("write_binary_file", {
                "project_id": "binpro", "path": "docs/秋招岗位/09-29-JD雷达.docx",
                "content_b64": b64_v2, "content_sha256": w1["sha256"]}))
            return w1, disk_v1, rd, w2

    w1, disk_v1, rd, w2 = asyncio.run(run())
    # 新建：PK magic 落盘、bytes/sha 与解码后内容一致
    assert w1["created"] is True and w1["write_seq"] == 1
    assert w1["bytes_written"] == len(DOCX_SPECIMEN)
    assert w1["sha256"] == hashlib.sha256(DOCX_SPECIMEN).hexdigest()
    assert disk_v1 == DOCX_SPECIMEN
    assert disk_v1.startswith(b"PK\x03\x04")
    # read_file 对二进制 → BINARY_FILE_UNSUPPORTED（不含 content_sha256）
    assert rd["error"]["code"] == "BINARY_FILE_UNSUPPORTED"
    assert rd["error"]["recoverable"] is False and "content_sha256" not in rd
    # 带正确 sha（取自 write_binary_file 返回值，绕过 read 限制）→ 更新成功
    assert w2["created"] is False and w2["write_seq"] == 2
    assert w2["sha256"] == hashlib.sha256(DOCX_SPECIMEN + b"v2").hexdigest()
    assert disk_path.read_bytes() == DOCX_SPECIMEN + b"v2"


def test_write_binary_extension_rejected_over_wire(bin_server):
    """真实链路回归：.exe → EXTENSION_NOT_ALLOWED 信封（code/recoverable=false/hint 完整）。"""
    from fastmcp import Client

    async def run():
        async with Client(f"http://127.0.0.1:{bin_server['port']}/mcp") as c:
            return _call(await c.call_tool("write_binary_file", {
                "project_id": "binpro", "path": "docs/evil.exe",
                "content_b64": base64.b64encode(b"MZ\x90\x00").decode("ascii"),
                "expected_absent": True}))

    e = asyncio.run(run())
    assert e["error"]["code"] == "EXTENSION_NOT_ALLOWED"
    assert e["error"]["recoverable"] is False and e["error"]["hint"]
    assert set(e.keys()) == {"error"} and set(e["error"].keys()) == {"code", "message", "recoverable", "hint"}
    assert not (bin_server["binpro"] / "docs" / "evil.exe").exists()
