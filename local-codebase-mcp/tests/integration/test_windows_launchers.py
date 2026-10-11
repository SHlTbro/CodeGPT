"""Real PowerShell/batch argv and exit-code contracts, isolated from the live owner."""
from __future__ import annotations

import json
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import PKG_DIR

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows launchers")


@pytest.mark.parametrize("entry,config_argument", [
    ("setup.ps1", "relative"), ("setup.ps1", "absolute"),
    ("setup.ps1", "default"), ("deploy.ps1", "relative"),
])
def test_launchers_pass_canonical_config_and_suppress_browser_from_other_cwd(tmp_path, entry, config_argument):
    project = tmp_path / "Code GPT 空格项目"
    project.mkdir()
    for name in ("setup.ps1", "deploy.ps1"):
        (project / name).write_bytes((PKG_DIR / name).read_bytes())
    # Reuse the installed runtime; this fixture only records argv and starts no MCP/tunnel.
    installed = Path(sys.prefix).resolve()
    interpreter = installed / 'Scripts/python.exe'
    if not interpreter.is_file() or interpreter.resolve() != Path(sys.executable).resolve():
        pytest.skip('Current interpreter is not an existing Windows venv; bootstrap installation is forbidden here')
    dependencies = ('pyright', 'nodejs_wheel', 'fastmcp', 'yaml')
    if not all(importlib.util.find_spec(name) is not None for name in dependencies):
        pytest.skip('Installed launcher dependencies missing; fixture must not invoke pip')
    probe = subprocess.run([str(interpreter), '-B', '-c',
        'from fastmcp import FastMCP,Client;from fastmcp.client.transports import StreamableHttpTransport;import yaml,tomllib,pyright,nodejs_wheel'], capture_output=True)
    if probe.returncode != 0:
        pytest.skip('Actual launcher dependency import failed; fixture must not invoke pip')
    os.symlink(installed, project / '.venv', target_is_directory=True)
    assert (project / '.venv/Scripts/python.exe').is_file()
    (project / "setup.py").write_text(
        "import json,sys\nfrom pathlib import Path\n"
        "Path(__file__).with_name('argv.json').write_text(json.dumps(sys.argv[1:]),encoding='utf-8')\n",
        encoding="utf-8")
    relative = Path("自定义 配置") / "settings.toml"
    expected = project / relative if config_argument != "default" else project / "config.toml"
    args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(project / entry),
            "-NoBrowser", "-SetupPort", "12345"]
    if config_argument != "default":
        args += ["-Config", str(relative if config_argument == "relative" else expected)]
    result = subprocess.run(args, cwd=tmp_path, stdin=subprocess.DEVNULL,
                            env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'), capture_output=True, timeout=25)
    assert result.returncode == 0, result.stdout.decode(errors="replace") + result.stderr.decode(errors="replace")
    forwarded = json.loads((project / "argv.json").read_text(encoding="utf-8"))
    assert forwarded == ["--config", str(expected), "--port", "12345"]
    assert not (expected.parent / ".setup").exists()


@pytest.mark.parametrize("exit_code", [0, 7])
def test_deploy_batch_preserves_powershell_exit_after_pause(tmp_path, exit_code):
    project = tmp_path / "batch with spaces"
    project.mkdir()
    (project / "deploy.bat").write_bytes((PKG_DIR / "deploy.bat").read_bytes())
    (project / "deploy.ps1").write_text(f"exit {exit_code}\n", encoding="ascii")
    result = subprocess.run(["cmd", "/c", str(project / "deploy.bat")], input="\n",
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
    assert result.returncode == exit_code, result.stdout + result.stderr
