"""P4 测试套件共享夹具：标本仓库 / git 三态仓库 / 万文件性能标本（task08）。

所有标本用 pytest tmp_path* 构造，fixture teardown 显式自清理（GWT-9 无残留）。
生产代码导入通过 sys.path 注入包目录，不复制、不修改 P1–P3 任何文件。
"""
from __future__ import annotations

import json
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PKG_DIR = Path(__file__).resolve().parents[1]          # local-codebase-mcp/
PROJECT_ROOT = PKG_DIR.parent                          # 111/
sys.path.insert(0, str(PKG_DIR))

CONTRACT_PATH = PROJECT_ROOT / "contracts" / "localcodebase-mcp-mvp.json"

# 生产源码清单（test_no_shell 等扫描用）
PRODUCTION_SOURCES = (
    "config.py", "errors.py", "projects.py", "security.py",
    "tools.py", "tools_services.py", "server.py",
    "writer.py",  # task15：受控写纳入静态扫描面（shell 形态）
    "intel.py",   # task18：LSP 薄适配器纳入静态扫描面（shell 形态）
)


def rmrf(path: Path) -> None:
    """Windows 兼容删除：git 只读对象先解只读再删（GWT-9：测试结束无残留临时目录）。"""

    def _retry(func, p, _exc):
        try:
            os_chmod_readonly_off(p)
            func(p)
        except OSError:
            pass

    try:
        shutil.rmtree(path, onexc=_retry)
    except (TypeError, FileNotFoundError):
        shutil.rmtree(path, ignore_errors=True)


def os_chmod_readonly_off(p: str | Path) -> None:
    try:
        import os
        os.chmod(p, stat.S_IWRITE)
    except OSError:
        pass


@pytest.fixture(scope="session")
def contract() -> dict:
    """已冻结契约 JSON（只读，禁止修改）。"""
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def _write(root: Path, rel: str, data, encoding: str = "utf-8") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        p.write_bytes(data)
    else:
        p.write_text(data, encoding=encoding)


def build_sample_repo(base: Path) -> SimpleNamespace:
    """功能标本仓库：src/.env/*.key/node_modules/dist/build/__pycache__/.git 噪音、
    50k/60k 文本、二进制、100/150 行 needle 命中文件、6MB 超限文件 + 空项目。"""
    alpha, empty = base / "alpha", base / "empty-proj"
    alpha.mkdir(parents=True, exist_ok=True)
    empty.mkdir(parents=True, exist_ok=True)

    _write(alpha, "src/main.py", "def main():\n    print('authentication module')\n")
    _write(alpha, "src/api.py", "def login():\n    return 'authentication'\n")
    _write(alpha, "src/deep/util.py", "# authentication helper\n")
    _write(alpha, "README.md", "# alpha specimen\n")
    _write(alpha, "package.json", '{"name": "alpha"}\n')
    _write(alpha, ".env", "SECRET_TOKEN=abc\n")
    _write(alpha, "apikey.key", "PRIVATE-KEY\n")
    _write(alpha, "credentials.json", '{"user": "u", "password": "p"}\n')
    _write(alpha, "sub/.env", "NESTED=1\n")
    _write(alpha, "sub/.ssh/id_rsa", "NESTED-PRIVATE-KEY\n")     # 嵌套 .ssh（[FIX] 回归标本）
    # 噪音目录（默认排除 + 伪 .git）
    _write(alpha, "node_modules/pkg/index.js", "module.exports = 'authentication'\n")
    _write(alpha, "dist/bundle.js", "authentication bundle\n")
    _write(alpha, "build/out.txt", "authentication build\n")
    _write(alpha, "__pycache__/m.pyc", b"\x00\x01\x02binary")
    _write(alpha, ".git/HEAD", "ref: refs/heads/main\n")
    # 文本预算三态：57k / 恰 50000 / 50001
    _write(alpha, "big.txt", "line authentication\n" * 3000)   # 57000 字符
    _write(alpha, "exact50k.txt", "a" * 50000)
    _write(alpha, "over50k.txt", "b" * 50001)
    # 二进制 / 非 UTF-8
    _write(alpha, "bin.dat", b"OK\x00BINARY")
    _write(alpha, "gbk.txt", "中文注释".encode("gbk"))
    # search 命中数边界：恰 100 / 超 100（150）
    _write(alpha, "hits100.txt", "".join(f"needle {i}\n" for i in range(100)))
    _write(alpha, "hits150.txt", "".join(f"needle {i}\n" for i in range(150)))
    # FILE_TOO_LARGE：> 5MB
    _write(alpha, "big6mb.txt", ("a" * 1023 + "\n") * (6 * 1024))   # ~6MiB
    # search path 限定目标
    _write(alpha, "notes.txt", "authentication here\nplain line\n")

    cfg_path = base / "config.yaml"
    cfg_path.write_text(
        "projects:\n"
        f"  - id: alpha\n    name: Alpha Specimen\n    path: {alpha.as_posix()}\n"
        f"  - id: empty-proj\n    name: Empty Project\n    path: {empty.as_posix()}\n",
        encoding="utf-8",
    )
    from config import load_config
    return SimpleNamespace(base=base, alpha=alpha, empty=empty, cfg_path=cfg_path, cfg=load_config(cfg_path))


@pytest.fixture(scope="module")
def sample_repo(tmp_path_factory) -> SimpleNamespace:
    """模块级功能标本（只读工具不改动标本，模块级复用提速）。teardown 自清理。"""
    base = tmp_path_factory.mktemp("specimen")
    ns = build_sample_repo(base)
    yield ns
    rmrf(base)


def _git(root: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, encoding="utf-8",
                       check=False, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise RuntimeError(f"git {args}: {r.stderr}")
    return r.stdout


def _commit_all(root: Path, msg: str) -> None:
    _git(root, "-c", "user.email=t@t.local", "-c", "user.name=t", "add", "-A")
    _git(root, "-c", "user.email=t@t.local", "-c", "user.name=t", "commit", "-m", msg)


def build_git_repos(base: Path) -> SimpleNamespace:
    """git 三态标本：clean（1 提交全干净）/ dirty（2 修改 + 1 未跟踪 + .env 基线）/ 非 git。"""
    clean, dirty, nongit = base / "spec-clean", base / "spec-dirty", base / "spec-nongit"
    for d in (clean, dirty, nongit):
        d.mkdir(parents=True)
    _git(clean, "init", "-b", "main")
    (clean / "src").mkdir()
    (clean / "a.txt").write_text("hello\n", encoding="utf-8")
    (clean / "src/main.py").write_text("print('authentication ok')\n", encoding="utf-8")
    _commit_all(clean, "init commit")

    _git(dirty, "init", "-b", "main")
    (dirty / "m1.txt").write_text("one\n", encoding="utf-8")
    (dirty / "m2.txt").write_text("two\n", encoding="utf-8")
    (dirty / ".env").write_text("SECRET=1\n", encoding="utf-8")
    _commit_all(dirty, "base")
    (dirty / "m1.txt").write_text("one changed\n", encoding="utf-8")
    (dirty / "m2.txt").write_text("two changed\n", encoding="utf-8")
    (dirty / "untracked.txt").write_text("new file\n", encoding="utf-8")

    (nongit / "plain.txt").write_text("no git here\n", encoding="utf-8")

    from config import Config
    from projects import Project
    cfg = Config(projects=tuple(
        Project(id=d.name, name=d.name, path=d) for d in (clean, dirty, nongit)
    ))
    return SimpleNamespace(base=base, clean=clean, dirty=dirty, nongit=nongit, cfg=cfg)


@pytest.fixture(scope="module")
def git_repos(tmp_path_factory) -> SimpleNamespace:
    base = tmp_path_factory.mktemp("git-specimens")
    ns = build_git_repos(base)
    yield ns
    rmrf(base)


@pytest.fixture(scope="module")
def bulk_repo(tmp_path_factory) -> SimpleNamespace:
    """性能标本：1 万个小文件目录（task08 GWT-6 基准）。teardown 自清理。"""
    from config import Config
    from projects import Project
    base = tmp_path_factory.mktemp("bulk-10k")
    bulk = base / "bulk-10k-files"
    bulk.mkdir()
    for i in range(10000):
        (bulk / f"bulk_{i:05d}.txt").write_text(f"perfmarker typical line {i}\n", encoding="utf-8")
    cfg = Config(projects=(Project(id="bulk", name="Bulk 10k", path=bulk),))
    ns = SimpleNamespace(base=base, bulk=bulk, cfg=cfg)
    yield ns
    rmrf(base)
