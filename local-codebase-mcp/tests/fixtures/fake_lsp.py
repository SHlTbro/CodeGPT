"""task17 测试夹具：Python mock 语言服务器（stdio JSON-RPC，真 Content-Length 帧协议）。

用法: python fake_lsp.py [--no-reply METHOD] [--die-on METHOD] [--refs N]
行为（0-based，符合 LSP 协议）:
  initialize → capabilities；textDocument/didOpen 登记文档；
  textDocument/definition → 仅对已 didOpen 文档返回固定位置 (line 9, char 2)；
  textDocument/references → 3 条固定引用（--refs N 覆盖条数，task18 截断测试）；
  workspace/symbol → 名称含 query 模糊匹配；
  textDocument/diagnostic → 固定 items（拉模式）；textDocument/rename → 跨 2 文件 WorkspaceEdit
  （请求 uri 2 处编辑 + main.py→helper.py 同目录兄弟文件 1 处编辑，newText 取 newName）；
  --rename-null：rename 返回 null（EMPTY 态）；--rename-escape：rename 返回项目根外 URI（守卫测试）；
  shutdown → null，exit → 退出。
  --no-reply METHOD：对该方法永不回包（超时测试）；--die-on METHOD：收到即 os._exit(1)（崩溃测试）。
"""
from __future__ import annotations

import json
import os
import sys

_no_reply = _die_on = None
_refs_count = 3
_rename_null = _rename_escape = False
_args = sys.argv[1:]
_i = 0
while _i < len(_args):
    if _args[_i] == "--no-reply" and _i + 1 < len(_args):
        _no_reply = _args[_i + 1]
        _i += 2
    elif _args[_i] == "--die-on" and _i + 1 < len(_args):
        _die_on = _args[_i + 1]
        _i += 2
    elif _args[_i] == "--refs" and _i + 1 < len(_args):
        _refs_count = int(_args[_i + 1])
        _i += 2
    elif _args[_i] == "--rename-null":
        _rename_null = True
        _i += 1
    elif _args[_i] == "--rename-escape":
        _rename_escape = True
        _i += 1
    else:
        _i += 1

_SYMBOLS = [  # (name, SymbolKind, uri, line, character) 全 0-based
    ("Authenticator", 5, "file:///proj/src/auth.py", 4, 6),
    ("Authenticator.login", 6, "file:///proj/src/auth.py", 9, 4),
    ("logout", 12, "file:///proj/src/auth.py", 20, 0),
    ("parse_config", 12, "file:///proj/src/cfg.py", 2, 4),
]
_REFS = [(4, 6), (12, 2), (30, 10)]
if _refs_count != 3:  # task18 截断测试：--refs N 生成 N 条确定性引用
    _REFS = [((i * 13) % 200, (i * 7) % 40) for i in range(_refs_count)]
_DEF = (9, 2)  # 固定 definition 位置 → 期望换算回 1-based (10, 3)
_DIAGS = [(1, 3, 0, "undefined name 'x'", "fake-lsp"),
          (2, 7, 4, "unused import", "fake-lsp")]

_opened: set[str] = set()
_out = sys.stdout.buffer


def _send(msg: dict) -> None:
    body = json.dumps(msg).encode("utf-8")
    _out.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    _out.flush()


def _range(line: int, char: int) -> dict:
    return {"start": {"line": line, "character": char}, "end": {"line": line, "character": char}}


def _respond(req: dict) -> None:
    method = req.get("method", "")
    if _die_on is not None and method == _die_on:
        os._exit(1)  # 模拟进程崩溃（不做任何清理，管道即断）
    if _no_reply is not None and method == _no_reply:
        return  # 永不回包，驱动客户端超时
    params = req.get("params") or {}
    result = None
    if method == "initialize":
        result = {"capabilities": {"textDocumentSync": 1}}
    elif method == "textDocument/definition":
        uri = params["textDocument"]["uri"]
        if uri in _opened:  # 未 didOpen 的文档不返回位置（LSP 语义）
            result = {"uri": uri, "range": _range(*_DEF)}
    elif method == "textDocument/references":
        uri = params["textDocument"]["uri"]
        result = [{"uri": uri, "range": _range(l, c)} for l, c in _REFS]
    elif method == "workspace/symbol":
        q = (params.get("query") or "").lower()
        result = [{"name": n, "kind": k, "location": {"uri": u, "range": _range(l, c)}}
                  for n, k, u, l, c in _SYMBOLS if q in n.lower()]
    elif method == "textDocument/diagnostic":
        uri = params["textDocument"]["uri"]
        result = {"kind": "full", "items": [
            {"range": _range(l, c), "severity": s, "message": m, "source": src}
            for s, l, c, m, src in _DIAGS]}
    elif method == "textDocument/rename":
        uri = params["textDocument"]["uri"]
        new_name = params.get("newName", "renamed")

        def _edit(l0, c0, l1, c1):
            return {"range": {"start": {"line": l0, "character": c0},
                              "end": {"line": l1, "character": c1}},
                    "newText": new_name}

        if _rename_null:
            result = None
        elif _rename_escape:
            result = {"changes": {"file:///outside-proj/src/other.py": [_edit(0, 0, 0, 4)]}}
        else:
            # 跨 2 文件：请求文件 2 处（同文件多编辑排序用）+ main.py→helper.py 兄弟文件 1 处
            # 编辑 1 覆盖 "def main():" 的 main（0-based 4..8），编辑 2 覆盖 "    old_one()" 的 old_
            sibling = uri.replace("main.py", "helper.py") if uri.endswith("main.py") else uri
            result = {"changes": {uri: [_edit(0, 4, 0, 8), _edit(6, 4, 6, 8)],
                                  sibling: [_edit(2, 0, 2, 5)]}}
    elif method == "shutdown":
        result = None
    if "id" in req:
        _send({"jsonrpc": "2.0", "id": req["id"], "result": result})


def main() -> None:
    f = sys.stdin.buffer
    while True:
        headers: dict[bytes, bytes] = {}
        while True:
            line = f.readline()
            if not line:
                return
            if line in (b"\r\n", b"\n"):
                break
            k, _, v = line.partition(b":")
            headers[k.strip().lower()] = v.strip()
        body = f.read(int(headers[b"content-length"]))
        req = json.loads(body.decode("utf-8"))
        if req.get("method") == "textDocument/didOpen":
            _opened.add(req["params"]["textDocument"]["uri"])
        elif req.get("method") == "exit":
            return
        else:
            _respond(req)


main()
