"""C-011 清账：intel.py 全链在真实 TypeScript 语言服务器（typescript-language-server）下的联调验证。

环境前置（本任务一次性 npm 全局安装，非代码交付物）：
  npm install -g typescript@5 typescript-language-server
  —— tsls 需要 workspace 内可解析的经典 JS tsserver（TypeScript 7.x 是原生移植版，
  不带 tsserver.js；v6 亦无全局回退），故标本 fixture 用 junction 把全局 typescript
  链入 tmp 标本 node_modules（teardown 先解除链接再删除，绝不波及全局安装）。

task20 实测上报的 2 个 intel.py 缺陷已由 task21 修复，本文件相应简化（原绕行删除）：
  1. Windows npx 探测：intel.resolve_launch_argv 在唯一启动点把裸 "npx" 解析为
     shutil.which("npx") 的 .cmd 绝对路径 → 自动探测 argv 无需 config 覆盖即可直调。
  2. 能力协商：_handshake 声明客户端能力 textDocument.publishDiagnostics → tsls v6
     直接推送诊断（v6 无拉取式 textDocument/diagnostic），task20 的 stdio 透明代理删除。

超时预算：直连用例显式传 intel 层 request(timeout=60)；工具层走默认 30s
（实测真实 tsserver 握手 <1s、单请求 <1s，预算充裕）。tsserver 工程加载异步，
definition 类断言用轮询容忍预热窗口（deadline 后仍不满足则按最后实测值判失败）。
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import intel
from conftest import rmrf

TLS = shutil.which("typescript-language-server")

pytestmark = pytest.mark.skipif(
    TLS is None,
    reason="typescript-language-server 不在 PATH（前置：npm install -g typescript@5 typescript-language-server）")

# 契约期望的 define/decl 实测位置（1-based）：util.ts 第 1 行第 17 列 = greet 标识符起点
UTIL_GREET_DEF = {"path": "src/util.ts", "line": 1, "character": 17}


def _global_ts_lib() -> Path | None:
    """定位全局经典 tsserver.js（tsc 所在 npm 目录的 node_modules/typescript/lib）。"""
    tsc = shutil.which("tsc")
    if not tsc:
        return None
    cand = Path(tsc).resolve().parent / "node_modules" / "typescript" / "lib" / "tsserver.js"
    return cand if cand.is_file() else None


def _eventually(fn, predicate, timeout: float = 20.0, interval: float = 0.5):
    """真实服务器异步预热容忍：轮询至 predicate 成立或超时，返回最后实测值。"""
    deadline = time.monotonic() + timeout
    last = fn()
    while not predicate(last) and time.monotonic() < deadline:
        time.sleep(interval)
        last = fn()
    return last


@pytest.fixture(scope="module")
def ts_spec(tmp_path_factory) -> SimpleNamespace:
    """真实 TS 标本（tmp_path 自清理）：package.json + tsconfig + util/main/bad 三文件。

    不做任何 intel.servers 覆盖：自动探测 argv（npx ...）经 task21 缺陷 1 修复直调成功，
    诊断推送经缺陷 2 修复直达（tool 层默认 config 链路验证）。
    """
    base = tmp_path_factory.mktemp("intel-real")
    proj = base / "spec"
    (proj / "src").mkdir(parents=True)
    (proj / "package.json").write_text('{"name": "spec", "version": "1.0.0"}', encoding="utf-8")
    (proj / "tsconfig.json").write_text(
        '{"compilerOptions": {"target": "ES2020", "module": "commonjs", "strict": true}}',
        encoding="utf-8")
    (proj / "src" / "util.ts").write_text(
        'export function greet(name: string): string { return "hi " + name; }', encoding="utf-8")
    (proj / "src" / "main.ts").write_text(
        'import { greet } from "./util";\nconst msg = greet("world");\n', encoding="utf-8")
    (proj / "src" / "bad.ts").write_text(
        'const broken: number = "not-a-number";\n', encoding="utf-8")

    ts_lib = _global_ts_lib()
    if ts_lib is None:
        pytest.skip("全局经典 tsserver.js 不可定位（需 npm install -g typescript@5），无法联调真实 tsls")
    (proj / "node_modules").mkdir()
    os.symlink(str(ts_lib.parent), str(proj / "node_modules" / "typescript"),
               target_is_directory=True)  # Windows 目录 junction（teardown 先解除链接）

    from config import Config
    from projects import Project
    project = Project(id="spec", name="Real TS Spec", path=proj)
    cfg = Config(projects=(project,))  # 自动探测：无 intel.servers 覆盖（task21 缺陷 1 修复回归）
    try:
        yield SimpleNamespace(project=project, cfg=cfg, base=base, proj=proj)
    finally:
        import tools_services
        if tools_services._INTEL[0] is cfg:
            tools_services._INTEL[1].close_all()
            tools_services._INTEL[0] = tools_services._INTEL[1] = None
        linked = proj / "node_modules" / "typescript"
        try:
            os.unlink(linked)  # 只拆 junction 本身，绝不动全局 typescript
        except OSError:
            pass
        rmrf(base)


def test_real_detect_server_auto_npx(ts_spec):
    """自动探测口径：package.json 标记 → 默认 argv ["npx", "typescript-language-server", "--stdio"]（纯探针）；启动点解析裸 npx。"""
    assert intel.detect_server(ts_spec.project, ts_spec.cfg) == \
        ["npx", "typescript-language-server", "--stdio"]
    argv = intel.resolve_launch_argv(intel.detect_server(ts_spec.project, ts_spec.cfg))
    if shutil.which("npx"):
        assert Path(argv[0]).is_absolute() and argv[0] != "npx"  # Windows .cmd 绝对路径
    assert argv[1:] == ["typescript-language-server", "--stdio"]


def test_real_find_definition(ts_spec):
    """真实 tsserver（自动探测 npx 直调）：main.ts 第 2 行 greet 调用点 → 跳转 util.ts 1:17 定义（1-based 契约口径）。"""
    import tools_services
    d = _eventually(
        lambda: tools_services.find_definition(ts_spec.cfg, "spec", "src/main.ts", 2, 13),
        lambda d: any(l["path"] == "src/util.ts" for l in d["locations"]))
    assert d["project_id"] == "spec" and d["truncated"] is False
    assert d["locations"][0] == UTIL_GREET_DEF


def test_real_find_references(ts_spec):
    """真实 tsserver：util.ts greet 声明处 → 含声明 3 处（声明+import+调用）；不含声明仅 2 处。"""
    import tools_services
    with_decl = tools_services.find_references(ts_spec.cfg, "spec", "src/util.ts", 1, 17,
                                               include_declaration=True)
    spots = {(l["path"], l["line"], l["character"]) for l in with_decl["locations"]}
    assert len(with_decl["locations"]) >= 2
    assert ("src/util.ts", 1, 17) in spots
    assert any(p == "src/main.ts" for p, _, _ in spots)
    no_decl = tools_services.find_references(ts_spec.cfg, "spec", "src/util.ts", 1, 17)
    assert len(no_decl["locations"]) >= 1
    assert all(l["path"] != "src/util.ts" for l in no_decl["locations"])  # 声明本身被排除


def test_real_workspace_symbols(ts_spec):
    """真实 tsserver：workspace/symbol 查 "greet" 命中，kind 数值 12 → "Function"，位置 1-based。"""
    import tools_services
    s = tools_services.workspace_symbols(ts_spec.cfg, "spec", "greet")
    sym = next((x for x in s["symbols"] if x["name"] == "greet"), None)
    assert sym is not None
    assert sym["kind"] == "Function"
    assert (sym["path"], sym["line"], sym["character"]) == ("src/util.ts", 1, 1)


def test_real_get_diagnostics(ts_spec):
    """真实 tsls v6 推送诊断（缺陷 2 修复后无需代理）经 intel.py 暂存：bad.ts 类型错误 severity=1 → "error"、line 1-based。"""
    import tools_services
    g = _eventually(
        lambda: tools_services.get_diagnostics(ts_spec.cfg, "spec", "src/bad.ts"),
        lambda g: bool(g["diagnostics"]))
    assert len(g["diagnostics"]) >= 1
    diag = g["diagnostics"][0]
    assert set(diag) == {"path", "severity", "line", "message", "source"}  # 契约结构合法
    assert diag["path"] == "src/bad.ts"
    assert diag["severity"] == "error"          # 真实 severity 数值 1 → "error" 映射正确
    assert diag["line"] == 1                    # 0-based 0 → 1-based 1
    assert diag["message"].strip() and diag["source"]  # 真实消息与来源（typescript）
    clean = tools_services.get_diagnostics(ts_spec.cfg, "spec", "src/main.ts")
    # main.ts 无错误；真实服务器会推 severity=4 的未用变量提示（"'msg' is declared but
    # its value is never read"）→ 恰好再验证一档 severity 映射（4 → "hint"）
    assert all(d["severity"] != "error" for d in clean["diagnostics"])
    hints = [d for d in clean["diagnostics"] if d["severity"] == "hint"]
    assert hints and all(d["path"] == "src/main.ts" and d["line"] >= 1 for d in hints)


def test_real_lsp_session_direct_request(ts_spec):
    """真实 tsls 直连：intel.LspSession 用「自动探测原始 argv」握手/ensure_open/request(timeout=60)/close 全链（缺陷 1 直调回归）。"""
    argv = intel.detect_server(ts_spec.project, ts_spec.cfg)
    sess = intel.LspSession(ts_spec.project, argv)  # 内部经 resolve_launch_argv 解析裸 npx
    try:
        assert sess.healthy and sess.pid
        util = ts_spec.proj / "src" / "util.ts"
        main = ts_spec.proj / "src" / "main.ts"
        sess.ensure_open(util, language_id="typescript")
        sess.ensure_open(main, language_id="typescript")

        def _ask():
            return sess.request("textDocument/definition", {
                "textDocument": {"uri": main.as_uri()},
                "position": intel.to_lsp_position(2, 13)}, timeout=60.0)

        raw = _eventually(_ask, lambda r: bool(r) and "util.ts" in json.dumps(r))
        items = raw if isinstance(raw, list) else [raw]
        assert any(intel.normalize_location(ts_spec.proj, x) == UTIL_GREET_DEF for x in items)
    finally:
        sess.close()
    assert not sess.healthy  # close 后会话不再健康（无残留子进程泄漏）
