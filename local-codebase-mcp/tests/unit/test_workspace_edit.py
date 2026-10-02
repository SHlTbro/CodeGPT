"""task21 单元测试：workspace 重构两段式（rename_preview / apply_workspace_edit）+ intel 双缺陷回归。

rename_preview 走真实 LspSession × fake_lsp（stdio JSON-RPC，--rename-null / --rename-escape 模式）；
apply_workspace_edit 为纯文件操作（无需 LSP），直接服务层驱动。
区间口径（契约）：1-based 双闭——首字符 0-based 偏移 = character-1，末字符 0-based 偏移 = end_character-1。
"""
from __future__ import annotations

import hashlib
import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import errors
import intel
from config import Config
from errors import McpError
from projects import Project
from tools_services import apply_workspace_edit, rename_preview

PKG_DIR = Path(__file__).resolve().parents[2]
FAKE_LSP = PKG_DIR / "tests" / "fixtures" / "fake_lsp.py"
PY = sys.executable

MAIN_OLD = "def main():\n    a = 1\n    b = 2\n    c = 3\n    d = 4\n    e = 5\n    old_one()\n"
HELPER_OLD = "import main\nfrom main import main\nold_x = 1\n"
MAIN_NEW = "def renamed():\n    a = 1\n    b = 2\n    c = 3\n    d = 4\n    e = 5\n    renamedone()\n"
HELPER_NEW = "import main\nfrom main import main\nrenamed = 1\n"


def sha_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_repo(tmp_path: Path, write_enabled: bool = True, fake_argv: list[str] | None = None):
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src/main.py").write_bytes(MAIN_OLD.encode("utf-8"))    # LF 落盘（防 Windows text 模式 CRLF 干扰字节断言）
    (tmp_path / "src/helper.py").write_bytes(HELPER_OLD.encode("utf-8"))
    proj = Project(id="ws", name="WS", path=tmp_path, write_enabled=write_enabled)
    cfg = Config(projects=(proj,), intel_servers={"python": fake_argv} if fake_argv else {})
    return SimpleNamespace(proj=proj, cfg=cfg, root=tmp_path)


@pytest.fixture()
def ws_lsp(tmp_path):
    """带 fake_lsp 会话的标本（teardown 关闭全局 IntelManager，无进程残留）。"""
    ns = make_repo(tmp_path, fake_argv=[PY, str(FAKE_LSP)])
    yield ns
    import tools_services
    if tools_services._INTEL[0] is ns.cfg:
        tools_services._INTEL[1].close_all()
        tools_services._INTEL[0] = tools_services._INTEL[1] = None


@pytest.fixture()
def ws_plain(tmp_path):
    return make_repo(tmp_path)


# ---------------------------------------------------------------- rename_preview

def test_preview_structured_cross_file(ws_lsp):
    """GWT-1：跨 2 文件重命名计划逐处正确（1-based、相对路径、0-based 半开 → 1-based 双闭）。"""
    r = rename_preview(ws_lsp.cfg, "ws", "src/main.py", 1, 5, "renamed")
    assert r["project_id"] == "ws" and r["truncated"] is False
    assert r["file_count"] == 2 and r["edit_count"] == 3
    by_path = {f["path"]: f["edits"] for f in r["files"]}
    assert set(by_path) == {"src/main.py", "src/helper.py"}
    assert by_path["src/main.py"] == [
        {"line": 1, "character": 5, "end_line": 1, "end_character": 8, "new_text": "renamed"},
        {"line": 7, "character": 5, "end_line": 7, "end_character": 8, "new_text": "renamed"},
    ]
    assert by_path["src/helper.py"] == [
        {"line": 3, "character": 1, "end_line": 3, "end_character": 5, "new_text": "renamed"}]


def test_preview_empty_state(tmp_path):
    """rename 返回 null → files 空数组（EMPTY 态正常返回，非报错）。"""
    ns = make_repo(tmp_path, fake_argv=[PY, str(FAKE_LSP), "--rename-null"])
    try:
        r = rename_preview(ns.cfg, "ws", "src/main.py", 1, 5, "renamed")
    finally:
        import tools_services
        if tools_services._INTEL[0] is ns.cfg:
            tools_services._INTEL[1].close_all()
            tools_services._INTEL[0] = tools_services._INTEL[1] = None
    assert r["files"] == [] and r["file_count"] == 0 and r["edit_count"] == 0


@pytest.mark.parametrize("bad", ["", " ", "a b", "a\nb", "重 命名"])
def test_preview_new_name_blank_or_whitespace_invalid(ws_lsp, bad):
    with pytest.raises(McpError) as ei:
        rename_preview(ws_lsp.cfg, "ws", "src/main.py", 1, 5, bad)
    assert ei.value.payload["error"]["code"] == errors.INVALID_ARGUMENT


def test_preview_out_of_project_uri_rejected(tmp_path):
    """changes 含项目根外 URI → PATH_OUTSIDE_WORKSPACE（逐一路径守卫）。"""
    ns = make_repo(tmp_path, fake_argv=[PY, str(FAKE_LSP), "--rename-escape"])
    try:
        with pytest.raises(McpError) as ei:
            rename_preview(ns.cfg, "ws", "src/main.py", 1, 5, "renamed")
    finally:
        import tools_services
        if tools_services._INTEL[0] is ns.cfg:
            tools_services._INTEL[1].close_all()
            tools_services._INTEL[0] = tools_services._INTEL[1] = None
    assert ei.value.payload["error"]["code"] == errors.PATH_OUTSIDE_WORKSPACE


def test_preview_sensitive_target_not_visible(ws_lsp):
    with pytest.raises(McpError) as ei:
        rename_preview(ws_lsp.cfg, "ws", "src/.env", 1, 1, "renamed")
    assert ei.value.payload["error"]["code"] == errors.FILE_NOT_VISIBLE


def test_preview_bad_position_invalid(ws_lsp):
    with pytest.raises(McpError) as ei:
        rename_preview(ws_lsp.cfg, "ws", "src/main.py", 0, 1, "renamed")
    assert ei.value.payload["error"]["code"] == errors.INVALID_ARGUMENT


# ---------------------------------------------------------------- apply_workspace_edit

def _preview_of(ns):
    return rename_preview(ns.cfg, "ws", "src/main.py", 1, 5, "renamed")


def test_apply_roundtrip_from_preview(ws_lsp, tmp_path):
    """GWT-2 前半：preview → read sha → apply 全 sha 匹配 → 全部落盘且 sha_after 正确。

    workspace 语义回归：src/**（docs 白名单之外）可写，路径相对项目根。
    """
    plan = _preview_of(ws_lsp)
    files = [{"path": f["path"], "edits": f["edits"]} for f in plan["files"]]
    assert all(not f["path"].startswith("docs") for f in files)  # 确为白名单外的源码路径
    shas = {"src/main.py": sha_of(ws_lsp.root / "src/main.py"),
            "src/helper.py": sha_of(ws_lsp.root / "src/helper.py")}
    r = apply_workspace_edit(ws_lsp.cfg, "ws", files, shas)
    assert r["project_id"] == "ws" and r["files_changed"] == 2
    assert {a["path"] for a in r["applied"]} == {"src/main.py", "src/helper.py"}
    by_path = {a["path"]: a for a in r["applied"]}
    assert (ws_lsp.root / "src/main.py").read_text(encoding="utf-8") == MAIN_NEW
    assert (ws_lsp.root / "src/helper.py").read_text(encoding="utf-8") == HELPER_NEW
    assert by_path["src/main.py"]["bytes_written"] == len(MAIN_NEW.encode("utf-8"))
    assert by_path["src/main.py"]["sha256"] == sha_of(ws_lsp.root / "src/main.py") == hashlib.sha256(
        MAIN_NEW.encode("utf-8")).hexdigest()
    assert by_path["src/helper.py"]["sha256"] == sha_of(ws_lsp.root / "src/helper.py")


def test_apply_same_file_two_edits_descending_no_misalignment(ws_plain):
    """编辑自后向前拼接：先应用的编辑改变行数，后（更早行）编辑不错位。"""
    target = ws_plain.root / "src/main.py"
    target.write_text("l0\nl1\nl2\nl3\n", encoding="utf-8")
    # 双闭口径：只替换每行首字符（1-based 第 1 列，含）→ end_character=1
    edits = [{"line": 1, "character": 1, "end_line": 1, "end_character": 1, "new_text": "X\nY"},
             {"line": 4, "character": 1, "end_line": 4, "end_character": 1, "new_text": "Z"}]
    r = apply_workspace_edit(ws_plain.cfg, "ws",
                             [{"path": "src/main.py", "edits": edits}],
                             {"src/main.py": sha_of(target)})
    assert r["files_changed"] == 1
    assert target.read_text(encoding="utf-8") == "X\nY0\nl1\nl2\nZ3\n"


def test_apply_multiline_range_collapses(ws_plain):
    """跨行区间 1-based 双闭：第 1 行第 2 列 … 第 2 行第 2 列（含）折叠为一段。"""
    target = ws_plain.root / "src/main.py"
    target.write_text("l0\nl1\nl2\n", encoding="utf-8")
    r = apply_workspace_edit(ws_plain.cfg, "ws",
                             [{"path": "src/main.py",
                               "edits": [{"line": 1, "character": 2, "end_line": 2,
                                          "end_character": 2, "new_text": "M"}]}],
                             {"src/main.py": sha_of(target)})
    assert target.read_text(encoding="utf-8") == "lM\nl2\n"
    assert r["applied"][0]["sha256"] == sha_of(target)


def test_apply_any_sha_mismatch_rejects_all_zero_writes(ws_plain):
    """GWT-2 后半：任一 sha 不匹配 → REVISION_CONFLICT，0 文件落盘。"""
    shas = {"src/main.py": sha_of(ws_plain.root / "src/main.py"),
            "src/helper.py": "0" * 64}  # helper 故意给错
    files = [{"path": "src/main.py", "edits": [{"line": 1, "character": 1, "end_line": 1,
                                                "end_character": 4, "new_text": "X"}]},
             {"path": "src/helper.py", "edits": [{"line": 1, "character": 1, "end_line": 1,
                                                  "end_character": 4, "new_text": "X"}]}]
    with pytest.raises(McpError) as ei:
        apply_workspace_edit(ws_plain.cfg, "ws", files, shas)
    err = ei.value.payload["error"]
    assert err["code"] == errors.REVISION_CONFLICT
    assert "src/helper.py" in err["message"] and "src/main.py" not in err["message"]
    assert (ws_plain.root / "src/main.py").read_text(encoding="utf-8") == MAIN_OLD
    assert (ws_plain.root / "src/helper.py").read_text(encoding="utf-8") == HELPER_OLD


def test_apply_missing_file_treated_as_conflict(ws_plain):
    with pytest.raises(McpError) as ei:
        apply_workspace_edit(ws_plain.cfg, "ws",
                             [{"path": "src/ghost.py", "edits": [{"line": 1, "character": 1,
                                                                  "end_line": 1, "end_character": 1,
                                                                  "new_text": "X"}]}],
                             {"src/ghost.py": "0" * 64})
    assert ei.value.payload["error"]["code"] == errors.REVISION_CONFLICT
    assert "src/ghost.py" in ei.value.payload["error"]["message"]
    assert not (ws_plain.root / "src/ghost.py").exists()


def test_apply_sha_keyset_mismatch_invalid(ws_plain):
    """files 与 expected_shas 键集必须一致：缺失/多余均 INVALID_ARGUMENT，零落盘。"""
    edits = [{"line": 1, "character": 1, "end_line": 1, "end_character": 2, "new_text": "X"}]
    sha = sha_of(ws_plain.root / "src/main.py")
    with pytest.raises(McpError) as ei:
        apply_workspace_edit(ws_plain.cfg, "ws", [{"path": "src/main.py", "edits": edits}], {})
    assert ei.value.payload["error"]["code"] == errors.INVALID_ARGUMENT
    with pytest.raises(McpError) as ei:
        apply_workspace_edit(ws_plain.cfg, "ws", [{"path": "src/main.py", "edits": edits}],
                             {"src/main.py": sha, "src/helper.py": sha})
    assert ei.value.payload["error"]["code"] == errors.INVALID_ARGUMENT
    assert (ws_plain.root / "src/main.py").read_text(encoding="utf-8") == MAIN_OLD


def test_apply_write_disabled(tmp_path):
    ns = make_repo(tmp_path, write_enabled=False)
    with pytest.raises(McpError) as ei:
        apply_workspace_edit(ns.cfg, "ws", [], {})
    assert ei.value.payload["error"]["code"] == errors.WRITE_DISABLED


def test_apply_workspace_semantics_accepts_docs_dotdot_src(ws_plain):
    """GWT-4：`docs/../src/...` 解析后仍在项目根内（workspace 本就含 src）→ 合法放行。"""
    target = ws_plain.root / "src/helper.py"
    r = apply_workspace_edit(ws_plain.cfg, "ws",
                             [{"path": "docs/../src/helper.py",
                               "edits": [{"line": 3, "character": 1, "end_line": 3,
                                          "end_character": 5, "new_text": "new_x"}]}],
                             {"docs/../src/helper.py": sha_of(target)})
    assert r["applied"][0]["path"] == "src/helper.py"
    assert target.read_text(encoding="utf-8") == "import main\nfrom main import main\nnew_x = 1\n"


@pytest.mark.parametrize("bad_path", ["../evil.py", "C:/evil.py", "~/.ssh/known_hosts", "//srv/share/x.py"])
def test_apply_escape_rejected(ws_plain, bad_path):
    """绝对/盘符/UNC/~ 逃逸一律 PATH_OUTSIDE_WORKSPACE（workspace 语义不放松逃逸防护）。"""
    with pytest.raises(McpError) as ei:
        apply_workspace_edit(ws_plain.cfg, "ws",
                             [{"path": bad_path, "edits": [{"line": 1, "character": 1, "end_line": 1,
                                                            "end_character": 1, "new_text": "X"}]}],
                             {bad_path: "0" * 64})
    assert ei.value.payload["error"]["code"] == errors.PATH_OUTSIDE_WORKSPACE
    assert not (ws_plain.root / "evil.py").exists()


def test_apply_sensitive_rejected(ws_plain):
    """目标命中敏感清单 → FILE_NOT_VISIBLE（内容不变）。"""
    env = ws_plain.root / "src" / ".env"
    env.write_text("SECRET=1\n", encoding="utf-8")
    with pytest.raises(McpError) as ei:
        apply_workspace_edit(ws_plain.cfg, "ws",
                             [{"path": "src/.env", "edits": [{"line": 1, "character": 1, "end_line": 1,
                                                              "end_character": 7, "new_text": "X"}]}],
                             {"src/.env": sha_of(env)})
    assert ei.value.payload["error"]["code"] == errors.FILE_NOT_VISIBLE
    assert env.read_text(encoding="utf-8") == "SECRET=1\n"


def test_apply_payload_too_large(ws_plain):
    """单文件应用结果 >1MB → PAYLOAD_TOO_LARGE，零落盘。"""
    target = ws_plain.root / "src/main.py"
    with pytest.raises(McpError) as ei:
        apply_workspace_edit(ws_plain.cfg, "ws",
                             [{"path": "src/main.py",
                               "edits": [{"line": 1, "character": 1, "end_line": 1,
                                          "end_character": 4, "new_text": "x" * (1_048_576 + 1)}]}],
                             {"src/main.py": sha_of(target)})
    assert ei.value.payload["error"]["code"] == errors.PAYLOAD_TOO_LARGE
    assert target.read_text(encoding="utf-8") == MAIN_OLD


@pytest.mark.parametrize("bad_files", [
    [{"path": "src/main.py", "edits": [{"line": 0, "character": 1, "end_line": 1, "end_character": 2, "new_text": "X"}]}],
    [{"path": "src/main.py", "edits": [{"line": 2, "character": 1, "end_line": 1, "end_character": 2, "new_text": "X"}]}],
    [{"path": "src/main.py", "edits": [{"line": 1, "character": 1, "end_line": 1, "end_character": 2, "new_text": 1}]}],
    [{"path": "", "edits": [{"line": 1, "character": 1, "end_line": 1, "end_character": 2, "new_text": "X"}]}],
])
def test_apply_structural_invalid(ws_plain, bad_files):
    with pytest.raises(McpError) as ei:
        apply_workspace_edit(ws_plain.cfg, "ws", bad_files, {"src/main.py": "0" * 64})
    assert ei.value.payload["error"]["code"] == errors.INVALID_ARGUMENT


def test_apply_duplicate_paths_invalid(ws_plain):
    e = {"line": 1, "character": 1, "end_line": 1, "end_character": 2, "new_text": "X"}
    with pytest.raises(McpError) as ei:
        apply_workspace_edit(ws_plain.cfg, "ws",
                             [{"path": "src/main.py", "edits": [e]}, {"path": "src/main.py", "edits": [e]}],
                             {"src/main.py": "0" * 64})
    assert ei.value.payload["error"]["code"] == errors.INVALID_ARGUMENT


# ---------------------------------------------------------------- intel.py 双缺陷回归（task20 上报）

def test_launch_argv_resolves_bare_npx_on_windows():
    """缺陷 1：Windows 下裸 "npx" 直调必败（实为 npx.cmd）→ 启动点解析为 which 绝对路径。"""
    argv = intel.resolve_launch_argv(["npx", "typescript-language-server", "--stdio"])
    if sys.platform == "win32" and __import__("shutil").which("npx"):
        assert argv[0] != "npx" and Path(argv[0]).is_absolute()
        assert argv[1:] == ["typescript-language-server", "--stdio"]
    else:  # 非 Windows / npx 不在 PATH：保持原样
        assert argv == ["npx", "typescript-language-server", "--stdio"]


def test_launch_argv_passthrough(monkeypatch):
    """非 Windows 一律原样；Windows 下解析不到 npx 保持原样；首元素非裸 npx 不动。"""
    monkeypatch.setattr(intel.os, "name", "posix")
    assert intel.resolve_launch_argv(["npx", "--stdio"]) == ["npx", "--stdio"]
    monkeypatch.setattr(intel.os, "name", "nt")
    monkeypatch.setattr(intel.shutil, "which", lambda _: None)
    assert intel.resolve_launch_argv(["npx", "--stdio"]) == ["npx", "--stdio"]
    assert intel.resolve_launch_argv(["pyright-langserver", "--stdio"]) == ["pyright-langserver", "--stdio"]


def test_handshake_declares_publish_diagnostics_capability():
    """缺陷 2：客户端 capabilities 最小声明 textDocument.publishDiagnostics（tsls v6 推送开关）。"""
    src = inspect.getsource(intel.LspSession._handshake)
    assert "publishDiagnostics" in src and '"textDocument"' in src
