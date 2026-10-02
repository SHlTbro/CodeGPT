"""task13 write_file 单元测试：安全链顺序、白名单逃逸矩阵、冲突语义、锁与原子写。"""
from __future__ import annotations

import asyncio
import hashlib
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import errors
import writer
from config import Config
from errors import McpError
from projects import Project
from tools_services import read_file
from writer import MAX_WRITE_BYTES, write_file


def sha_of(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def expect_err(fn, code: str) -> McpError:
    with pytest.raises(McpError) as ei:
        fn()
    assert ei.value.payload["error"]["code"] == code, f"期望 {code}，实际 {ei.value.payload['error']['code']}"
    return ei.value


@pytest.fixture()
def wrepo(tmp_path) -> tuple[Config, Path]:
    """write_enabled=true 项目 + write_enabled=false 项目，docs/ 预建。"""
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "docs/notes.md").write_text("v1\n", encoding="utf-8")
    off = tmp_path / "off"
    off.mkdir()
    on = Project(id="on", name="On", path=root, write_enabled=True)
    offp = Project(id="off", name="Off", path=off, write_enabled=False)
    # write_seq 为进程内存态：测试间重置（契约允许重启归零，测试口径对齐）
    writer._LOCKS.clear()
    writer._SEQS.clear()
    return Config(projects=(on, offp)), root


# ---------------------------------------------------------------- 安全链 1-2：registry / write_enabled
def test_write_disabled_by_default(tmp_path):
    """GWT-11：write_enabled 缺省=false → WRITE_DISABLED。"""
    root = tmp_path / "r"
    root.mkdir()
    cfg = Config(projects=(Project(id="d", name="D", path=root),))   # 未传 write_enabled
    expect_err(lambda: write_file(cfg, "d", "docs/x.md", "c"), errors.WRITE_DISABLED)


def test_write_disabled_project(wrepo):
    """GWT-11：显式 write_enabled=false → WRITE_DISABLED，且未产生文件。"""
    cfg, root = wrepo
    expect_err(lambda: write_file(cfg, "off", "docs/x.md", "c"), errors.WRITE_DISABLED)
    assert not (root.parent / "off" / "docs").exists()


def test_project_not_found(wrepo):
    """安全链第 1 步：project_id 未登记 → PROJECT_NOT_FOUND。"""
    cfg, _ = wrepo
    expect_err(lambda: write_file(cfg, "ghost", "docs/x.md", "c"), errors.PROJECT_NOT_FOUND)


# ---------------------------------------------------------------- 安全链 4-5：逃逸与白名单
def test_escape_dotdot(wrepo):
    """.. 逃逸 → PATH_OUTSIDE_WORKSPACE。"""
    cfg, _ = wrepo
    expect_err(lambda: write_file(cfg, "on", "../evil.md", "c"), errors.PATH_OUTSIDE_WORKSPACE)


def test_escape_absolute_and_drive(wrepo):
    """绝对路径/盘符 → PATH_OUTSIDE_WORKSPACE。"""
    cfg, _ = wrepo
    expect_err(lambda: write_file(cfg, "on", "D:/evil.md", "c"), errors.PATH_OUTSIDE_WORKSPACE)
    expect_err(lambda: write_file(cfg, "on", "/etc/x", "c"), errors.PATH_OUTSIDE_WORKSPACE)


@pytest.mark.parametrize("rel", ["src/evil.py", "docs/../src/x.py", "docs/sub/../../x.md", "README.md"])
def test_whitelist_escape_matrix(wrepo, rel):
    """GWT-5：白名单基于 final resolved path ∈ root/docs——src/evil、docs/../src/x 等全拒。"""
    cfg, _ = wrepo
    expect_err(lambda: write_file(cfg, "on", rel, "c"), errors.WRITE_PATH_FORBIDDEN)


def test_whitelist_sub_docs_not_top(wrepo):
    """GWT-8：项目根下 sub/docs/x.md 非顶层 docs → WRITE_PATH_FORBIDDEN（白名单锚定 root/docs）。"""
    cfg, root = wrepo
    (root / "sub" / "docs").mkdir(parents=True)
    expect_err(lambda: write_file(cfg, "on", "sub/docs/x.md", "c"), errors.WRITE_PATH_FORBIDDEN)
    assert not (root / "sub" / "docs" / "x.md").exists()


def test_whitelist_casefold_docs(wrepo):
    """GWT-7：DOCS/notes.md 大小写变体（Windows）→ 归一 docs/ 生效（casefold 语义，与读一致）。"""
    cfg, root = wrepo
    r = write_file(cfg, "on", "DOCS/new.md", "case")
    assert r["path"] == "docs/new.md"
    assert (root / "docs" / "new.md").read_text(encoding="utf-8") == "case"


def test_docs_dotdot_stays_in_docs_ok(wrepo):
    """docs/sub/../ok.md → final ∈ root/docs → 放行（白名单按 final 判定，非输入串）。"""
    cfg, root = wrepo
    r = write_file(cfg, "on", "docs/sub/../ok.md", "fine", expected_absent=True)
    assert r["path"] == "docs/ok.md"
    assert (root / "docs" / "ok.md").read_text(encoding="utf-8") == "fine"


# ---------------------------------------------------------------- 安全链 6：敏感
def test_env_name_rejected(wrepo):
    """GWT-9：docs/.env 敏感名义目标 → FILE_NOT_VISIBLE（敏感过滤同样约束写）。"""
    cfg, root = wrepo
    expect_err(lambda: write_file(cfg, "on", "docs/.env", "K=1"), errors.FILE_NOT_VISIBLE)
    assert not (root / "docs" / ".env").exists()


def test_key_pattern_rejected(wrepo):
    """docs/server.key 命中 *.key → FILE_NOT_VISIBLE。"""
    cfg, _ = wrepo
    expect_err(lambda: write_file(cfg, "on", "docs/server.key", "k"), errors.FILE_NOT_VISIBLE)


# ---------------------------------------------------------------- 安全链 7：冲突检查
def test_create_success(wrepo):
    """GWT-1：新建成功，返回 {path, bytes_written, sha256, created:True, write_seq:1}。"""
    cfg, root = wrepo
    r = write_file(cfg, "on", "docs/guide.md", "# guide\n", expected_absent=True)
    assert r["project_id"] == "on"
    assert r["path"] == "docs/guide.md"
    assert r["bytes_written"] == len("# guide\n".encode("utf-8"))
    assert r["sha256"] == sha_of(root / "docs" / "guide.md")
    assert r["created"] is True and r["write_seq"] == 1


def test_create_conflict(wrepo):
    """GWT-2：文件已存在再以 expected_absent=true 写 → FILE_ALREADY_EXISTS。"""
    cfg, _ = wrepo
    expect_err(lambda: write_file(cfg, "on", "docs/notes.md", "c", expected_absent=True),
               errors.FILE_ALREADY_EXISTS)


def test_update_without_sha_rejected(wrepo):
    """更新已有文件未带 content_sha256 → INVALID_ARGUMENT（提示必须带 read_file 的 sha）。"""
    cfg, _ = wrepo
    err = expect_err(lambda: write_file(cfg, "on", "docs/notes.md", "c"), errors.INVALID_ARGUMENT)
    assert "content_sha256" in err.payload["error"]["message"]


def test_update_with_correct_sha(wrepo):
    """GWT-3：read_file 取 sha 后立即更新 → 成功 created:False、write_seq 递增。"""
    cfg, root = wrepo
    write_file(cfg, "on", "docs/seed.md", "seed", expected_absent=True)   # 先写一次，验证递增
    rd = read_file(cfg, "on", "docs/notes.md")
    assert rd["content_sha256"] == sha_of(root / "docs" / "notes.md")   # read_file 输出补 sha
    r = write_file(cfg, "on", "docs/notes.md", "v2\n", content_sha256=rd["content_sha256"])
    assert r["created"] is False and r["write_seq"] == 2
    assert r["sha256"] == sha_of(root / "docs" / "notes.md")
    assert (root / "docs" / "notes.md").read_text(encoding="utf-8") == "v2\n"


def test_revision_conflict_and_recovery(wrepo):
    """GWT-4：read 后文件被改，旧 sha 更新 → REVISION_CONFLICT；重读带新 sha 再写成功。"""
    cfg, root = wrepo
    rd = read_file(cfg, "on", "docs/notes.md")
    (root / "docs" / "notes.md").write_text("tampered\n", encoding="utf-8")
    expect_err(lambda: write_file(cfg, "on", "docs/notes.md", "c", content_sha256=rd["content_sha256"]),
               errors.REVISION_CONFLICT)
    rd2 = read_file(cfg, "on", "docs/notes.md")
    r = write_file(cfg, "on", "docs/notes.md", "v3\n", content_sha256=rd2["content_sha256"])
    assert r["created"] is False and (root / "docs" / "notes.md").read_text(encoding="utf-8") == "v3\n"


# ---------------------------------------------------------------- 安全链 8：长度预算
def test_payload_too_large(wrepo):
    """GWT-10：content 1.5MB → PAYLOAD_TOO_LARGE（UTF-8 字节数口径）。"""
    cfg, _ = wrepo
    big = "a" * (MAX_WRITE_BYTES + 100)   # 1.5MB 量级，按字节超限
    expect_err(lambda: write_file(cfg, "on", "docs/big.md", big, expected_absent=True),
               errors.PAYLOAD_TOO_LARGE)


def test_payload_boundary_ok(wrepo):
    """恰 1MB 字节不超限（多字节字符按 UTF-8 字节计）。"""
    cfg, root = wrepo
    content = "中" * (MAX_WRITE_BYTES // 3)   # 每字 3 字节 ≤ 1MB
    r = write_file(cfg, "on", "docs/cjk.md", content, expected_absent=True)
    assert r["bytes_written"] == len(content.encode("utf-8")) <= MAX_WRITE_BYTES


# ---------------------------------------------------------------- 锁 / 原子写 / write_seq
def test_concurrent_same_sha_exactly_one_wins(wrepo):
    """GWT-12：两协程同起始 sha 并发更新 → 恰一成功，另一 REVISION_CONFLICT（预检在锁内）。"""
    cfg, _ = wrepo
    rd = read_file(cfg, "on", "docs/notes.md")
    sha = rd["content_sha256"]

    async def run():
        return await asyncio.gather(
            asyncio.to_thread(write_file, cfg, "on", "docs/notes.md", "A\n", content_sha256=sha),
            asyncio.to_thread(write_file, cfg, "on", "docs/notes.md", "B\n", content_sha256=sha),
            return_exceptions=True,
        )

    results = asyncio.run(run())
    oks = [r for r in results if not isinstance(r, Exception)]
    conflicts = [r for r in results if isinstance(r, McpError)
                 and r.payload["error"]["code"] == errors.REVISION_CONFLICT]
    assert len(oks) == 1 and len(conflicts) == 1


def test_write_seq_monotonic(wrepo):
    """write_seq per-project 自 1 递增；两项目互不干扰。"""
    cfg, root = wrepo
    s1 = write_file(cfg, "on", "docs/a.md", "1", expected_absent=True)
    s2 = write_file(cfg, "on", "docs/b.md", "2", expected_absent=True)
    s3 = write_file(cfg, "on", "docs/notes.md", "3", content_sha256=sha_of(root / "docs" / "notes.md"))
    assert (s1["write_seq"], s2["write_seq"], s3["write_seq"]) == (1, 2, 3)
    off = tmp_off_write(cfg)
    assert off["write_seq"] == 1     # 'off' 项目开启后从 1 起计


def tmp_off_write(cfg):
    from dataclasses import replace as _replace
    root = cfg.registry["off"].path
    (root / "docs").mkdir(exist_ok=True)
    on2 = _replace(cfg.registry["off"], write_enabled=True)
    cfg2 = Config(projects=(on2,))
    return write_file(cfg2, "off", "docs/x.md", "c", expected_absent=True)


def test_no_tmp_residue(wrepo):
    """GWT-13：正常写成功后无 .tmp 残留，既有文件未被破坏（原子 rename）。"""
    cfg, root = wrepo
    write_file(cfg, on_id := "on", "docs/notes.md", "updated\n",
               content_sha256=sha_of(root / "docs" / "notes.md"))
    assert list((root / "docs").glob("*.tmp*")) == []
    assert (root / "docs" / "notes.md").read_text(encoding="utf-8") == "updated\n"
    assert on_id == "on"


def test_tmp_cleanup_on_simulated_crash(wrepo, monkeypatch):
    """崩溃安全：os.replace 抛错时 tmp 被清理，既有文件保持原内容。"""
    cfg, root = wrepo
    real_replace = __import__("os").replace

    def boom(a, b):
        raise OSError("simulated crash")

    monkeypatch.setattr(writer.os, "replace", boom)
    with pytest.raises(OSError):
        write_file(cfg, "on", "docs/notes.md", "c", content_sha256=sha_of(root / "docs" / "notes.md"))
    monkeypatch.setattr(writer.os, "replace", real_replace)
    assert list((root / "docs").glob("*.tmp*")) == []
    assert (root / "docs" / "notes.md").read_text(encoding="utf-8") == "v1\n"


# ---------------------------------------------------------------- 参数校验
def test_invalid_args(wrepo):
    cfg, _ = wrepo
    expect_err(lambda: write_file(cfg, "on", "", "c"), errors.INVALID_ARGUMENT)
    expect_err(lambda: write_file(cfg, "on", "docs/x.md", None), errors.INVALID_ARGUMENT)
    expect_err(lambda: write_file(cfg, "on", "docs/x.md", "c", expected_absent="yes"), errors.INVALID_ARGUMENT)
    expect_err(lambda: write_file(cfg, "on", "docs/x.md", "c", content_sha256=123), errors.INVALID_ARGUMENT)


# ---------------------------------------------------------------- junction 逃逸（真实 mklink /J，Windows）
@pytest.mark.skipif(sys.platform != "win32", reason="junction 逃逸 case 仅适用 Windows")
def test_junction_escape_in_docs(wrepo, tmp_path):
    """GWT-6：docs/link 为指向 docs 外的 junction，写 docs/link/x.md → 拒绝（真实 junction）。"""
    cfg, root = wrepo
    outside = tmp_path / "outside-j"
    outside.mkdir(exist_ok=True)
    link = root / "docs" / "link"
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0 or not link.exists():
        pytest.fail(f"真实 junction 创建失败（mklink /J）: {r.stderr or r.stdout}")
    try:
        expect_err(lambda: write_file(cfg, "on", "docs/link/x.md", "c", expected_absent=True),
                   errors.PATH_OUTSIDE_WORKSPACE)
        assert not (outside / "x.md").exists()
    finally:
        try:
            link.unlink()
        except OSError:
            pass


@pytest.mark.skipif(sys.platform != "win32", reason="junction 逃逸 case 仅适用 Windows")
def test_junction_escape_at_root(wrepo, tmp_path):
    """根级 junction 指向根外：写 escape/x.md → PATH_OUTSIDE_WORKSPACE。"""
    cfg, root = wrepo
    outside = tmp_path / "outside-root"
    outside.mkdir(exist_ok=True)
    link = root / "escape"
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0 or not link.exists():
        pytest.fail(f"真实 junction 创建失败（mklink /J）: {r.stderr or r.stdout}")
    try:
        expect_err(lambda: write_file(cfg, "on", "escape/docs/x.md", "c", expected_absent=True),
                   errors.PATH_OUTSIDE_WORKSPACE)
    finally:
        try:
            link.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------- config 解析 write_enabled
def test_config_parses_write_enabled(tmp_path):
    """config.yaml projects[].write_enabled 解析：true 生效、缺省 False（兼容无该字段）。"""
    proj = tmp_path / "p"
    proj.mkdir()
    (tmp_path / "cfg.yaml").write_text(
        "projects:\n"
        f"  - id: w\n    name: W\n    path: {proj.as_posix()}\n    write_enabled: true\n"
        f"  - id: r\n    name: R\n    path: {proj.as_posix()}\n",
        encoding="utf-8",
    )
    from config import load_config
    c = load_config(tmp_path / "cfg.yaml")
    assert c.registry["w"].write_enabled is True
    assert c.registry["r"].write_enabled is False
