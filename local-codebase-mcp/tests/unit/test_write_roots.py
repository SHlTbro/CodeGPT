"""task25 write_roots 单元测试（契约 v1.7）：项目级可写目录白名单。

覆盖：默认 docs 回归 / 多 root 放行 / 越界拒绝 / 嵌套子路径 / 大小写 casefold /
write_binary_file 同源约束 / writer 参数语义 / config.yaml 解析与校验（CONFIG_ERROR）/
注册层（tools.py 闭包）真实传递 project.write_roots。
"""
from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import pytest
from fastmcp import Client, FastMCP

import errors
import writer
from config import Config, ConfigError, load_config, validate_config
from errors import McpError
from projects import Project
from tools import register_tools
from writer import write_binary_file, write_file

DOCX_SPECIMEN = b"PK\x03\x04\x14\x00\x06\x00" + b"\x00" * 8 + b"write_roots specimen"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def expect_err(fn, code: str) -> McpError:
    with pytest.raises(McpError) as ei:
        fn()
    assert ei.value.payload["error"]["code"] == code, f"期望 {code}，实际 {ei.value.payload['error']['code']}"
    return ei.value


@pytest.fixture()
def rrepo(tmp_path) -> tuple[Config, Path]:
    """write_enabled=true 项目（不预建目标目录，write 自动 mkdir）；write_seq 进程态测试间重置。"""
    root = tmp_path / "repo"
    root.mkdir()
    writer._LOCKS.clear()
    writer._SEQS.clear()
    return Config(projects=(Project(id="on", name="On", path=root, write_enabled=True),)), root


# ---------------------------------------------------------------- 默认行为回归（v1.2 口径）
def test_default_field_is_docs_tuple():
    """缺省 write_roots = ("docs",) 单元素 tuple（frozen dataclass 默认）。"""
    assert Project(id="d", name="D", path=Path(".")).write_roots == ("docs",)


def test_default_docs_only_regression(rrepo):
    """默认（不传 write_roots 参数）→ 仅 docs/** 可写；src/ 与根下文件全拒（v1.2 行为不变）。"""
    cfg, root = rrepo
    r = write_file(cfg, "on", "docs/a.md", "ok", expected_absent=True)
    assert r["created"] is True and (root / "docs" / "a.md").read_text(encoding="utf-8") == "ok"
    expect_err(lambda: write_file(cfg, "on", "src/x.py", "c"), errors.WRITE_PATH_FORBIDDEN)
    expect_err(lambda: write_file(cfg, "on", "README.md", "c"), errors.WRITE_PATH_FORBIDDEN)


# ---------------------------------------------------------------- write_file 多 root 行为
def test_multi_roots_all_writable(rrepo):
    """write_roots=("docs","30-cards","wiki","sources") → 四目录均可写（owner「项目灵感」口径）。"""
    cfg, root = rrepo
    roots = ("docs", "30-cards", "wiki", "sources")
    for rel in ("docs/a.md", "30-cards/b.md", "wiki/c.md", "sources/d.md"):
        r = write_file(cfg, "on", rel, "c", expected_absent=True, write_roots=roots)
        assert r["path"] == rel and r["created"] is True
        assert (root / rel).read_text(encoding="utf-8") == "c"


def test_declared_root_but_src_forbidden(rrepo):
    """声明了 30-cards 仍写 src/ → WRITE_PATH_FORBIDDEN，且磁盘无残留。"""
    cfg, root = rrepo
    expect_err(lambda: write_file(cfg, "on", "src/x.py", "c", write_roots=("30-cards",)),
               errors.WRITE_PATH_FORBIDDEN)
    assert not (root / "src").exists()


def test_nested_subpath_under_root(rrepo):
    """root 为 30-cards → 30-cards/prj/PRJ-001.md 嵌套子路径放行（前缀逐段匹配）。"""
    cfg, root = rrepo
    r = write_file(cfg, "on", "30-cards/prj/PRJ-001.md", "card", expected_absent=True,
                   write_roots=("30-cards",))
    assert r["path"] == "30-cards/prj/PRJ-001.md"
    assert (root / "30-cards" / "prj" / "PRJ-001.md").read_text(encoding="utf-8") == "card"


def test_casefold_root_match(rrepo):
    """write_roots 声明 "Wiki"，写 wiki/index.md → casefold 命中放行。"""
    cfg, root = rrepo
    r = write_file(cfg, "on", "wiki/index.md", "idx", expected_absent=True, write_roots=("Wiki",))
    assert r["created"] is True
    assert (root / "wiki" / "index.md").read_text(encoding="utf-8") == "idx"


def test_hierarchical_roots_both_allowed(rrepo):
    """docs 与 docs/sub 层级交叠 → 均放行（匹配取最先命中，无冲突）。"""
    cfg, root = rrepo
    roots = ("docs", "docs/sub")
    for rel in ("docs/a.md", "docs/sub/b.md", "docs/sub/deep/c.md"):
        assert write_file(cfg, "on", rel, "c", expected_absent=True, write_roots=roots)["created"] is True


def test_prefix_is_segment_wise_not_string_prefix(rrepo):
    """root "30-cards" 不放行 "30-cards-extra/x.md"（逐段比对，非字符串前缀）。"""
    cfg, _ = rrepo
    expect_err(lambda: write_file(cfg, "on", "30-cards-extra/x.md", "c", write_roots=("30-cards",)),
               errors.WRITE_PATH_FORBIDDEN)


def test_writer_param_overrides_default(rrepo):
    """writer.write_file 不传 write_roots 参数 → 恒为默认 docs（即使项目对象声明了多 root）。"""
    cfg, root = rrepo
    on = cfg.registry["on"]
    multi = Config(projects=(Project(id="on", name="On", path=on.path, write_enabled=True,
                                     write_roots=("docs", "30-cards")),))
    # 未传参数 → 默认 docs：即使项目对象声明了 30-cards，直接调用 writer 也不放行
    expect_err(lambda: write_file(multi, "on", "30-cards/x.md", "c", expected_absent=True),
               errors.WRITE_PATH_FORBIDDEN)
    expect_err(lambda: write_file(multi, "on", "30-cards/y.md", "c"), errors.WRITE_PATH_FORBIDDEN)
    assert not (root / "30-cards").exists()


# ---------------------------------------------------------------- write_binary_file 同源约束
def test_binary_file_under_write_roots(rrepo):
    """write_binary_file 同受 write_roots 控制：30-cards/ 下 .docx 可写。"""
    cfg, root = rrepo
    r = write_binary_file(cfg, "on", "30-cards/prj/JD雷达.docx", b64(DOCX_SPECIMEN),
                          expected_absent=True, write_roots=("docs", "30-cards"))
    assert r["created"] is True and r["bytes_written"] == len(DOCX_SPECIMEN)
    assert (root / "30-cards" / "prj" / "JD雷达.docx").read_bytes() == DOCX_SPECIMEN


def test_binary_file_outside_roots_forbidden(rrepo):
    """write_binary_file 写 src/a.pdf（root 外）→ WRITE_PATH_FORBIDDEN（先于扩展名/敏感链）。"""
    cfg, _ = rrepo
    expect_err(lambda: write_binary_file(cfg, "on", "src/a.pdf", b64(b"%PDF-1.4"),
                                         write_roots=("30-cards",)), errors.WRITE_PATH_FORBIDDEN)


# ---------------------------------------------------------------- config.yaml 解析与校验
def _write_cfg(tmp_path, extra: str) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    p = tmp_path / "config.yaml"
    p.write_text(
        "projects:\n"
        f"  - id: prj\n    name: P\n    path: {proj.as_posix()}\n{extra}",
        encoding="utf-8")
    return p


def test_config_parses_write_roots_list(tmp_path):
    """config.yaml 实际解析 write_roots 列表（integration 级：YAML → Project 字段）。"""
    c = load_config(_write_cfg(tmp_path, "    write_enabled: true\n"
                                         "    write_roots: [docs, 30-cards, wiki, sources]\n"))
    assert c.registry["prj"].write_roots == ("docs", "30-cards", "wiki", "sources")


def test_config_missing_write_roots_defaults_docs(tmp_path):
    """config.yaml 不写 write_roots → 默认 ("docs",)（向后兼容）。"""
    c = load_config(_write_cfg(tmp_path, ""))
    assert c.registry["prj"].write_roots == ("docs",)


def test_config_empty_write_roots_rejected(tmp_path):
    """write_roots: []（空列表）→ CONFIG_ERROR。"""
    with pytest.raises(ConfigError) as ei:
        load_config(_write_cfg(tmp_path, "    write_roots: []\n"))
    assert "CONFIG_ERROR" in str(ei.value) and "write_roots" in str(ei.value)


@pytest.mark.parametrize("bad", ["..", "docs/../src", "D:/abs", "/abs", "docs/*", "*", "a//b", ""])
def test_config_dirty_roots_rejected(tmp_path, bad):
    """含 .. / 绝对路径 / 盘符 / * / 空段的 root → CONFIG_ERROR。"""
    with pytest.raises(ConfigError) as ei:
        load_config(_write_cfg(tmp_path, f"    write_roots: ['{bad}']\n"))
    assert "CONFIG_ERROR" in str(ei.value) and "write_roots" in str(ei.value)


def test_config_non_string_root_rejected(tmp_path):
    """write_roots 混入非字符串项（数字）→ CONFIG_ERROR。"""
    with pytest.raises(ConfigError):
        load_config(_write_cfg(tmp_path, "    write_roots: [30]\n"))


def test_validate_config_surfaces_write_roots_problems(tmp_path):
    """server.py --check 共用的 validate_config 同样报告 write_roots 问题。"""
    import yaml
    p = _write_cfg(tmp_path, "    write_roots: ['../evil']\n")
    problems = validate_config(yaml.safe_load(p.read_text(encoding="utf-8")), p)
    assert any("write_roots" in msg for msg in problems)


# ---------------------------------------------------------------- 注册层：tools.py 真实传递
def test_registered_tool_uses_project_write_roots(tmp_path):
    """注册链路（FastMCP Client）真实传递 project.write_roots：30-cards 可写、src 拒绝。"""
    proj = tmp_path / "regen"
    proj.mkdir()
    cfg = Config(projects=(Project(id="reg", name="R", path=proj, write_enabled=True,
                                   write_roots=("docs", "30-cards", "wiki", "sources")),))
    mcp = FastMCP(name="write-roots-regen")
    register_tools(mcp, cfg)

    async def run():
        async with Client(mcp) as c:
            ok = await c.call_tool("write_file", {
                "project_id": "reg", "path": "30-cards/prj/PRJ-001.md",
                "content": "card", "expected_absent": True})
            bad = await c.call_tool("write_file", {
                "project_id": "reg", "path": "src/x.py", "content": "c"})
            return ok, bad

    ok, bad = asyncio.run(run())
    ok_data = getattr(ok, "data", None) or ok.structured_content
    assert ok_data["path"] == "30-cards/prj/PRJ-001.md" and ok_data["created"] is True
    assert (proj / "30-cards" / "prj" / "PRJ-001.md").read_text(encoding="utf-8") == "card"
    bad_data = getattr(bad, "data", None) or bad.structured_content
    assert bad_data["error"]["code"] == "WRITE_PATH_FORBIDDEN"
    assert not (proj / "src").exists()
