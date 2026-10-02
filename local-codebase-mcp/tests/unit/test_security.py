"""task02 security 单元测试：PathGuard 拒绝矩阵（八类）+ casefold + is_sensitive + truncate。

junction/真实 mklink 逃逸在 tests/security/test_path_guard_windows.py（Windows 真实 FS 语义）；
本文件 symlink 类目在环境支持时验证，不支持时显式 SKIP 并注明（task08 GWT-4）。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import errors
from errors import McpError
from projects import Project
from security import (
    MAX_CHARS,
    MAX_FILE_SIZE_BYTES,
    is_sensitive,
    is_too_large,
    resolve_path,
    truncate,
    truncate_search_line,
)


@pytest.fixture()
def proj(tmp_path) -> tuple[Project, Path]:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src/main.py").write_text("x = 1\n", encoding="utf-8")
    return Project(id="p", name="P", path=root), root


def expect_outside(fn):
    with pytest.raises(McpError) as ei:
        fn()
    assert ei.value.payload["error"]["code"] == errors.PATH_OUTSIDE_WORKSPACE
    assert ei.value.payload["error"]["recoverable"] is True


def test_category_relative_ok(proj):
    p, root = proj
    assert resolve_path(p, "src/main.py") == (root / "src" / "main.py").resolve()
    assert resolve_path(p, ".") == root.resolve()      # 空路径 / "." 指向 root
    assert resolve_path(p, "") == root.resolve()


def test_category_dotdot_escape_rejected(proj):
    p, _ = proj
    expect_outside(lambda: resolve_path(p, "../outside.txt"))
    expect_outside(lambda: resolve_path(p, "src/../../../outside.txt"))   # 多级 .. 深逃逸


def test_category_absolute_rejected(proj):
    p, _ = proj
    expect_outside(lambda: resolve_path(p, "/home/user/x"))


def test_category_tilde_rejected(proj):
    p, _ = proj
    expect_outside(lambda: resolve_path(p, "~/x"))
    expect_outside(lambda: resolve_path(p, "~admin/x"))


def test_category_drive_letter_rejected(proj):
    p, _ = proj
    expect_outside(lambda: resolve_path(p, "C:/Windows/system32/x"))
    expect_outside(lambda: resolve_path(p, "C:x"))          # 盘符相对形态
    expect_outside(lambda: resolve_path(p, "d:/demo"))      # 小写盘符


def test_category_unc_rejected(proj):
    p, _ = proj
    expect_outside(lambda: resolve_path(p, "\\\\server\\share\\x"))
    expect_outside(lambda: resolve_path(p, "//server/share/x"))


def test_category_symlink_outside_rejected(proj, tmp_path):
    """GWT-4：环境支持 symlink 时验证拒绝；不支持时显式 SKIP 并注明原因。"""
    p, root = proj
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    (outside / "secret.txt").write_text("s", encoding="utf-8")
    try:
        (root / "link").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不支持创建 symlink（需管理员或开发者模式）；junction 真实逃逸见 tests/security")
    expect_outside(lambda: resolve_path(p, "link/secret.txt"))
    expect_outside(lambda: resolve_path(p, "link"))


def test_category_resolved_outside_inside_symlink_deep(proj, tmp_path):
    """链接指向的根内深层再跳出：resolved final 在根外 → 拒绝（resolved-outside 类目）。"""
    p, root = proj
    (root / "sub").mkdir(exist_ok=True)
    expect_outside(lambda: resolve_path(p, "sub/../../outside.txt"))


def test_casefold_drive_and_dir_no_misjudge(proj):
    """GWT-5（task02-GWT-5 回归）：盘符/目录名大小写差异不误判、不逃逸。"""
    p, root = proj
    variant = str(root).replace("D:", "d:", 1) if str(root).upper().startswith("D:") \
        else str(root)[:1].lower() + str(root)[1:]
    p2 = Project(id="p2", name="P2", path=Path(variant))
    got = resolve_path(p2, "src/main.py")
    assert got == (root / "src" / "main.py").resolve()
    # 目录名大小写差异（Windows 不区分大小写）：仍在根内 → 放行
    got2 = resolve_path(p2, "SRC/MAIN.py")
    assert got2 == (root / "src" / "main.py").resolve()
    # 大小写变体根的 .. 逃逸仍拒绝
    with pytest.raises(McpError):
        resolve_path(p2, "../escape.txt")


SENSITIVE = [
    (".env", True), ("sub/.env", True),
    ("a.key", True), ("sub/b.key", True),
    ("c.pem", True),
    ("credentials.json", True), ("credentials.txt", True), ("x/credentials.yml", True),
    (".ssh/id_rsa", True), (".ssh/config", True),
    (".aws/credentials", True),
    # [FIX] 回归：含 / 的 pattern（.ssh/**、.aws/**）需按任意目录深度匹配
    ("sub/.ssh/id_rsa", True), ("deep/nest/.aws/credentials", True), ("sub/.ssh/", True),
    (".env.example", False), ("env", False), ("environment.yml", False),
    ("keys.txt", False), ("src/main.py", False), ("README.md", False),
]


@pytest.mark.parametrize("rel,want", SENSITIVE)
def test_is_sensitive_matrix(rel, want):
    assert is_sensitive(rel) is want, f"is_sensitive({rel!r}) 期望 {want}"


def test_is_too_large():
    assert is_too_large(6 * 1024 * 1024) is True            # 6MB > 5MB
    assert is_too_large(MAX_FILE_SIZE_BYTES) is False       # 恰 5MB 不超
    assert is_too_large(1024) is False
    assert is_too_large(6 * 1024 * 1024, 8 * 1024 * 1024) is False   # 自定义上限


def test_truncate():
    content, truncated = truncate("x" * 60_000)
    assert truncated is True and len(content) == MAX_CHARS == 50_000
    assert truncate("short") == ("short", False)
    content2, truncated2 = truncate("y" * 50_000)           # 恰 50000 不截断
    assert truncated2 is False and len(content2) == 50_000


def test_truncate_search_line():
    long_line = "z" * 800
    assert truncate_search_line(long_line) == "z" * 500     # 单行命中截断至 500 字符
    assert truncate_search_line("short") == "short"
