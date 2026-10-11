"""契约一致性测试：tools.py 注册的工具名/描述/注解与契约一致；各工具 errors ⊆ 契约 errorCodes。"""
from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client, FastMCP

import errors
from conftest import build_git_repos, build_sample_repo
from tools import register_tools


@pytest.fixture(scope="module")
def registered_tools(tmp_path_factory):
    """真实注册链路：build_sample_repo → FastMCP → register_tools → Client.list_tools()。"""
    base = tmp_path_factory.mktemp("contract-specimen")
    sample = build_sample_repo(base)
    git_ns = build_git_repos(base)
    cfg = type(sample.cfg)(projects=sample.cfg.projects + git_ns.cfg.projects)
    mcp = FastMCP(name="contract-check")
    register_tools(mcp, cfg)

    async def list_all():
        async with Client(mcp) as c:
            return await c.list_tools()

    tools = asyncio.run(list_all())
    yield {t.name: t for t in tools}
    from conftest import rmrf
    rmrf(base)


def test_registered_tool_names_match_contract(registered_tools, contract):
    contract_names = {t["name"] for t in contract["tools"]}
    assert set(registered_tools) == contract_names, "注册工具集合与契约 tools 不一致"
    assert len(registered_tools) == 15  # C-010 迁移 v1.6：14 → 15（+write_binary_file）


def test_registered_descriptions_match_contract(registered_tools, contract):
    for t in contract["tools"]:
        got = registered_tools[t["name"]].description
        assert got == t["description"], f"{t['name']} description 与契约不一致"


def test_registered_annotations_match_contract(registered_tools, contract):
    for t in contract["tools"]:
        ann = registered_tools[t["name"]].annotations
        # 契约 v1.2/v1.4/v1.6：write_file、apply_workspace_edit 与 write_binary_file
        # readOnlyHint=false + destructiveHint=true，其余只读
        if t["name"] in ("write_file", "apply_workspace_edit", "write_binary_file"):
            assert getattr(ann, "readOnlyHint", None) is False
            assert getattr(ann, "destructiveHint", None) is True
        else:
            assert getattr(ann, "readOnlyHint", None) is True
        assert getattr(ann, "openWorldHint", False) is False


def test_each_tool_errors_subset_of_contract_error_codes(contract):
    all_codes = {e["code"] for e in contract["errorCodes"]}
    for t in contract["tools"]:
        assert set(t["errors"]) <= all_codes, f"{t['name']}.errors 含契约外错误码"


def test_module_error_codes_match_contract(contract):
    contract_codes = [e["code"] for e in contract["errorCodes"]]
    assert len(contract_codes) == 18
    assert {getattr(errors, c) for c in contract_codes} == set(contract_codes)
    for e in contract["errorCodes"]:
        assert errors._RECOVERABLE[e["code"]] == e["recoverable"]


def test_limits_constants_match_contract(contract):
    from security import (
        LIST_FILES_MAX_ENTRIES,
        MAX_CHARS,
        MAX_FILE_SIZE_BYTES,
        MAX_SEARCH_RESULTS,
    )
    limits = contract["configSchema"]["shape"]["limits"]
    assert MAX_FILE_SIZE_BYTES == limits["max_file_size_bytes"]
    assert MAX_CHARS == limits["max_chars"]
    assert MAX_SEARCH_RESULTS == limits["max_search_results"]
    assert LIST_FILES_MAX_ENTRIES == limits["list_files_max_entries"]


def test_transport_matches_contract(contract):
    from config import ServerConfig
    t = contract["transport"]
    d = ServerConfig()
    assert d.host == t["bind"] and d.port == t["port"] and d.path == t["path"]
    assert t["bind"] == "127.0.0.1"          # 禁 0.0.0.0 / 非 loopback


# ---------------------------------------------------------------- v1.2 收口断言（task16 GWT-1）
def test_contract_read_file_output_contains_content_sha256(contract):
    """契约侧：read_file.output 必含 content_sha256（v1.2 恢复字段），且与 write_file 冲突检测闭环。

    实现侧（read_file 返回该键、write_file 用其做 REVISION_CONFLICT 预检）由
    tests/integration/test_write_e2e.py 真实链路覆盖，此处补契约结构断言。
    """
    tools = {t["name"]: t for t in contract["tools"]}
    assert "content_sha256" in tools["read_file"]["output"]
    assert tools["read_file"]["output"]["content_sha256"].startswith("string")
    # 闭环：write_file.input.content_sha256（更新凭据）+ output.sha256/write_seq（写后凭据）
    wf = tools["write_file"]
    assert "content_sha256" in wf["input"]["properties"]
    assert {"sha256", "write_seq"} <= set(wf["output"])


def test_contract_write_paths_default_docs_glob(contract, tmp_path):
    """契约 configSchema security.write_paths 默认 ["docs/**"]，与 config._DEFAULT_SECURITY 一致。

    （test_config.test_defaults_match_contract_limits_and_security 已做整 dict 相等断言，
    此处按 task16 交付物单独显式化 write_paths 与 projects_note 的 write_enabled 默认值。）
    """
    from config import _DEFAULT_SECURITY
    security = contract["configSchema"]["shape"]["security"]
    assert security["write_paths"] == ["docs/**"]
    assert _DEFAULT_SECURITY["write_paths"] == ["docs/**"]
    note = contract["configSchema"]["shape"]["projects_note"]
    assert "write_enabled" in note and "默认 false" in note
    # 实现侧 write_enabled 解析默认 false（config.py _parse_projects）
    from config import config_from_data
    actual = config_from_data({"projects": [{"id": "default", "name": "Default", "path": str(tmp_path)}]})
    assert actual.projects[0].write_enabled is False
    assert actual.projects[0].write_roots == ("docs",)


# ---------------------------------------------------------------- v1.2 契约 diff 审计（task16 GWT-2）
# 审计基准：contracts/proposal-v1.1-v1.2.md 两笔已批准 delta 的逐字内容。
# 等价表示规则（不计为变更，报告留痕）：
#   R1 键名 "depth≥1" 落盘为 "depth>=1"（JSON 编码规避，同义）；
#   R2 提案 output 字段描述的数组形态 [...] 与落盘字符串形态同义（字段名集合为准）；
#   R3 错误码名 "(新)" 后缀为提案标记，落盘剥离；
#   R4 write_seq 描述尾缀 "供模型检测并发写冲突" 裁剪（字段/类型/语义不变，语义由 notes 并发段保留）；
#   R5 错误码含义中的 sha256→content_sha256、提示语具体化（WRITE_PATH_FORBIDDEN 追加
#      "白名单锚定 root/docs"、WRITE_DISABLED 指明 config.toml 键名）——均为提案安全链/表格同义细化。
_D1_LIST_FILES = {
    "description": "列出项目内某目录的内容。默认返回该目录的直接子目录与文件（ls 风格）；传 depth（1-5）时返回限深目录树。默认过滤 .git/node_modules/dist/build/__pycache__ 与敏感文件。",
    "input": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "path": {"type": "string", "default": "."},
            "depth": {"type": "integer", "minimum": 0, "maximum": 5, "default": 0,
                      "description": "0=直接子项分组；≥1=限深树形"},
        },
        "required": ["project_id"],
    },
    "responseFields": {"project_id": True, "truncated": "全树条目总数超 2000 时 true"},
    "errors": ["PROJECT_NOT_FOUND", "INVALID_ARGUMENT", "PATH_OUTSIDE_WORKSPACE",
               "FILE_NOT_VISIBLE", "SEARCH_BACKEND_ERROR"],
}

_D2_WRITE_FILE = {
    "description": "创建或更新项目 docs/ 目录下的 UTF-8 文本文件（文档维护专用）。新建文件必须 expected_absent=true；更新已有文件必须传入最近一次 read_file 返回的 content_sha256（防覆盖他人/用户新改动）。禁止写 docs/ 之外路径。",
    "annotations": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False},
    "input": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "path": {"type": "string", "description": "docs/ 下相对路径，如 docs/design/xx.md"},
            "content": {"type": "string", "maxBytes": 1048576},
            "expected_absent": {"type": "boolean", "description": "新建时必须为 true"},
            "content_sha256": {"type": "string",
                               "description": "更新已有文件时必填=写入前最近一次 read_file 返回值"},
        },
        "required": ["project_id", "path", "content"],
    },
    "responseFields": {"project_id": True, "truncated": False},
    "errors": ["PROJECT_NOT_FOUND", "INVALID_ARGUMENT", "PATH_OUTSIDE_WORKSPACE", "FILE_NOT_VISIBLE",
               "WRITE_PATH_FORBIDDEN", "REVISION_CONFLICT", "FILE_ALREADY_EXISTS",
               "WRITE_DISABLED", "PAYLOAD_TOO_LARGE"],  # R3：提案 "(新)" 后缀已剥离
}

_D2_NEW_ERROR_CODES = {  # 提案表格五码（提案标题"4 个"为笔误，表格实列 5 码）
    "WRITE_PATH_FORBIDDEN": (False, "目标不在 docs/** 白名单（基于 final resolved path 判定）"),
    "REVISION_CONFLICT": (True, "文件在 read 之后被修改（sha256 不匹配）"),
    "FILE_ALREADY_EXISTS": (True, "新建但文件已存在（expected_absent=true 而文件在）"),
    "WRITE_DISABLED": (False, "项目配置 write_enabled=false（默认 false）"),
    "PAYLOAD_TOO_LARGE": (False, "content 超 1MB"),
}


def _norm_meaning(code: str, meaning: str) -> str:
    """R5 归一：契约措辞回退到提案表格基线再比对。"""
    m = meaning.replace("content_sha256 不匹配", "sha256 不匹配")
    if code == "WRITE_PATH_FORBIDDEN":
        m = m.replace("，白名单锚定 root/docs", "")
    return m


def test_contract_v13_diff_audit_vs_proposal(contract):
    """GWT-2（C-010 迁移到 v1.3/v1.4 口径）：当前 version vs 对应 proposal 的 delta 逐项比对，
    超出批准范围的字段变更 = FAIL。保留 v1.2/v1.3 两笔 delta 校验（内容仍在契约中），新增 v1.4 段。"""
    tools = {t["name"]: t for t in contract["tools"]}

    # ---- 结构不变量：v1.3 后工具 7→11、错误码 16→17；v1.4 后 11→13；v1.5 后 13→14；
    #      v1.6 后 14→15（+write_binary_file）、错误码 17→18（+EXTENSION_NOT_ALLOWED）
    assert len(tools) == 15
    assert len(contract["errorCodes"]) == 18
    codes = {e["code"]: e for e in contract["errorCodes"]}
    assert set(_D2_NEW_ERROR_CODES) <= set(codes)
    # v1.2 写系新码允许出现在 write_file 与 apply_workspace_edit（v1.4 批准语义）
    # 及 write_binary_file（v1.6 批准语义，安全链同 write_file）；
    # 但 WRITE_PATH_FORBIDDEN / FILE_ALREADY_EXISTS 仍仅限 docs 白名单写工具（apply 绕过 docs 白名单）
    _WRITE_CODE_TOOLS = {"write_file", "apply_workspace_edit", "write_binary_file"}
    for t in contract["tools"]:
        if t["name"] not in _WRITE_CODE_TOOLS:
            assert not (set(t["errors"]) & set(_D2_NEW_ERROR_CODES)), \
                f"{t['name']} 携带 v1.2 新错误码（超出批准范围）"
        elif t["name"] == "apply_workspace_edit":
            assert not ({"WRITE_PATH_FORBIDDEN", "FILE_ALREADY_EXISTS"} & set(t["errors"]))

    # ---- Delta 1（v1.0→v1.1）：list_files 双视图，逐字段比对
    #      （v1.5 起 description 更新为批量 paths 语义、input 增 paths、output 增「paths 批量」——见 Delta 5；
    #       此处仅锁 v1.1 基线字段 path/depth/responseFields/errors 不被后续版本悄悄触碰）
    lf = tools["list_files"]
    assert lf["description"] == ("列出项目内某目录或多个目录的内容（ls 风格分组或限深树）。默认过滤 "
                                 ".git/node_modules/dist/build/__pycache__ 与敏感文件。分析新项目时建议："
                                 "先用 depth=2 树建立结构全貌，再定向读文件，避免逐目录多轮往返。")
    assert lf["input"]["properties"]["path"] == _D1_LIST_FILES["input"]["properties"]["path"]
    assert lf["input"]["properties"]["depth"] == _D1_LIST_FILES["input"]["properties"]["depth"]
    assert lf["input"]["required"] == _D1_LIST_FILES["input"]["required"]
    assert lf["responseFields"] == _D1_LIST_FILES["responseFields"]
    assert lf["errors"] == _D1_LIST_FILES["errors"]
    # output：R1/R2 归一后键集合一致（v1.5 增「paths 批量」段）
    assert set(lf["output"]) == {"depth=0 单目录", "depth>=1", "paths 批量"}
    assert set(lf["output"]["depth=0 单目录"]) == {"dirs", "files"}
    assert set(lf["output"]["depth>=1"]) == {"tree"}

    # ---- Delta 2（v1.1→v1.2）：write_file 全段 + 5 新错误码 + 配套改动
    wf = tools["write_file"]
    # v1.7（owner 执行令「项目灵感多目录写」）：description 由 v1.2 基线「docs/ 目录下」改为
    # write_roots 通用表述（契约与 tools.py 同步更新，test_registered_descriptions_match_contract 仍闭环）
    assert wf["description"] == (
        "创建或更新项目声明的可写目录（默认 docs/，可在 config.toml 项目级 write_roots 扩展）下的"
        " UTF-8 文本文件（文档维护专用）。新建文件必须 expected_absent=true；更新已有文件必须传入"
        "最近一次 read_file 返回的 content_sha256（防覆盖他人/用户新改动）。禁止写可写目录之外路径。")
    # annotations：契约 JSON 侧为 dict；注册侧 readOnlyHint=false/destructiveHint=true
    # 由 test_registered_annotations_match_contract 与 e2e 覆盖
    assert wf["annotations"] == {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False}
    assert wf["input"] == _D2_WRITE_FILE["input"]
    assert wf["responseFields"] == _D2_WRITE_FILE["responseFields"]
    assert wf["errors"] == _D2_WRITE_FILE["errors"]
    # output（R4）：键集合一致，各字段类型前缀与提案一致
    assert set(wf["output"]) == {"path", "bytes_written", "sha256", "created", "write_seq"}
    assert wf["output"]["write_seq"].startswith("int(项目级写序号，自 1 递增")
    # 5 个新错误码：recoverable 严格一致；meaning 经 R5 归一后一致
    for code, (recoverable, meaning) in _D2_NEW_ERROR_CODES.items():
        e = codes[code]
        assert e["recoverable"] == recoverable, code
        assert _norm_meaning(code, e["meaning"]) == meaning, code

    # ---- 配套改动：read_file.output.content_sha256（唯一触碰其他工具的点）+ configSchema 扩展
    rd = tools["read_file"]
    assert "content_sha256" in rd["output"]
    assert "v1.2 恢复字段" in rd["notes"]
    shape = contract["configSchema"]["shape"]
    assert shape["security"]["write_paths"] == ["docs/**"]
    assert "write_enabled" in shape["projects_note"]

    # ---- 合法元数据差异（不计变更）：version/status/frozenAt/frozenBy/changePolicy 引用提案
    assert contract["version"] == "1.7.0" and contract["status"] == "FROZEN"  # C-010 迁移 v1.7
    assert "proposal-v1.1-v1.2.md" in contract["changePolicy"]
    assert "proposal-v1.3.md" in contract["changePolicy"]
    assert "v1.4" in contract["changePolicy"]
    assert "v1.5" in contract["changePolicy"]
    assert "v1.6" in contract["changePolicy"]
    assert "v1.7" in contract["changePolicy"]

    # ---- Delta 3（v1.2→v1.3，对照 proposal-v1.3.md）：恰好 4 个 intel 工具 + 恰好 1 个新错误码 + intel 配置段
    # v1.2 基线不变量：7 个既有工具名全保留，新增恰好 4 个 LSP 工具，无第五个新名字
    _V12_TOOL_NAMES = {"list_projects", "project_info", "list_files", "search_code",
                       "read_file", "git_info", "write_file"}
    lsp_tools = {"find_definition", "find_references", "workspace_symbols", "get_diagnostics"}
    ws_tools = {"rename_preview", "apply_workspace_edit"}  # v1.4 新增（恰 2 个）
    batch_tools = {"read_batch"}                           # v1.5 新增（恰 1 个）
    bin_tools = {"write_binary_file"}                      # v1.6 新增（恰 1 个）
    assert _V12_TOOL_NAMES <= set(tools)
    assert set(tools) == _V12_TOOL_NAMES | lsp_tools | ws_tools | batch_tools | bin_tools, \
        "工具集合 ≠ v1.2 七工具 + 4 LSP 工具 + 2 workspace 重构工具 + read_batch + write_binary_file"
    assert len(contract["tools"]) == 15
    # 新错误码：v1.3 增 LSP_UNAVAILABLE、v1.6 增 EXTENSION_NOT_ALLOWED，再无其他
    #（语义字段由 test_module_error_codes_match_contract 逐码比对 recoverable，此处锁集合不变量）
    _V12_ERROR_CODES = {"PROJECT_NOT_FOUND", "INVALID_ARGUMENT", "PATH_OUTSIDE_WORKSPACE",
                        "FILE_NOT_VISIBLE", "FILE_NOT_FOUND", "FILE_TOO_LARGE",
                        "BINARY_FILE_UNSUPPORTED", "SEARCH_BACKEND_ERROR", "GIT_ERROR",
                        "CONFIG_ERROR", "INTERNAL_ERROR", "WRITE_PATH_FORBIDDEN",
                        "REVISION_CONFLICT", "FILE_ALREADY_EXISTS", "WRITE_DISABLED",
                        "PAYLOAD_TOO_LARGE"}
    assert set(codes) == _V12_ERROR_CODES | {"LSP_UNAVAILABLE", "EXTENSION_NOT_ALLOWED"}, \
        "错误码集合 ≠ v1.2 十六码 + LSP_UNAVAILABLE + EXTENSION_NOT_ALLOWED"
    # 7 个既有工具段与 v1.2 语义不变：errors 域均不含 v1.3 新码（既有工具未被触碰）
    for name in _V12_TOOL_NAMES:
        assert "LSP_UNAVAILABLE" not in tools[name]["errors"], \
            f"{name} 携带 v1.3 新错误码（超出批准范围）"

    # 4 个 intel 工具：提案表格逐行比对（readOnly 注解 / errors 域 / input 必填 / output 键）
    _INTEL_ERRORS = ["PROJECT_NOT_FOUND", "INVALID_ARGUMENT", "PATH_OUTSIDE_WORKSPACE",
                     "FILE_NOT_VISIBLE", "LSP_UNAVAILABLE"]
    _LSP_SPEC = {  # 提案表格：input 必填域 / output 键集合
        "find_definition": {"required": ["project_id", "path", "line", "character"],
                            "output": {"locations"}},
        "find_references": {"required": ["project_id", "path", "line", "character"],
                            "output": {"locations"}},
        "workspace_symbols": {"required": ["project_id", "query"],
                              "output": {"symbols"}},
        "get_diagnostics": {"required": ["project_id"],
                            "output": {"diagnostics"}},
    }
    for name, spec in _LSP_SPEC.items():
        t = tools[name]
        assert t["annotations"] == {"readOnlyHint": True, "openWorldHint": False}, name
        assert t["errors"] == _INTEL_ERRORS, f"{name}.errors 与提案/契约不一致"
        assert set(t["input"]["required"]) == set(spec["required"]), name
        assert set(t["input"]["properties"]) >= set(spec["required"]), name
        assert set(t["output"]) == spec["output"], name
        for pos in ("line", "character"):
            if pos in t["input"]["properties"]:
                assert t["input"]["properties"][pos]["minimum"] == 1, f"{name}.{pos} 必须 1-based"
    # 提案表格：include_declaration 默认 false；输出截断上限 100（三工具）
    assert tools["find_references"]["input"]["properties"]["include_declaration"] == \
        {"type": "boolean", "default": False}
    for name in ("find_references", "workspace_symbols", "get_diagnostics"):
        assert "100" in tools[name]["responseFields"]["truncated"], name
    # 降级保证（提案正文）：find_definition description 可发现 LSP_UNAVAILABLE 与 search_code 指引
    assert "LSP_UNAVAILABLE" in tools["find_definition"]["description"]
    assert "search_code" in tools["find_definition"]["description"]
    # 新错误码：recoverable=false + hint 指引安装或 search_code 降级（禁止自动安装语义不变）
    lsp = codes["LSP_UNAVAILABLE"]
    assert lsp["recoverable"] is False
    assert "search_code" in lsp["hint"] and "language server" in lsp["hint"]
    # configSchema.intel 段：servers 覆盖 + 自动探测规则（提案四条命中全部服务器名）
    intel = contract["configSchema"]["shape"]["intel"]
    assert set(intel) == {"servers", "auto_detect"}
    assert "command" in intel["servers"]
    for marker in ("pyright-langserver", "typescript-language-server", "gopls", "rust-analyzer"):
        assert marker in intel["auto_detect"], marker

    # ---- Delta 4（v1.3→v1.4，对照 owner 执行令/proposal-v1.4 语义）：恰好 2 个 workspace 重构工具
    #      + 零新错误码 + apply 绕过 docs 白名单但保留逃逸与敏感防护 + expected_shas 全预检
    _INTEL_ERRORS = ["PROJECT_NOT_FOUND", "INVALID_ARGUMENT", "PATH_OUTSIDE_WORKSPACE",
                     "FILE_NOT_VISIBLE", "LSP_UNAVAILABLE"]
    rp = tools["rename_preview"]
    assert rp["annotations"] == {"readOnlyHint": True, "openWorldHint": False}
    assert rp["errors"] == _INTEL_ERRORS  # 纯只读预览：与 4 个 LSP 工具同错误域
    assert set(rp["input"]["required"]) == {"project_id", "path", "line", "character", "new_name"}
    for pos in ("line", "character"):
        assert rp["input"]["properties"][pos]["minimum"] == 1, f"rename_preview.{pos} 必须 1-based"
    assert rp["input"]["properties"]["new_name"]["minLength"] == 1
    assert set(rp["output"]) == {"files", "file_count", "edit_count"}
    assert rp["responseFields"] == {"project_id": True, "truncated": False}
    assert "INVALID_ARGUMENT" in rp["notes"] and "EMPTY" in rp["notes"]  # new_name 空校验 + EMPTY 态
    assert "apply_workspace_edit" in rp["description"] and "content_sha256" in rp["description"]

    aw = tools["apply_workspace_edit"]
    assert aw["annotations"] == {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False}
    # 绕过 docs 白名单（errors 无 WRITE_PATH_FORBIDDEN）但保留逃逸与敏感防护
    # （PATH_OUTSIDE_WORKSPACE / FILE_NOT_VISIBLE 在列），写前置 WRITE_DISABLED + 全预检 REVISION_CONFLICT
    assert aw["errors"] == ["PROJECT_NOT_FOUND", "INVALID_ARGUMENT", "PATH_OUTSIDE_WORKSPACE",
                            "FILE_NOT_VISIBLE", "WRITE_DISABLED", "REVISION_CONFLICT",
                            "PAYLOAD_TOO_LARGE"]
    assert "WRITE_PATH_FORBIDDEN" not in aw["errors"]
    assert set(aw["input"]["required"]) == {"project_id", "files", "expected_shas"}
    assert set(aw["output"]) == {"applied", "files_changed"}
    assert aw["responseFields"] == {"project_id": True, "truncated": False}
    assert "write_enabled" in aw["description"] and "白名单" in aw["description"]
    # expected_shas 全预检语义：任一不匹配整体拒绝，不部分写；审计含每文件前后 sha
    assert "整体拒绝" in aw["notes"] and "不部分写" in aw["notes"] and "审计" in aw["notes"]
    assert "敏感过滤" in aw["notes"] and "原子写" in aw["notes"]

    # ---- Delta 5（v1.4→v1.5，对照 owner 执行令/proposal-v1.5 语义）：read_batch 全段
    #      + list_files 批量 paths + 3 处既有 description 更新；零新错误码
    #      （BUDGET_EXHAUSTED 为组内软标注，不进契约 errorCodes；v1.6 起错误码 17→18）
    assert len(contract["errorCodes"]) == 18
    assert "BUDGET_EXHAUSTED" not in codes
    rb = tools["read_batch"]
    assert rb["annotations"] == {"readOnlyHint": True, "openWorldHint": False}
    assert rb["errors"] == ["PROJECT_NOT_FOUND", "INVALID_ARGUMENT"]
    assert set(rb["input"]["required"]) == {"project_id", "paths"}
    rb_in = rb["input"]["properties"]
    assert rb_in["paths"]["minItems"] == 1 and rb_in["paths"]["maxItems"] == 20
    assert "glob" in rb_in["paths"]["description"] and "50" in rb_in["paths"]["description"]
    assert rb_in["max_chars_per_file"]["default"] == 50000 and rb_in["max_chars_per_file"]["minimum"] == 1000
    assert rb_in["max_chars_total"]["default"] == 200000 and rb_in["max_chars_total"]["minimum"] == 10000
    assert set(rb["output"]) == {"files", "total_chars", "truncated", "next_hint"}
    assert rb["responseFields"] == {"project_id": True, "truncated": "总额度耗尽时 true"}
    # 组内错误语义：单文件失败不整体失败 + content_sha256 供写流使用
    _rb_files_spec = rb["output"]["files"]
    assert isinstance(_rb_files_spec, list) and _rb_files_spec
    assert "单文件失败不整体失败" in _rb_files_spec[0]["error"]
    assert "content_sha256" in rb["notes"] and "顺序按输入展开序" in rb["notes"]
    # 检索策略内建：3 处既有 description 更新（project_info / read_file / list_files）
    assert "read_batch" in tools["project_info"]["description"]
    assert "read_batch" in tools["read_file"]["description"]
    assert "多个目录" in tools["list_files"]["description"] and "depth=2" in tools["list_files"]["description"]
    # list_files v1.5 批量 paths 段（Delta 1 处已锁基线字段，此处锁新增段）
    assert tools["list_files"]["input"]["properties"]["paths"] == {
        "type": "array", "maxItems": 20, "items": {"type": "string"},
        "description": "批量模式：多个目录一次列出；与 path 二选一（paths 优先）"}
    assert set(tools["list_files"]["output"]["paths 批量"]) == {"groups"}
    _groups_spec = tools["list_files"]["output"]["paths 批量"]["groups"]
    assert isinstance(_groups_spec, list) and _groups_spec
    assert {"path", "dirs", "files", "error"} <= set(_groups_spec[0])

    # ---- Delta 6（v1.5→v1.6，task24 受控二进制写入）：write_binary_file 全段
    #      + 恰好 1 个新错误码 EXTENSION_NOT_ALLOWED；既有工具段零触碰
    wb = tools["write_binary_file"]
    assert wb["annotations"] == {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False}
    assert set(wb["input"]["required"]) == {"project_id", "path", "content_b64"}
    assert wb["input"]["properties"]["expected_absent"] == {
        "type": "boolean", "description": "新建时必须为 true"}
    assert wb["input"]["properties"]["content_sha256"]["description"] == "更新已有文件时必填=写入前最近一次读取返回值"
    assert wb["errors"] == ["PROJECT_NOT_FOUND", "INVALID_ARGUMENT", "PATH_OUTSIDE_WORKSPACE",
                            "FILE_NOT_VISIBLE", "WRITE_PATH_FORBIDDEN", "REVISION_CONFLICT",
                            "FILE_ALREADY_EXISTS", "WRITE_DISABLED", "PAYLOAD_TOO_LARGE"]
    # 契约留痕：errors 数组未列 EXTENSION_NOT_ALLOWED（errorCodes 已定义、notes 已引用白名单语义，
    # 冻结 JSON 的工具段疏漏）；实现按 task 指令与 notes 语义实际抛出该码，subset 校验不受影响
    assert "EXTENSION_NOT_ALLOWED" not in wb["errors"]
    assert "EXTENSION_NOT_ALLOWED" in {c["code"] for c in contract["errorCodes"]}
    assert set(wb["output"]) == {"path", "bytes_written", "sha256", "created", "write_seq"}
    assert wb["responseFields"] == {"project_id": True, "truncated": False}
    # notes：安全链同源 + 扩展名白名单 + 解码后 10MB + read_file 二进制降级指引
    assert "安全链同 write_file" in wb["notes"]
    for ext in (".docx", ".pdf", ".xlsx", ".zip", ".pptx", ".doc", ".xls"):
        assert ext in wb["notes"], ext
    assert "10MB" in wb["notes"] and "大小写不敏感" in wb["notes"]
    assert "BINARY_FILE_UNSUPPORTED" in wb["notes"] and "本工具" in wb["notes"]
    # 新错误码：recoverable=false + hint 指向 write_file 或放弃
    ext_code = codes["EXTENSION_NOT_ALLOWED"]
    assert ext_code["recoverable"] is False
    assert "write_file" in ext_code["hint"] and ".docx" in ext_code["hint"]
    # 既有工具段零触碰：v1.6 新码不出现在任何既有工具的 errors 域（含 write_binary_file 的冻结数组，见上）
    for t in contract["tools"]:
        assert "EXTENSION_NOT_ALLOWED" not in t["errors"], \
            f"{t['name']} 携带 v1.6 新错误码（超出批准范围）"

    # ---- Delta 7（v1.6→v1.7，owner 执行令「项目灵感需写 30-cards/、sources/、wiki/ 等多目录」）：
    #      项目级 write_roots 可写目录白名单。零新工具（仍 15）/零新错误码（仍 18）；
    #      变更面 = write_file/write_binary_file description+notes 措辞 + configSchema.projects.write_roots
    assert "write_roots" in shape["projects"][0] \
        and '["docs"]' in shape["projects"][0]["write_roots"]      # 默认值留痕
    assert "write_roots" in wf["notes"] and "v1.7" in wf["notes"]
    assert "write_roots" in wb["notes"] and "v1.7" in wb["notes"]
    assert "write_roots" in tools["write_binary_file"]["description"]
