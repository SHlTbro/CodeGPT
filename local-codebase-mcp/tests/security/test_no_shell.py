"""task08 静态安全断言：生产源码无 shell 形态（shell=True / os.system / os.popen 等），argv 数组直传。"""
from __future__ import annotations

import re
from pathlib import Path

from conftest import PKG_DIR, PRODUCTION_SOURCES

FORBIDDEN = [
    (r"shell\s*=\s*True", "shell=True（shell 注入面）"),
    (r"\bos\.system\s*\(", "os.system"),
    (r"\bos\.popen\s*\(", "os.popen"),
    (r"\bsubprocess\.getoutput\s*\(", "subprocess.getoutput"),
    (r"\bsubprocess\.getstatusoutput\s*\(", "subprocess.getstatusoutput"),
    (r"\bcommands\.", "commands 模块（已废弃且走 shell）"),
    (r"^\s*import\s+commands\b", "import commands"),
]


def test_production_sources_contain_no_shell_forms():
    violations: list[str] = []
    for name in PRODUCTION_SOURCES:
        src = (PKG_DIR / name).read_text(encoding="utf-8")
        for pattern, label in FORBIDDEN:
            for m in re.finditer(pattern, src, re.MULTILINE):
                line_no = src.count("\n", 0, m.start()) + 1
                violations.append(f"{name}:{line_no} {label}")
    assert violations == [], f"生产源码发现 shell 形态: {violations}"


# task15 GWT-4：网络出站 import 模块黑名单（writer.py 为受控写面，禁止任何出站能力）
NETWORK_FORBIDDEN = [
    (r"^\s*import\s+socket\b", "import socket"),
    (r"^\s*from\s+socket\s+import\b", "from socket import"),
    (r"^\s*import\s+urllib\b", "import urllib"),
    (r"^\s*from\s+urllib\b", "from urllib import"),
    (r"^\s*import\s+http\b", "import http(.client)"),
    (r"^\s*from\s+http\b", "from http(.client) import"),
    (r"^\s*import\s+requests\b", "import requests"),
    (r"^\s*from\s+requests\b", "from requests import"),
    (r"^\s*import\s+httpx\b", "import httpx"),
    (r"^\s*from\s+httpx\b", "from httpx import"),
]


def test_writer_covered_by_shell_scan_and_no_network_outbound():
    """writer.py 纳入 PRODUCTION_SOURCES（shell 形态扫描覆盖）+ 无网络出站 import。"""
    assert "writer.py" in PRODUCTION_SOURCES
    src = (PKG_DIR / "writer.py").read_text(encoding="utf-8")
    violations = [
        label for pattern, label in NETWORK_FORBIDDEN if re.search(pattern, src, re.MULTILINE)
    ]
    assert violations == [], f"writer.py 发现网络出站 import: {violations}"


def test_process_spawns_are_argv_direct():
    """所有进程调用必须是 argv 数组形态：create_subprocess_exec / subprocess.run，且 stdin=DEVNULL 防挂起。"""
    src = (PKG_DIR / "tools_services.py").read_text(encoding="utf-8")
    assert "asyncio.create_subprocess_exec(" in src          # rg 搜索：argv 直传
    assert "subprocess.run(" in src                          # rg --files / git：argv 直传
    assert "subprocess.Popen(" not in src
    assert "stdin=subprocess.DEVNULL" in src or "stdin=asyncio.subprocess.DEVNULL" in src


def test_server_binds_loopback_only():
    """绑定 host 必须来自 config（仅允许 loopback）；0.0.0.0 只允许出现在模块 docstring 的警示文案里。"""
    import ast

    server_src = (PKG_DIR / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(server_src)
    docstring = ast.get_docstring(tree, clean=False) or ""
    bad_consts = [
        ast.dump(n) for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        and "0.0.0.0" in n.value and n.value != docstring
    ]
    assert bad_consts == [], f"server.py 代码中出现 0.0.0.0（docstring 之外）: {bad_consts}"
    assert "host=config.server.host" in server_src          # host 显式取自 config
    config_src = (PKG_DIR / "config.py").read_text(encoding="utf-8")
    assert "_LOOPBACK_HOSTS" in config_src                   # config 校验仅允许 loopback
