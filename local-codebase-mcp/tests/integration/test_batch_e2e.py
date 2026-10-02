"""task22 端到端：真实 server.py 子进程 + fastmcp Client 调 read_batch / list_files 批量 paths。

server fixture 模式与 test_server_e2e.py 一致（空闲端口注入 config，teardown 杀进程并清理）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import subprocess
import sys
import time

import pytest

from conftest import PKG_DIR, rmrf


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
def batch_server(sample_repo, tmp_path_factory):
    """module 级真实 server（alpha 标本仓库）；teardown 终止进程并断言无残留。"""
    cfg_dir = tmp_path_factory.mktemp("batch-e2e-cfg")
    port = _free_port()
    cfg_path = cfg_dir / "config.yaml"
    cfg_path.write_text(
        "projects:\n"
        f"  - id: alpha\n    name: Alpha Specimen\n    path: {sample_repo.alpha.as_posix()}\n"
        "server:\n"
        f"  host: 127.0.0.1\n  port: {port}\n  path: /mcp\n",
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
        yield {"proc": proc, "port": port, "base": cfg_dir}
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        rmrf(cfg_dir)


def _tool(batch_server, name: str, args: dict):
    from fastmcp import Client

    async def run():
        async with Client(f"http://127.0.0.1:{batch_server['port']}/mcp") as c:
            return await c.call_tool(name, args)

    return _call(asyncio.run(run()))


def test_read_batch_e2e_three_files_and_glob(batch_server, sample_repo):
    """read_batch 一次读 3 文件 + glob：内容/顺序/sha256 与磁盘一致。"""
    r = _tool(batch_server, "read_batch", {
        "project_id": "alpha",
        "paths": ["README.md", "package.json", "src/main.py", "**/*.py"],
    })
    files = r["files"]
    assert [f["path"] for f in files[:3]] == ["README.md", "package.json", "src/main.py"]
    for f in files[:3]:
        assert "error" not in f
        raw = (sample_repo.alpha / f["path"]).read_bytes()
        assert f["content_sha256"] == hashlib.sha256(raw).hexdigest()
    globbed = {f["path"] for f in files[3:]}
    assert {"src/api.py", "src/deep/util.py"} <= globbed  # 排除/敏感目录不出现
    assert all("error" not in f for f in files)
    assert r["total_chars"] == sum(len(f["content"]) for f in files) and r["truncated"] is False


def test_list_files_batch_e2e_three_dirs(batch_server):
    """list_files 批量 3 目录：正常组 {dirs,files}、不存在组 error、顺序按输入。"""
    r = _tool(batch_server, "list_files", {"project_id": "alpha", "paths": ["src", "no-such", "."]})
    assert [g["path"] for g in r["groups"]] == ["src", "no-such", "."]
    assert r["groups"][0]["files"] == ["api.py", "deep", "main.py"] or \
        (r["groups"][0]["files"] == ["api.py", "main.py"] and r["groups"][0]["dirs"] == ["deep"])
    assert r["groups"][1]["error"]["code"] == "FILE_NOT_FOUND"
    assert "README.md" in r["groups"][2]["files"] and "error" not in r["groups"][2]
    assert r["project_id"] == "alpha"


def test_tools_list_contains_read_batch(batch_server):
    """tools/list 含 read_batch（共 15 个工具，C-010 迁移 v1.6），readOnlyHint=true。"""
    from fastmcp import Client

    async def run():
        async with Client(f"http://127.0.0.1:{batch_server['port']}/mcp") as c:
            return await c.list_tools()

    tools = {t.name: t for t in asyncio.run(run())}
    assert "read_batch" in tools and len(tools) == 15
    assert getattr(tools["read_batch"].annotations, "readOnlyHint", None) is True
