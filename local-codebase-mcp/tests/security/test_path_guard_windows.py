"""task08 GWT-3/4/5：Windows 真实文件系统语义 —— 真实 junction（mklink /J）逃逸拒绝 + symlink skip 语义。"""
from __future__ import annotations

import subprocess
import sys

import pytest

import errors
from errors import McpError
from projects import Project
from security import resolve_path

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="junction 逃逸 case 仅适用 Windows")


@pytest.fixture()
def junction_repo(tmp_path):
    """项目根内建 junction 指向根外目录（真实 mklink /J，非 monkeypatch）。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("TOP SECRET", encoding="utf-8")
    link = repo / "escape_junction"
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0 or not link.exists():
        pytest.fail(f"真实 junction 创建失败（mklink /J）: {r.stderr or r.stdout}")
    yield Project(id="j", name="J", path=repo), repo, outside, link
    # teardown：先删 junction 本体（只删链接，不动目标），再交给 tmp 清理
    try:
        link.unlink()
    except OSError:
        pass


def test_real_junction_escape_rejected(junction_repo):
    """GWT-3：真实 junction 指向项目外，解析 → PATH_OUTSIDE_WORKSPACE 拒绝。"""
    proj, repo, outside, link = junction_repo
    assert (link / "secret.txt").is_file()          # junction 真实存在且可达
    with pytest.raises(McpError) as ei:
        resolve_path(proj, "escape_junction/secret.txt")
    assert ei.value.payload["error"]["code"] == errors.PATH_OUTSIDE_WORKSPACE
    with pytest.raises(McpError):
        resolve_path(proj, "escape_junction")       # junction 目录本身也拒绝


def test_real_junction_escape_blocked_at_service_layer(junction_repo):
    """服务层同闸：read_file 经 junction 逃逸 → PATH_OUTSIDE_WORKSPACE（真实 FS，非 mock）。"""
    from config import Config
    from tools_services import read_file
    proj, repo, outside, link = junction_repo
    cfg = Config(projects=(proj,))
    with pytest.raises(McpError) as ei:
        read_file(cfg, "j", "escape_junction/secret.txt")
    assert ei.value.payload["error"]["code"] == errors.PATH_OUTSIDE_WORKSPACE


def test_symlink_escape_rejected_or_explicit_skip(tmp_path):
    """GWT-4：环境支持 symlink 时验证拒绝；不支持时显式 SKIP 并注明原因。"""
    repo = tmp_path / "repo-sym"
    repo.mkdir()
    outside = tmp_path / "outside-sym"
    outside.mkdir()
    (outside / "secret.txt").write_text("TOP SECRET", encoding="utf-8")
    link = repo / "escape_symlink"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"当前环境不支持创建 symlink（需管理员或开发者模式）：{e}")
    proj = Project(id="s", name="S", path=repo)
    with pytest.raises(McpError) as ei:
        resolve_path(proj, "escape_symlink/secret.txt")
    assert ei.value.payload["error"]["code"] == errors.PATH_OUTSIDE_WORKSPACE


def test_junction_inside_root_still_allowed(tmp_path):
    """根内真实子目录不受影响（对照：junction 拒绝是因逃逸，不是因路径存在）。"""
    repo = tmp_path / "repo-ok"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src/main.py").write_text("x = 1\n", encoding="utf-8")
    proj = Project(id="ok", name="OK", path=repo)
    assert resolve_path(proj, "src/main.py") == (repo / "src" / "main.py").resolve()
