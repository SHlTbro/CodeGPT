"""task21 GWT：真实 server.py 子进程 + fastmcp Client，两段式 workspace 重构全链 e2e。

ChatGPT 视角：rename_preview 预览跨 2 文件重命名 → read_file 取各文件 content_sha256 →
apply_workspace_edit 全 sha 校验后原子应用 → 再 read 验证内容；sha 不匹配整体拒绝零落盘；
write_enabled=false 项目 → WRITE_DISABLED。intel.servers.python 指向 fake_lsp.py（rename 模式）。
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
import yaml

from conftest import PKG_DIR, rmrf

FAKE_LSP = PKG_DIR / "tests" / "fixtures" / "fake_lsp.py"

MAIN_OLD = "def main():\n    a = 1\n    b = 2\n    c = 3\n    d = 4\n    e = 5\n    old_one()\n"
HELPER_OLD = "import main\nfrom main import main\nold_x = 1\n"
MAIN_NEW = "def renamed():\n    a = 1\n    b = 2\n    c = 3\n    d = 4\n    e = 5\n    renamedone()\n"
HELPER_NEW = "import main\nfrom main import main\nrenamed = 1\n"


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
def ws_server(tmp_path_factory):
    """module 级真实 server：开 write 的 wsproj + 未开 write 的 wslocked；LSP 指向 fake_lsp。"""
    base = tmp_path_factory.mktemp("workspace-e2e")
    proj = base / "wsproj"
    (proj / "src").mkdir(parents=True)
    (proj / "pyproject.toml").write_text("", encoding="utf-8")
    (proj / "src" / "main.py").write_bytes(MAIN_OLD.encode("utf-8"))
    (proj / "src" / "helper.py").write_bytes(HELPER_OLD.encode("utf-8"))
    locked = base / "wslocked"
    (locked / "src").mkdir(parents=True)
    (locked / "pyproject.toml").write_text("", encoding="utf-8")
    (locked / "src" / "main.py").write_bytes(MAIN_OLD.encode("utf-8"))
    cfg = {
        "projects": [{"id": "wsproj", "name": "WS", "path": str(proj), "write_enabled": True},
                     {"id": "wslocked", "name": "Locked", "path": str(locked)}],
        "server": {"host": "127.0.0.1", "port": _free_port(), "path": "/mcp"},
        "intel": {"servers": {"python": {"command": [sys.executable, str(FAKE_LSP)]}}},
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
        yield {"port": port, "base": base, "proj": proj}
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        rmrf(base)


def _run(port, coro_fn):
    from fastmcp import Client

    async def run():
        async with Client(f"http://127.0.0.1:{port}/mcp") as c:
            return await coro_fn(c)

    return asyncio.run(run())


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_workspace_full_pipeline(ws_server):
    """GWT-1/2/3 e2e 闭环：preview → read 取 sha → apply → 再 read 验证内容与 sha_after。"""
    preview = _run(ws_server["port"], lambda c: c.call_tool("rename_preview", {
        "project_id": "wsproj", "path": "src/main.py", "line": 1, "character": 5,
        "new_name": "renamed"}))
    plan = _call(preview)
    assert plan["file_count"] == 2 and plan["edit_count"] == 3
    by_path = {f["path"]: f["edits"] for f in plan["files"]}
    assert set(by_path) == {"src/main.py", "src/helper.py"}
    assert by_path["src/main.py"][0] == {
        "line": 1, "character": 5, "end_line": 1, "end_character": 8, "new_text": "renamed"}

    reads = _run(ws_server["port"], lambda c: asyncio.gather(
        c.call_tool("read_file", {"project_id": "wsproj", "path": "src/main.py"}),
        c.call_tool("read_file", {"project_id": "wsproj", "path": "src/helper.py"})))
    shas = {r["path"]: r["content_sha256"] for r in map(_call, reads)}
    assert shas == {"src/main.py": _sha(ws_server["proj"] / "src/main.py"),
                    "src/helper.py": _sha(ws_server["proj"] / "src/helper.py")}

    applied = _call(_run(ws_server["port"], lambda c: c.call_tool("apply_workspace_edit", {
        "project_id": "wsproj",
        "files": [{"path": p, "edits": e} for p, e in by_path.items()],
        "expected_shas": shas})))
    assert applied["files_changed"] == 2
    after = {a["path"]: a["sha256"] for a in applied["applied"]}
    assert after["src/main.py"] == _sha(ws_server["proj"] / "src/main.py")
    assert after["src/helper.py"] == _sha(ws_server["proj"] / "src/helper.py")
    verify = _run(ws_server["port"], lambda c: c.call_tool("read_file", {
        "project_id": "wsproj", "path": "src/main.py"}))
    # read_file 按行 rstrip 换行输出 → 末行不含 \n
    assert _call(verify)["content"] == MAIN_NEW.rstrip("\n")
    assert (ws_server["proj"] / "src/helper.py").read_bytes().decode("utf-8") == HELPER_NEW


def test_workspace_sha_conflict_rejects_all(ws_server):
    """过期 sha → REVISION_CONFLICT 整体拒绝；e2e 再证零落盘。"""
    files = [{"path": "src/main.py", "edits": [{"line": 1, "character": 1, "end_line": 1,
                                                "end_character": 4, "new_text": "X"}]}]
    r = _call(_run(ws_server["port"], lambda c: c.call_tool("apply_workspace_edit", {
        "project_id": "wsproj", "files": files, "expected_shas": {"src/main.py": "0" * 64}})))
    assert r["error"]["code"] == "REVISION_CONFLICT" and r["error"]["recoverable"] is True


def test_workspace_locked_project_write_disabled(ws_server):
    """write_enabled=false 项目 → WRITE_DISABLED（工具层信封）。"""
    r = _call(_run(ws_server["port"], lambda c: c.call_tool("apply_workspace_edit", {
        "project_id": "wslocked", "files": [], "expected_shas": {}})))
    assert r["error"]["code"] == "WRITE_DISABLED"


def test_workspace_audit_log_written(ws_server):
    """审计：server.log 每文件一行 {tool=apply_workspace_edit, path, bytes, sha_before, sha_after, result}，无内容。"""
    log_path = PKG_DIR / "server.log"
    assert log_path.is_file()
    hits = [ln for ln in log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            if "tool=apply_workspace_edit" in ln and "path=src/main.py" in ln
            and "project_id=wsproj" in ln]
    assert hits, "server.log 缺 apply_workspace_edit 审计行"
    line = hits[-1]
    for token in ("bytes=", "sha_before=", "sha_after=", "result="):
        assert token in line, f"审计行缺 {token}: {line}"
    assert MAIN_NEW.splitlines()[0] not in line  # 审计不含文件内容
