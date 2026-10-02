"""task12 错误提示打磨：PROJECT_NOT_FOUND 建议列表、FILE_NOT_FOUND hint、INVALID_ARGUMENT 区间、11 码文案非空不重复。"""
from __future__ import annotations

import difflib

import pytest

import errors
from config import Config
from errors import McpError, error_payload
from projects import Project
from tools_services import list_files, project_info, read_file, search_code


# ---------------------------------------------------------------- GWT-1 PROJECT_NOT_FOUND
def test_project_not_found_lists_all_available_ids(sample_repo):
    """GWT-1：message 列出 config 登记的全部可用项目 id。"""
    with pytest.raises(McpError) as ei:
        project_info(sample_repo.cfg, "no-such-project")
    msg = ei.value.payload["error"]["message"]
    for p in sample_repo.cfg.projects:
        assert p.id in msg, f"message 缺少可用 id: {p.id}"


def test_project_not_found_did_you_mean_hit(sample_repo):
    """GWT-1：输入与某 id 相似度 >=0.6 → 附 Did you mean。"""
    target = sample_repo.cfg.projects[0].id          # 'alpha'
    typo = target[:-1]                               # 'alph'（比率 >0.9）
    with pytest.raises(McpError) as ei:
        project_info(sample_repo.cfg, typo)
    err = ei.value.payload["error"]
    assert "Did you mean:" in err["message"] and target in err["message"]
    # 双语义可执行：与契约 modelHandling 一致（换正确 id 重试）
    assert difflib.SequenceMatcher(None, typo, target).ratio() >= 0.6


def test_project_not_found_did_you_mean_miss(sample_repo):
    """GWT-1：无相近 id（比率 <0.6）→ 不附 Did you mean。"""
    with pytest.raises(McpError) as ei:
        project_info(sample_repo.cfg, "zzz12345")
    assert "Did you mean" not in ei.value.payload["error"]["message"]


def test_project_not_found_multi_project_config_full_list():
    """GWT-1（多项目 config）：列表完整含每个 id。"""
    cfg = Config(projects=tuple(
        Project(id=pid, name=pid, path=__import__("pathlib").Path("."))
        for pid in ("alpha", "beta-repo", "gamma")
    ))
    with pytest.raises(McpError) as ei:
        project_info(cfg, "alph")
    msg = ei.value.payload["error"]["message"]
    for pid in ("alpha", "beta-repo", "gamma"):
        assert pid in msg
    assert "Did you mean: alpha?" in msg


# ---------------------------------------------------------------- GWT-2 FILE_NOT_FOUND hint
def test_file_not_found_hint_points_to_parent_listing(sample_repo):
    """GWT-2：hint 指引先 list_files 查父目录确认实际路径。"""
    with pytest.raises(McpError) as ei:
        read_file(sample_repo.cfg, "alpha", "nope.txt")
    err = ei.value.payload["error"]
    assert err["code"] == errors.FILE_NOT_FOUND
    assert "list_files" in err["hint"] and "父目录" in err["hint"]


# ---------------------------------------------------------------- GWT-3 INVALID_ARGUMENT 区间文案
def test_invalid_argument_embeds_allowed_ranges(sample_repo):
    """GWT-3：start_line<1 / end_line<start_line / depth 越界 → message 内嵌允许区间。"""
    cases = [
        (lambda: read_file(sample_repo.cfg, "alpha", "notes.txt", 0), ">= 1"),
        (lambda: read_file(sample_repo.cfg, "alpha", "notes.txt", 3, 2), "end_line >= start_line"),
        (lambda: list_files(sample_repo.cfg, "alpha", ".", 6), "0-5"),
        (lambda: list_files(sample_repo.cfg, "alpha", ".", -1), "0-5"),
    ]
    for fn, frag in cases:
        with pytest.raises(McpError) as ei:
            fn()
        err = ei.value.payload["error"]
        assert err["code"] == errors.INVALID_ARGUMENT
        assert frag in err["message"], f"message 缺区间文案 {frag!r}: {err['message']}"


def test_invalid_argument_depth_out_of_range_measured(sample_repo):
    """GWT-3（实测）：depth=7 越界 → INVALID_ARGUMENT + 'depth must be 0-5' 语义。"""
    with pytest.raises(McpError) as ei:
        list_files(sample_repo.cfg, "alpha", ".", 7)
    msg = ei.value.payload["error"]["message"]
    assert "0-5" in msg and "depth" in msg


# ---------------------------------------------------------------- GWT-4/5 11 码文案非空 + 不重复样板
def _collect_payloads(sample_repo, git_repos):
    """触发各码的真实 payload（无法触发的码用代表构造）。"""
    payloads: dict[str, dict] = {}
    try:
        project_info(sample_repo.cfg, "zzz12345")
    except McpError as e:
        payloads[e.payload["error"]["code"]] = e.payload
    try:
        read_file(sample_repo.cfg, "alpha", "")
    except McpError as e:
        payloads[e.payload["error"]["code"]] = e.payload
    try:
        read_file(sample_repo.cfg, "alpha", "notes.txt", 0)
    except McpError as e:
        payloads[e.payload["error"]["code"]] = e.payload
    try:
        list_files(sample_repo.cfg, "alpha", "../x")
    except McpError as e:
        payloads[e.payload["error"]["code"]] = e.payload
    try:
        read_file(sample_repo.cfg, "alpha", ".env")
    except McpError as e:
        payloads[e.payload["error"]["code"]] = e.payload
    try:
        read_file(sample_repo.cfg, "alpha", "nope.txt")
    except McpError as e:
        payloads[e.payload["error"]["code"]] = e.payload
    try:
        read_file(sample_repo.cfg, "alpha", "big6mb.txt")
    except McpError as e:
        payloads[e.payload["error"]["code"]] = e.payload
    try:
        read_file(sample_repo.cfg, "alpha", "bin.dat")
    except McpError as e:
        payloads[e.payload["error"]["code"]] = e.payload
    import asyncio
    try:
        asyncio.run(search_code(sample_repo.cfg, "alpha", ""))
    except McpError as e:
        payloads[e.payload["error"]["code"]] = e.payload
    try:
        import subprocess
        import tools_services
        real = tools_services.subprocess.run
        tools_services.subprocess.run = lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError("git"))
        try:
            import tools_services as ts
            ts.git_info(git_repos.cfg, "spec-clean")
        finally:
            tools_services.subprocess.run = real
    except McpError as e:
        payloads[e.payload["error"]["code"]] = e.payload
    payloads[errors.SEARCH_BACKEND_ERROR] = error_payload(
        errors.SEARCH_BACKEND_ERROR, "ripgrep 未安装或不可执行", "安装 ripgrep 后重启 server")
    payloads[errors.CONFIG_ERROR] = error_payload(errors.CONFIG_ERROR, "config.yaml 缺失或语法错误，按提示修复", "按提示修复 config.yaml")
    payloads[errors.INTERNAL_ERROR] = error_payload(errors.INTERNAL_ERROR, "内部错误，请查看 server 日志", "查看 server 日志")
    return payloads


def test_all_11_codes_message_and_hint_nonempty(sample_repo, git_repos):
    """GWT-5：11 码 payload 的 message 与 hint 均非空。"""
    payloads = _collect_payloads(sample_repo, git_repos)
    assert set(payloads) == {getattr(errors, c) for c in
                             ("PROJECT_NOT_FOUND", "INVALID_ARGUMENT", "PATH_OUTSIDE_WORKSPACE",
                              "FILE_NOT_VISIBLE", "FILE_NOT_FOUND", "FILE_TOO_LARGE",
                              "BINARY_FILE_UNSUPPORTED", "SEARCH_BACKEND_ERROR", "GIT_ERROR",
                              "CONFIG_ERROR", "INTERNAL_ERROR")}
    for code, p in payloads.items():
        assert p["error"]["message"].strip(), f"{code}.message 为空"
        assert p["error"]["hint"].strip(), f"{code}.hint 为空"


def test_all_11_codes_hints_not_duplicated_boilerplate(sample_repo, git_repos):
    """GWT-5：不同码不得共用同一句 hint 样板（互不重复）。"""
    payloads = _collect_payloads(sample_repo, git_repos)
    hints = [p["error"]["hint"] for p in payloads.values()]
    assert len(hints) == len(set(hints)), f"存在重复 hint: {hints}"
    messages = [p["error"]["message"] for p in payloads.values()]
    assert len(messages) == len(set(messages))


def test_recoverable_mapping_unchanged(contract):
    """GWT-5：既有 11 码 + v1.2 新增 5 码 + v1.3 新增 LSP_UNAVAILABLE 的 recoverable 与契约逐字一致。"""
    contract_codes = [e["code"] for e in contract["errorCodes"]]
    legacy = [c for c in contract_codes if hasattr(errors, c)]
    assert len(legacy) == 18
    for e in contract["errorCodes"]:
        if e["code"] in legacy:
            assert errors._RECOVERABLE[e["code"]] == e["recoverable"]
