"""task15 写逃逸矩阵（真实 FS 语义）：junction/symlink 写穿、逐层穿越、绝对/UNC/盘符/~、白名单纵深、归一与敏感拒绝。

约束：junction 用真实 mklink /J（非 monkeypatch）；symlink 不可用时显式 skip 注明。
"""
from __future__ import annotations

import subprocess
import sys

import pytest

import errors
import writer
from config import Config
from errors import McpError
from projects import Project
from writer import write_file

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="junction/盘符/UNC 逃逸 case 仅适用 Windows")


def expect_err(fn, code: str) -> McpError:
    with pytest.raises(McpError) as ei:
        fn()
    assert ei.value.payload["error"]["code"] == code, f"期望 {code}，实际 {ei.value.payload['error']['code']}"
    return ei.value


def _mkjunction(link, target) -> None:
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0 and link.exists(), f"真实 junction 创建失败（mklink /J）: {r.stderr or r.stdout}"


def _drop_junction(link) -> None:
    try:
        link.unlink()          # 只删 reparse point 本体，不动 junction 目标
    except OSError:
        pass


@pytest.fixture()
def wrepo(tmp_path):
    root = tmp_path / "repo"
    (root / "docs/sub").mkdir(parents=True)
    (root / "docs/notes.md").write_text("v1\n", encoding="utf-8")
    writer._LOCKS.clear()      # write_seq 进程内存态，测试间复位（契约允许重启归零）
    writer._SEQS.clear()
    return Config(projects=(Project(id="on", name="On", path=root, write_enabled=True),)), root


# ---------------------------------------------------------------- 逐层穿越矩阵（1 层 / 2 层 / 混合 ./ / 出 root）
@pytest.mark.parametrize("rel,code", [
    ("docs/../x.md", errors.WRITE_PATH_FORBIDDEN),                  # 1 层出 docs，仍在 root 内 → 白名单拒
    ("docs/sub/../../x.md", errors.WRITE_PATH_FORBIDDEN),           # 2 层出 docs，仍在 root 内
    ("docs/a/../../x.md", errors.WRITE_PATH_FORBIDDEN),             # 2 层出 docs（子目录变体）
    ("docs/./../x.md", errors.WRITE_PATH_FORBIDDEN),                # 混合 ./ 的穿越
    ("docs/a/b/../../../../x.md", errors.PATH_OUTSIDE_WORKSPACE),   # 穿出 root → 路径守卫拒
    ("../x.md", errors.PATH_OUTSIDE_WORKSPACE),
])
def test_traversal_matrix(wrepo, rel, code):
    cfg, root = wrepo
    expect_err(lambda: write_file(cfg, "on", rel, "c", expected_absent=True), code)
    assert not (root / "x.md").exists() and not (root / "docs" / "x.md").exists()


# ---------------------------------------------------------------- 绝对路径 / UNC / 盘符 / ~（一律拒绝，禁止 ~ 展开）
@pytest.mark.parametrize("rel", ["/etc/passwd", "D:/evil.md", "//srv/share/x.md",
                                 "\\\\srv\\share\\x.md", "~/evil.md", "~user/x.md"])
def test_absolute_unc_drive_tilde_rejected(wrepo, rel):
    cfg, _ = wrepo
    expect_err(lambda: write_file(cfg, "on", rel, "c", expected_absent=True), errors.PATH_OUTSIDE_WORKSPACE)


# ---------------------------------------------------------------- 白名单纵深正例：非只允许一层
def test_nested_docs_positive(wrepo):
    cfg, root = wrepo
    r = write_file(cfg, "on", "docs/a/b/c.md", "deep", expected_absent=True)
    assert r["path"] == "docs/a/b/c.md" and r["created"] is True
    assert (root / "docs/a/b/c.md").read_text(encoding="utf-8") == "deep"


# ---------------------------------------------------------------- 大小写与 ./ 归一（final resolved path 口径）
def test_case_and_dot_normalization(wrepo):
    cfg, root = wrepo
    r1 = write_file(cfg, "on", "DOCS/X.MD", "upper", expected_absent=True)
    r2 = write_file(cfg, "on", "docs/./y.md", "dot", expected_absent=True)
    assert r1["path"] == "docs/X.MD" and r2["path"] == "docs/y.md"
    assert (root / "docs/X.MD").read_text(encoding="utf-8") == "upper"
    assert (root / "docs/y.md").read_text(encoding="utf-8") == "dot"


# ---------------------------------------------------------------- 敏感过滤同样约束写（含 docs 子目录）
@pytest.mark.parametrize("rel", ["docs/.env", "docs/sub/.env", "docs/x.key"])
def test_sensitive_rejected(wrepo, rel):
    cfg, root = wrepo
    expect_err(lambda: write_file(cfg, "on", rel, "K=1", expected_absent=True), errors.FILE_NOT_VISIBLE)
    assert not (root / rel).exists()


# ---------------------------------------------------------------- 真实 junction 写穿：docs/link → 项目外
def test_docs_junction_write_through_rejected(wrepo, tmp_path):
    cfg, root = wrepo
    outside = tmp_path / "outside-j"
    outside.mkdir()
    link = root / "docs/jlink"
    _mkjunction(link, outside)
    try:
        expect_err(lambda: write_file(cfg, "on", "docs/jlink/x.md", "c", expected_absent=True),
                   errors.PATH_OUTSIDE_WORKSPACE)
        assert not (outside / "x.md").exists()      # 写穿未发生
    finally:
        _drop_junction(link)


def test_root_junction_resolves_normally(tmp_path):
    """对比 case：root 本身是 junction 指向真实目录 → 正常解析可写（拒绝的是逃逸，不是 junction 本身）。"""
    real = tmp_path / "real-root"
    (real / "docs").mkdir(parents=True)
    jroot = tmp_path / "root-j"
    _mkjunction(jroot, real)
    cfg = Config(projects=(Project(id="j", name="J", path=jroot, write_enabled=True),))
    r = write_file(cfg, "j", "docs/ok.md", "via-root-junction", expected_absent=True)
    assert r["path"] == "docs/ok.md"
    assert (real / "docs/ok.md").read_text(encoding="utf-8") == "via-root-junction"


# ---------------------------------------------------------------- symlink 逃逸（开发者模式/管理员可用，否则显式 skip）
def test_docs_symlink_escape_rejected_or_skip(wrepo, tmp_path):
    cfg, root = wrepo
    outside = tmp_path / "outside-s"
    outside.mkdir()
    link = root / "docs/slink"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"当前环境不支持创建 symlink（需管理员或开发者模式）：{e}")
    try:
        expect_err(lambda: write_file(cfg, "on", "docs/slink/x.md", "c", expected_absent=True),
                   errors.PATH_OUTSIDE_WORKSPACE)
        assert not (outside / "x.md").exists()
    finally:
        _drop_junction(link)
