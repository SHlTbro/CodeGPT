"""task22 批量取证单元测试（GWT 1–8）：list_files 批量 paths + read_batch（字面/glob/双层预算）。

复用 conftest 的 Config/Project 构造方式自建标本（含 patch.yml、51 个 glob 命中、预算边界文件）。
只读测试不动标本，module 级复用；teardown 显式清理（GWT-9）。
"""
from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

import errors
from config import Config
from errors import McpError
from projects import Project
from tools_services import list_files, read_batch

from conftest import rmrf


def _w(root, rel, data, encoding="utf-8"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        p.write_bytes(data)
    else:
        p.write_text(data, encoding=encoding)


@pytest.fixture(scope="module")
def batch_repo(tmp_path_factory):
    """批量取证标本：patch.yml 三处（含排除/敏感各一）、51 个 .dat、预算边界文件、二进制/超大文件。"""
    base = tmp_path_factory.mktemp("batch-specimen")
    root = base / "batch-proj"
    _w(root, "README.md", "# batch specimen\n")
    _w(root, "package.json", '{"name": "batch"}\n')
    _w(root, "src/main.py", "def main():\n    print('hi')\n")
    _w(root, "src/api.py", "def api():\n    return 1\n")
    _w(root, "docs/guide.md", "# guide\n")
    _w(root, "docs/fix.patch.yml", "key: a\n")
    _w(root, "docs/deep/x.patch.yml", "key: b\n")
    _w(root, "docs/.env", "SECRET=1\n")                    # GWT-6：字面敏感组内拒
    _w(root, "credentials.patch.yml", "leak: 1\n")          # 敏感（credentials*）且命中 glob
    _w(root, ".env", "SECRET_TOKEN=abc\n")
    _w(root, "pkgs/a/package.json", '{"name": "a"}\n')
    _w(root, "pkgs/b/package.json", '{"name": "b"}\n')
    _w(root, "node_modules/nm.patch.yml", "x: 1\n")         # 默认排除目录
    _w(root, "long.txt", "a" * 3000)                        # 单行 3000 字符（per-file 截断）
    _w(root, "big1.txt", "a" * 6000)                        # 总额度边界
    _w(root, "big2.txt", "b" * 6000)
    _w(root, "bin.dat", b"OK\x00BIN")                       # 二进制
    _w(root, "big6mb.txt", ("a" * 1023 + "\n") * (6 * 1024))  # >5MB（FILE_TOO_LARGE）
    for i in range(51):                                     # GWT-4：glob 命中 51
        _w(root, f"g{i:02d}.dat", "x")
    cfg = Config(projects=(Project(id="batch", name="Batch Specimen", path=root),))
    ns = SimpleNamespace(base=base, root=root, cfg=cfg)
    yield ns
    rmrf(base)


def expect_err(fn, code: str) -> McpError:
    with pytest.raises(McpError) as ei:
        fn()
    assert ei.value.payload["error"]["code"] == code, f"期望 {code}，实际 {ei.value.payload['error']['code']}"
    return ei.value


# ---------------------------------------------------------------- GWT-1 批量 list
def test_list_batch_mixed_existing_and_missing(batch_repo):
    """GWT-1：3 组各自 {dirs,files}；不存在的 1 组 error，其余正常，顺序按输入。"""
    r = list_files(batch_repo.cfg, "batch", paths=["src", "no-such-dir", "docs"])
    assert r["project_id"] == "batch" and [g["path"] for g in r["groups"]] == ["src", "no-such-dir", "docs"]
    g0, g1, g2 = r["groups"]
    assert g0["dirs"] == [] and sorted(g0["files"]) == ["api.py", "main.py"]
    assert g1 == {"path": "no-such-dir", "error": {"code": "FILE_NOT_FOUND", "message": g1["error"]["message"]}}
    assert "fix.patch.yml" in g2["files"] and "deep" in g2["dirs"]
    assert "truncated" not in r


def test_list_batch_sensitive_group_error_does_not_affect_rest(batch_repo):
    """批量 list：敏感路径组内 FILE_NOT_VISIBLE，其余组不受影响（单组失败不整体失败）。"""
    r = list_files(batch_repo.cfg, "batch", paths=[".env", "src"])
    g0, g1 = r["groups"]
    assert g0["error"]["code"] == "FILE_NOT_VISIBLE"
    assert g1["files"] == ["api.py", "main.py"] and "error" not in g1


def test_list_batch_depth_applies_per_group(batch_repo):
    """批量 list 含 depth：每组独立调用单目录逻辑（含 depth 树形）。"""
    r = list_files(batch_repo.cfg, "batch", paths=["docs", "pkgs"], depth=2)
    assert all("tree" in g and "error" not in g for g in r["groups"])
    assert r["groups"][0]["tree"]["dirs"][0]["name"] == "deep"


def test_list_batch_paths_priority_over_path(batch_repo):
    """paths 与 path 同时给：paths 优先（path 被忽略）。"""
    r = list_files(batch_repo.cfg, "batch", "src", 0, ["docs", "src"])
    assert [g["path"] for g in r["groups"]] == ["docs", "src"]
    assert "dirs" not in r and "files" not in r  # 未落入单目录形态


def test_list_batch_validation(batch_repo):
    """paths 非数组/空数组/超 20 项/项非字符串/depth 非法 → INVALID_ARGUMENT（信封级）。"""
    expect_err(lambda: list_files(batch_repo.cfg, "batch", paths=[]), errors.INVALID_ARGUMENT)
    expect_err(lambda: list_files(batch_repo.cfg, "batch", paths=["a"] * 21), errors.INVALID_ARGUMENT)
    expect_err(lambda: list_files(batch_repo.cfg, "batch", paths="src"), errors.INVALID_ARGUMENT)
    expect_err(lambda: list_files(batch_repo.cfg, "batch", paths=["src", ""]), errors.INVALID_ARGUMENT)
    expect_err(lambda: list_files(batch_repo.cfg, "batch", paths=["src"], depth=9), errors.INVALID_ARGUMENT)
    expect_err(lambda: list_files(batch_repo.cfg, "no-such", paths=["src"]), errors.PROJECT_NOT_FOUND)


def test_list_batch_single_mode_regression(batch_repo):
    """GWT-7（抽查）：不给 paths 时单目录行为逐字不变（双视图 + project_id + 逃逸/敏感同闸）。"""
    r = list_files(batch_repo.cfg, "batch", "src")
    assert r["project_id"] == "batch" and r["files"] == ["api.py", "main.py"] and r["truncated"] is False
    expect_err(lambda: list_files(batch_repo.cfg, "batch", "../escape"), errors.PATH_OUTSIDE_WORKSPACE)
    expect_err(lambda: list_files(batch_repo.cfg, "batch", ".env"), errors.FILE_NOT_VISIBLE)


# ---------------------------------------------------------------- GWT-2/3 read_batch 展开
def test_read_batch_literal_three_files_order_and_sha(batch_repo):
    """GWT-2：3 文件内容 + 各自 sha256，顺序按输入；sha256 往返（= 文件字节 hash）。"""
    paths = ["README.md", "package.json", "src/main.py"]
    r = read_batch(batch_repo.cfg, "batch", paths)
    assert [f["path"] for f in r["files"]] == paths
    for f, rel in zip(r["files"], paths):
        assert "error" not in f
        raw = (batch_repo.root / rel).read_bytes()
        expected = raw.decode("utf-8").replace("\r\n", "\n")
        assert f["content"] == expected.rstrip("\n")
        assert f["content_sha256"] == hashlib.sha256(raw).hexdigest()
        assert f["truncated"] is False
    assert r["total_chars"] == sum(len(f["content"]) for f in r["files"])
    assert r["truncated"] is False and "next_hint" not in r


def test_read_batch_glob_star_and_deep(batch_repo):
    """GWT-3：**/*.patch.yml 命中全部可见匹配；排除目录（node_modules）与敏感（credentials*）不出现。"""
    r = read_batch(batch_repo.cfg, "batch", ["**/*.patch.yml"])
    got = {f["path"] for f in r["files"]}
    assert got == {"docs/fix.patch.yml", "docs/deep/x.patch.yml"}
    assert all("error" not in f for f in r["files"])
    # 字面 + glob 混合；? 通配单字符（glob 展开序按 rg 输出，断言与顺序无关）
    r2 = read_batch(batch_repo.cfg, "batch", ["src/main.py", "docs/guide.??", "pkgs/*/package.json"])
    assert [f["path"] for f in r2["files"]][:2] == ["src/main.py", "docs/guide.md"]
    assert {f["path"] for f in r2["files"]} == {"src/main.py", "docs/guide.md", "pkgs/a/package.json", "pkgs/b/package.json"}


def test_read_batch_dedupe_literal_and_glob(batch_repo):
    """展开结果去重保序：重复字面与 glob 重叠命中各只保留首个。"""
    r = read_batch(batch_repo.cfg, "batch", ["README.md", "README.md", "*.md"])
    ps = [f["path"] for f in r["files"]]
    assert ps.count("README.md") == 1
    assert set(ps) == {"README.md", "docs/guide.md"}


def test_read_batch_glob_over_50_invalid_argument(batch_repo):
    """GWT-4：glob 命中 51 个 → INVALID_ARGUMENT，message 注明 50 上限与缩小模式建议。"""
    ei = expect_err(lambda: read_batch(batch_repo.cfg, "batch", ["*.dat"]), errors.INVALID_ARGUMENT)
    assert "50" in ei.payload["error"]["message"]
    assert "缩小" in ei.payload["error"]["hint"]


def test_read_batch_empty_result(batch_repo):
    """glob 零命中 → 空结果正常返回（不报错）。"""
    r = read_batch(batch_repo.cfg, "batch", ["*.no-such-ext"])
    assert r["files"] == [] and r["total_chars"] == 0 and r["truncated"] is False


def test_read_batch_validation(batch_repo):
    """paths 空数组/超 20 项/非数组/项非字符串、per-file < 1000、total < 10000 → INVALID_ARGUMENT。"""
    expect_err(lambda: read_batch(batch_repo.cfg, "batch", []), errors.INVALID_ARGUMENT)
    expect_err(lambda: read_batch(batch_repo.cfg, "batch", ["a"] * 21), errors.INVALID_ARGUMENT)
    expect_err(lambda: read_batch(batch_repo.cfg, "batch", "README.md"), errors.INVALID_ARGUMENT)
    expect_err(lambda: read_batch(batch_repo.cfg, "batch", ["README.md", ""]), errors.INVALID_ARGUMENT)
    expect_err(lambda: read_batch(batch_repo.cfg, "batch", ["README.md"], max_chars_per_file=999), errors.INVALID_ARGUMENT)
    expect_err(lambda: read_batch(batch_repo.cfg, "batch", ["README.md"], max_chars_total=9999), errors.INVALID_ARGUMENT)
    expect_err(lambda: read_batch(batch_repo.cfg, "no-such", ["README.md"]), errors.PROJECT_NOT_FOUND)


# ---------------------------------------------------------------- GWT-5/6 组内错误与预算
def test_read_batch_sensitive_literal_group_error(batch_repo):
    """GWT-6：docs/.env 组内 FILE_NOT_VISIBLE，其余文件不受影响。"""
    r = read_batch(batch_repo.cfg, "batch", ["docs/.env", "README.md"])
    f0, f1 = r["files"]
    assert f0["path"] == "docs/.env" and f0["error"]["code"] == "FILE_NOT_VISIBLE" and "content" not in f0
    assert f1["path"] == "README.md" and "error" not in f1 and f1["content"].startswith("# batch")
    assert r["truncated"] is False


def test_read_batch_binary_and_too_large_group_errors(batch_repo):
    """二进制 → BINARY_FILE_UNSUPPORTED；>5MB → FILE_TOO_LARGE；均组内标注不整体失败。"""
    r = read_batch(batch_repo.cfg, "batch", ["bin.dat", "big6mb.txt", "README.md"])
    codes = {f["path"]: f.get("error", {}).get("code") for f in r["files"]}
    assert codes["bin.dat"] == "BINARY_FILE_UNSUPPORTED"
    assert codes["big6mb.txt"] == "FILE_TOO_LARGE"
    assert "error" not in r["files"][2] and r["files"][2]["content"].startswith("# batch")


def test_read_batch_missing_literal_group_error(batch_repo):
    """字面路径不存在 → 组内 FILE_NOT_FOUND，不整体失败。"""
    r = read_batch(batch_repo.cfg, "batch", ["nope.txt", "README.md"])
    assert r["files"][0]["error"]["code"] == "FILE_NOT_FOUND"
    assert "error" not in r["files"][1]


def test_read_batch_per_file_truncation(batch_repo):
    """GWT-5a：max_chars_per_file=1000 → 长文件截断 truncated:true，sha256 仍为全文 hash。"""
    r = read_batch(batch_repo.cfg, "batch", ["long.txt", "README.md"], max_chars_per_file=1000)
    f0, f1 = r["files"]
    assert f0["content"] == "a" * 1000 and f0["truncated"] is True
    assert f0["content_sha256"] == hashlib.sha256((batch_repo.root / "long.txt").read_bytes()).hexdigest()
    assert f1["truncated"] is False  # 每文件独立截断，短文件不受影响


def test_read_batch_total_budget_exhausted(batch_repo):
    """GWT-5b：总额度耗尽 → 剩余文件 BUDGET_EXHAUSTED + 顶层 truncated:true + next_hint。"""
    r = read_batch(batch_repo.cfg, "batch", ["big1.txt", "big2.txt", "README.md"], max_chars_total=10000)
    f0, f1, f2 = r["files"]
    assert f0["content"] == "a" * 6000 and "error" not in f0
    assert f1["error"]["code"] == "BUDGET_EXHAUSTED" and "content" not in f1
    assert f2["error"]["code"] == "BUDGET_EXHAUSTED"
    assert r["total_chars"] == 6000 and r["truncated"] is True
    assert "next_hint" in r and "max_chars_total" in r["next_hint"]


def test_read_batch_error_files_do_not_consume_budget(batch_repo):
    """error 文件不占内容额度：组内错误文件之后的小文件仍正常读出。"""
    r = read_batch(batch_repo.cfg, "batch", ["bin.dat", "big1.txt", "README.md"], max_chars_total=10000)
    assert r["files"][0]["error"]["code"] == "BINARY_FILE_UNSUPPORTED"
    assert r["files"][1]["content"] == "a" * 6000
    assert "error" not in r["files"][2]  # 6000 + len(README) < 10000
    assert r["total_chars"] == 6000 + len(r["files"][2]["content"])
    assert r["truncated"] is False
