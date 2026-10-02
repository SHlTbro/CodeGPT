"""task13 write_file 集成测试：注册链路（名称/描述/注解/errors ⊆ 契约）+ 真实 FastMCP 调用 + e2e 实写 docs。"""
from __future__ import annotations

import asyncio
import hashlib
import json

import pytest
from fastmcp import Client, FastMCP

import errors
from conftest import rmrf
from tools import register_tools


def _sha(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


@pytest.fixture(scope="module")
def write_server(tmp_path_factory):
    """真实注册链路：write_enabled=true 项目 + false 项目 → FastMCP → Client。"""
    base = tmp_path_factory.mktemp("write-specimen")
    root = base / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "docs/notes.md").write_text("v1\n", encoding="utf-8")
    off = base / "off"
    off.mkdir()
    from config import Config
    from projects import Project
    cfg = Config(projects=(
        Project(id="w", name="W", path=root, write_enabled=True),
        Project(id="r", name="R", path=off, write_enabled=False),
    ))
    mcp = FastMCP(name="write-check")
    register_tools(mcp, cfg)
    yield {"client_factory": lambda: Client(mcp), "root": root, "off": off}
    rmrf(base)


def _call(result):
    data = getattr(result, "data", None)
    if data is None:
        data = getattr(result, "structured_content", None)
    if data is None and getattr(result, "content", None):
        data = json.loads(result.content[0].text)
    return data


def test_write_file_registered_with_contract_description(write_server, contract):
    """write_file 注册：description 逐字用契约 v1.2。"""
    spec = next(t for t in contract["tools"] if t["name"] == "write_file")
    from tools import _DESC_WRITE_FILE
    assert _DESC_WRITE_FILE == spec["description"]


def test_write_file_errors_subset_of_contract(write_server, contract):
    """write_file.errors ⊆ 契约 errorCodes。"""
    spec = next(t for t in contract["tools"] if t["name"] == "write_file")
    all_codes = {e["code"] for e in contract["errorCodes"]}
    assert set(spec["errors"]) <= all_codes


def test_mcp_write_create_and_read_back(write_server):
    """经 FastMCP Client 实写 docs：创建 → read_file 回读内容一致。"""
    root = write_server["root"]

    async def run():
        async with write_server["client_factory"]() as c:
            r1 = _call(await c.call_tool("write_file", {
                "project_id": "w", "path": "docs/plan.md", "content": "# plan\nstep 1\n",
                "expected_absent": True}))
            rd = _call(await c.call_tool("read_file", {
                "project_id": "w", "path": "docs/notes.md"}))
            r2 = _call(await c.call_tool("write_file", {
                "project_id": "w", "path": "docs/notes.md", "content": "v2-updated\n",
                "content_sha256": rd["content_sha256"]}))
            return r1, r2, rd

    r1, r2, rd = asyncio.run(run())
    assert r1["created"] is True and r1["write_seq"] == 1
    assert (root / "docs" / "plan.md").read_text(encoding="utf-8") == "# plan\nstep 1\n"
    assert r2["created"] is False and r2["sha256"] == _sha("v2-updated\n")
    assert (root / "docs" / "notes.md").read_text(encoding="utf-8") == "v2-updated\n"


def test_mcp_write_disabled_error_envelope(write_server):
    """WRITE_DISABLED 经 MCP 信封返回（write_enabled=false 项目）。"""
    async def run():
        async with write_server["client_factory"]() as c:
            return _call(await c.call_tool("write_file", {
                "project_id": "r", "path": "docs/x.md", "content": "c"}))

    e = asyncio.run(run())
    assert e["error"]["code"] == "WRITE_DISABLED" and e["error"]["recoverable"] is False


def test_mcp_write_conflict_error_envelope(write_server):
    """REVISION_CONFLICT 经 MCP 信封返回（带旧 sha 更新）。"""
    root = write_server["root"]
    old = hashlib.sha256(b"stale\n").hexdigest()

    async def run():
        async with write_server["client_factory"]() as c:
            rd = _call(await c.call_tool("read_file", {"project_id": "w", "path": "docs/notes.md"}))
            return _call(await c.call_tool("write_file", {
                "project_id": "w", "path": "docs/notes.md", "content": "x",
                "content_sha256": old})), rd["content_sha256"]

    e, fresh = asyncio.run(run())
    assert e["error"]["code"] == "REVISION_CONFLICT" and e["error"]["recoverable"] is True
    assert e["error"]["hint"]
    assert fresh != old and root.exists()


def test_mcp_write_forbidden_outside_docs(write_server):
    """WRITE_PATH_FORBIDDEN 经 MCP 信封返回（src/evil.py）。"""
    async def run():
        async with write_server["client_factory"]() as c:
            return _call(await c.call_tool("write_file", {
                "project_id": "w", "path": "src/evil.py", "content": "c", "expected_absent": True}))

    e = asyncio.run(run())
    assert e["error"]["code"] == "WRITE_PATH_FORBIDDEN"


def test_mcp_write_sensitive_rejected(write_server):
    """FILE_NOT_VISIBLE 经 MCP 信封返回（docs/.env）。"""
    async def run():
        async with write_server["client_factory"]() as c:
            return _call(await c.call_tool("write_file", {
                "project_id": "w", "path": "docs/.env", "content": "K=1", "expected_absent": True}))

    e = asyncio.run(run())
    assert e["error"]["code"] == "FILE_NOT_VISIBLE"


def test_mcp_write_already_exists(write_server):
    """FILE_ALREADY_EXISTS 经 MCP 信封返回。"""
    async def run():
        async with write_server["client_factory"]() as c:
            return _call(await c.call_tool("write_file", {
                "project_id": "w", "path": "docs/notes.md", "content": "c",
                "expected_absent": True}))

    e = asyncio.run(run())
    assert e["error"]["code"] == "FILE_ALREADY_EXISTS" and e["error"]["recoverable"] is True


def test_mcp_write_payload_too_large(write_server):
    """PAYLOAD_TOO_LARGE 经 MCP 信封返回。"""
    async def run():
        async with write_server["client_factory"]() as c:
            return _call(await c.call_tool("write_file", {
                "project_id": "w", "path": "docs/big.md", "content": "a" * 1_500_000,
                "expected_absent": True}))

    e = asyncio.run(run())
    assert e["error"]["code"] == "PAYLOAD_TOO_LARGE"


def test_read_file_returns_content_sha256(write_server):
    """read_file 输出补 content_sha256（全文 hash），与磁盘一致。"""
    root = write_server["root"]

    async def run():
        async with write_server["client_factory"]() as c:
            return _call(await c.call_tool("read_file", {"project_id": "w", "path": "docs/notes.md"}))

    rd = asyncio.run(run())
    disk = (root / "docs" / "notes.md").read_bytes()
    assert rd["content_sha256"] == hashlib.sha256(disk).hexdigest()
    assert set(rd.keys()) >= {"path", "start_line", "end_line", "content", "encoding", "content_sha256"}
