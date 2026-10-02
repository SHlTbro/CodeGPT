"""task08 GWT-7/8：6 工具服务层 happy / EMPTY / ERROR 三态 + .env 三工具同闸 + truncated 三态。"""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

import errors
from config import Config
from errors import McpError
from tools_services import (
    git_info,
    list_files,
    list_projects,
    project_info,
    read_file,
    search_code,
)


def expect_err(fn, code: str) -> McpError:
    with pytest.raises(McpError) as ei:
        fn()
    assert ei.value.payload["error"]["code"] == code, f"期望 {code}，实际 {ei.value.payload['error']['code']}"
    return ei.value


def cfg_with_limits(sample_repo, **limits) -> Config:
    """自定义 limits 的 Config（截断边界测试用，无需海量文件）。"""
    return replace(sample_repo.cfg, limits=limits)


# ---------------------------------------------------------------- Tool 1 list_projects
def test_list_projects_happy(sample_repo):
    r = list_projects(sample_repo.cfg)
    assert [p["id"] for p in r["projects"]] == ["alpha", "empty-proj"]       # 声明序
    assert all(set(p) == {"id", "name"} for p in r["projects"])              # 无 project_id（契约 responseFields）


# ---------------------------------------------------------------- Tool 2 project_info
def test_project_info_happy(sample_repo):
    r = project_info(sample_repo.cfg, "alpha")
    assert r["project_id"] == "alpha" and r["name"] == "Alpha Specimen"
    assert "src" in r["root_dirs"] and "node_modules" not in r["root_dirs"]
    assert "README.md" in r["detected_files"] and "package.json" in r["detected_files"]


def test_project_info_empty(sample_repo):
    r = project_info(sample_repo.cfg, "empty-proj")     # EMPTY 态正常返回不报错
    assert r["files_count"] == 0 and r["root_dirs"] == [] and r["detected_files"] == []


def test_project_info_errors(sample_repo):
    expect_err(lambda: project_info(sample_repo.cfg, "no-such"), errors.PROJECT_NOT_FOUND)
    expect_err(lambda: project_info(sample_repo.cfg, ""), errors.INVALID_ARGUMENT)


# ---------------------------------------------------------------- Tool 3 list_files（双视图）
def test_list_files_depth0_direct_children(sample_repo):
    """GWT1：depth=0 只返回直接子项，无递归。"""
    r = list_files(sample_repo.cfg, "alpha", "src")
    assert set(r["dirs"]) == {"deep"} and set(r["files"]) == {"main.py", "api.py"}
    assert r["project_id"] == "alpha" and r["truncated"] is False
    r_root = list_files(sample_repo.cfg, "alpha", ".")
    assert "src" in r_root["dirs"] and r_root["dirs"] == ["src"]     # sub 全滤 → 不出现（GWT5）
    assert {"README.md", "package.json", "notes.txt"} <= set(r_root["files"])
    assert all("/" not in p for p in r_root["files"])       # 只有直接文件名
    assert all(d not in ("node_modules", "dist", "build", "__pycache__", ".git") for d in r_root["dirs"])
    assert r_root["dirs"] == sorted(r_root["dirs"])


def test_list_files_depth0_visibility(sample_repo):
    """GWT4/5：排除目录与敏感文件（含嵌套）不出现在 depth=0 视图。"""
    r_all = list_files(sample_repo.cfg, "alpha", "")
    assert ".env" not in r_all["files"] and "apikey.key" not in r_all["files"]
    assert not any(d in ("node_modules", "dist", "build", "__pycache__", ".git") for d in r_all["dirs"])
    r_sub = list_files(sample_repo.cfg, "alpha", "sub")
    assert r_sub["files"] == [] and r_sub["dirs"] == []     # sub 下仅敏感文件 → 全滤空（GWT7 变体）


def test_list_files_depth2_tree(sample_repo):
    """GWT2：depth=2 树——src 的 dirs 含 core 等价物（deep），files 含 main.py/api.py。"""
    r = list_files(sample_repo.cfg, "alpha", "src", 2)
    t = r["tree"]
    assert t["name"] == "src" and set(t["files"]) == {"main.py", "api.py"}
    assert [d["name"] for d in t["dirs"]] == ["deep"]
    assert t["dirs"][0]["files"] == ["util.py"] and t["dirs"][0]["dirs"] == []
    r_root = list_files(sample_repo.cfg, "alpha", ".", 2)
    root_t = r_root["tree"]
    assert root_t["name"] == "." and all("/" not in f for f in root_t["files"])
    assert "README.md" in root_t["files"]
    src_node = next(d for d in root_t["dirs"] if d["name"] == "src")
    assert set(src_node["files"]) == {"main.py", "api.py"}


def test_list_files_depth_tree_excludes(sample_repo):
    """GWT4/5：排除目录不进树，嵌套敏感 .env 不出现在树形视图。"""
    r = list_files(sample_repo.cfg, "alpha", ".", 3)
    t = r["tree"]
    names = {d["name"] for d in t["dirs"]}
    assert not names & {"node_modules", "dist", "build", "__pycache__", ".git"}

    def walk(node):
        yield node["name"]
        assert ".env" not in node["files"] and "id_rsa" not in node["files"]
        for d in node["dirs"]:
            yield from walk(d)
    all_names = list(walk(t))
    assert not any(n in ("node_modules", "dist", "build", "__pycache__", ".git") for n in all_names)


def test_list_files_invalid_depth(sample_repo):
    """GWT3：depth 越界 → INVALID_ARGUMENT，message 注明 0-5。"""
    for bad in (6, -1):
        err = expect_err(lambda b=bad: list_files(sample_repo.cfg, "alpha", ".", b), errors.INVALID_ARGUMENT)
        assert "0-5" in err.payload["error"]["message"]


def test_list_files_empty(sample_repo):
    """GWT7：空目录两视图均空数组正常返回。"""
    r = list_files(sample_repo.cfg, "empty-proj", ".")
    assert r["dirs"] == [] and r["files"] == [] and r["truncated"] is False
    r_tree = list_files(sample_repo.cfg, "empty-proj", ".", 2)
    assert r_tree["tree"] == {"name": ".", "dirs": [], "files": []}


def test_list_files_truncation_boundary(sample_repo):
    """GWT6（边界）：全树条目数 > limit → 截断 + truncated + next_hint；恰等 → 不截断。"""
    # src 子树 depth=0 恰 3 项（dirs 1 + files 2）
    c3 = cfg_with_limits(sample_repo, list_files_max_entries=3)
    r = list_files(c3, "alpha", "src")
    assert r["truncated"] is False and "next_hint" not in r
    c2 = cfg_with_limits(sample_repo, list_files_max_entries=2)
    r = list_files(c2, "alpha", "src")
    assert r["truncated"] is True and r["next_hint"] == "缩小 path 或减小 depth"
    assert len(r["dirs"]) + len(r["files"]) == 2
    # 树形视图同口径：depth=1 下 src 全树 3 项 > 2 → 截断
    r_tree = list_files(c2, "alpha", "src", 1)
    assert r_tree["truncated"] is True and r_tree["next_hint"] == "缩小 path 或减小 depth"


def test_list_files_errors(sample_repo):
    """GWT8（path 逃逸）：PathGuard 不因新视图回退。"""
    expect_err(lambda: list_files(sample_repo.cfg, "no-such", "."), errors.PROJECT_NOT_FOUND)
    expect_err(lambda: list_files(sample_repo.cfg, "alpha", "../escape.txt"), errors.PATH_OUTSIDE_WORKSPACE)
    expect_err(lambda: list_files(sample_repo.cfg, "alpha", "C:/Windows"), errors.PATH_OUTSIDE_WORKSPACE)
    expect_err(lambda: list_files(sample_repo.cfg, "alpha", ".env"), errors.FILE_NOT_VISIBLE)
    expect_err(lambda: list_files(sample_repo.cfg, "alpha", 123), errors.INVALID_ARGUMENT)
    expect_err(lambda: list_files(sample_repo.cfg, "alpha", ".."), errors.PATH_OUTSIDE_WORKSPACE)


# ---------------------------------------------------------------- Tool 5 read_file
def test_read_file_happy(sample_repo):
    r = read_file(sample_repo.cfg, "alpha", "src/main.py", 1, 10)
    assert r["project_id"] == "alpha" and r["path"] == "src/main.py"
    assert r["content"] == "def main():\n    print('authentication module')"
    assert r["start_line"] == 1 and r["end_line"] == 2 and r["encoding"] == "utf-8"
    assert r["truncated"] is False


def test_read_file_truncated_three_states(sample_repo):
    """truncated 三态：恰 50000 → False；超出 → True（按实际末行返回）；不足 → False。"""
    r_exact = read_file(sample_repo.cfg, "alpha", "exact50k.txt")
    assert len(r_exact["content"]) == 50000 and r_exact["truncated"] is False and r_exact["end_line"] == 1
    r_over = read_file(sample_repo.cfg, "alpha", "over50k.txt")
    assert len(r_over["content"]) == 50000 and r_over["truncated"] is True
    r_small = read_file(sample_repo.cfg, "alpha", "notes.txt")
    assert len(r_small["content"]) < 50000 and r_small["truncated"] is False
    # 越界行号请求按实际末行返回，不报错
    r_beyond = read_file(sample_repo.cfg, "alpha", "notes.txt", 1, 99999)
    assert r_beyond["end_line"] == 2


def test_read_file_errors(sample_repo):
    expect_err(lambda: read_file(sample_repo.cfg, "no-such", "notes.txt"), errors.PROJECT_NOT_FOUND)
    expect_err(lambda: read_file(sample_repo.cfg, "alpha", ""), errors.INVALID_ARGUMENT)
    expect_err(lambda: read_file(sample_repo.cfg, "alpha", "notes.txt", 0), errors.INVALID_ARGUMENT)
    expect_err(lambda: read_file(sample_repo.cfg, "alpha", "notes.txt", 3, 2), errors.INVALID_ARGUMENT)
    expect_err(lambda: read_file(sample_repo.cfg, "alpha", "../../etc/passwd"), errors.PATH_OUTSIDE_WORKSPACE)
    expect_err(lambda: read_file(sample_repo.cfg, "alpha", "~/.ssh/id_rsa"), errors.PATH_OUTSIDE_WORKSPACE)
    expect_err(lambda: read_file(sample_repo.cfg, "alpha", ".env"), errors.FILE_NOT_VISIBLE)
    expect_err(lambda: read_file(sample_repo.cfg, "alpha", "nope.txt"), errors.FILE_NOT_FOUND)
    expect_err(lambda: read_file(sample_repo.cfg, "alpha", "big6mb.txt"), errors.FILE_TOO_LARGE)
    expect_err(lambda: read_file(sample_repo.cfg, "alpha", "bin.dat"), errors.BINARY_FILE_UNSUPPORTED)   # 含 \x00
    expect_err(lambda: read_file(sample_repo.cfg, "alpha", "gbk.txt"), errors.BINARY_FILE_UNSUPPORTED)   # 非 UTF-8


# ---------------------------------------------------------------- Tool 4 search_code
def test_search_code_happy(sample_repo):
    r = asyncio.run(search_code(sample_repo.cfg, "alpha", "authentication"))
    assert r["project_id"] == "alpha" and r["matches"], "应有命中"
    files = {m["file"] for m in r["matches"]}
    allowed = {"big.txt", "notes.txt", "src/main.py", "src/api.py", "src/deep/util.py"}
    assert files <= allowed, f"命中泄漏非可见文件: {files - allowed}"
    assert "big.txt" in files      # big.txt 含 3000 行命中，默认 max=100 全部落在其内
    assert all(isinstance(m["line"], int) and m["line"] >= 1 for m in r["matches"])
    assert all(len(m["text"]) <= 500 for m in r["matches"])
    # big.txt 3000 行命中 > 默认 max_search_results=100 → 截断 + next_hint
    assert r["truncated"] is True and r["next_hint"] == "缩小 query 或限定 path"


def test_search_code_empty(sample_repo):
    r = asyncio.run(search_code(sample_repo.cfg, "alpha", "zzz_no_match_zzz"))
    assert r["matches"] == [] and r["truncated"] is False       # rg 退出码 1 = 空 matches 非错误


def test_search_code_truncation_boundary_default_100(sample_repo):
    r150 = asyncio.run(search_code(sample_repo.cfg, "alpha", "needle", "hits150.txt"))
    assert len(r150["matches"]) == 100 and r150["truncated"] is True
    assert r150["next_hint"] == "缩小 query 或限定 path"
    r100 = asyncio.run(search_code(sample_repo.cfg, "alpha", "needle", "hits100.txt"))
    assert len(r100["matches"]) == 100 and r100["truncated"] is False and "next_hint" not in r100


def test_search_code_path_scoped_and_literal(sample_repo):
    r = asyncio.run(search_code(sample_repo.cfg, "alpha", "authentication", "src"))
    assert r["matches"] and all(m["file"].startswith("src/") for m in r["matches"])
    r_lit = asyncio.run(search_code(sample_repo.cfg, "alpha", "return 'authentication'"))
    assert any(m["file"] == "src/api.py" and m["line"] == 2 for m in r_lit["matches"])   # fixed-strings 字面


def test_search_code_errors(sample_repo, monkeypatch):
    expect_err(lambda: asyncio.run(search_code(sample_repo.cfg, "no-such", "x")), errors.PROJECT_NOT_FOUND)
    expect_err(lambda: asyncio.run(search_code(sample_repo.cfg, "alpha", "")), errors.INVALID_ARGUMENT)
    expect_err(lambda: asyncio.run(search_code(sample_repo.cfg, "alpha", "x", "..")), errors.PATH_OUTSIDE_WORKSPACE)
    monkeypatch.setenv("PATH", "")          # rg 不可达 → SEARCH_BACKEND_ERROR
    expect_err(lambda: asyncio.run(search_code(sample_repo.cfg, "alpha", "authentication")),
               errors.SEARCH_BACKEND_ERROR)


# ---------------------------------------------------------------- .env 三工具同闸（GWT-8）
def test_env_same_gate_across_three_tools(sample_repo):
    """同一 .env：list_files / read_file / search_code 三者一致拒绝/不泄漏。"""
    expect_err(lambda: list_files(sample_repo.cfg, "alpha", ".env"), errors.FILE_NOT_VISIBLE)
    expect_err(lambda: read_file(sample_repo.cfg, "alpha", ".env"), errors.FILE_NOT_VISIBLE)
    r = asyncio.run(search_code(sample_repo.cfg, "alpha", "SECRET_TOKEN"))
    assert all(m["file"] != ".env" for m in r["matches"])
    # 嵌套 .env 同闸
    expect_err(lambda: read_file(sample_repo.cfg, "alpha", "sub/.env"), errors.FILE_NOT_VISIBLE)


# ---------------------------------------------------------------- Tool 6 git_info
def test_git_info_clean(git_repos):
    r = git_info(git_repos.cfg, "spec-clean")
    assert r["project_id"] == "spec-clean" and r["is_git_repo"] is True and r["branch"] == "main"
    assert r["changed_files"] == [] and len(r["recent_commits"]) >= 1


def test_git_info_dirty(git_repos):
    r = git_info(git_repos.cfg, "spec-dirty")
    st = {f["path"]: f["status"] for f in r["changed_files"]}
    assert st.get("m1.txt") == "modified" and st.get("untracked.txt") == "untracked"
    assert {f["file"] for f in r["diff_stat"]} >= {"m1.txt", "m2.txt"}


def test_git_info_nongit_degrades(git_repos):
    """EMPTY 态：非 git 目录 is_git_repo=false 降级正常返回，不报错。"""
    r = git_info(git_repos.cfg, "spec-nongit")
    assert r["project_id"] == "spec-nongit" and r["is_git_repo"] is False and r["branch"] is None
    assert r["changed_files"] == [] and r["recent_commits"] == [] and r["diff_stat"] == []


def test_git_info_errors(git_repos, monkeypatch):
    expect_err(lambda: git_info(git_repos.cfg, "no-such"), errors.PROJECT_NOT_FOUND)
    import subprocess
    import tools_services
    monkeypatch.setattr(tools_services.subprocess, "run",
                        lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError("git")))
    err = expect_err(lambda: git_info(git_repos.cfg, "spec-clean"), errors.GIT_ERROR)
    assert err.payload["error"]["recoverable"] is False
    monkeypatch.undo()


def test_all_project_bound_responses_carry_project_id(sample_repo, git_repos):
    r_info = project_info(sample_repo.cfg, "alpha")
    r_files = list_files(sample_repo.cfg, "alpha", ".")
    r_read = read_file(sample_repo.cfg, "alpha", "notes.txt")
    r_search = asyncio.run(search_code(sample_repo.cfg, "alpha", "authentication"))
    r_git = git_info(git_repos.cfg, "spec-clean")
    assert all(x["project_id"] == "alpha" for x in (r_info, r_files, r_read, r_search))
    assert r_git["project_id"] == "spec-clean"
