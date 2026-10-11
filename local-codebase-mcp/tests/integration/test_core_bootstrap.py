"""Opt-in first-install acceptance; all packages/processes live in an owned directory."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import tomllib

import httpx2 as httpx
import pytest

from conftest import PKG_DIR
from runtime import Runtime
from test_setup_e2e import free_port


@pytest.mark.skipif(os.name != "nt", reason="Windows install path limit")
def test_unsupported_long_install_path_has_actionable_error(tmp_path):
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as key:
        enabled, _ = winreg.QueryValueEx(key, "LongPathsEnabled")
    if enabled == 1:
        pytest.skip("This host supports long paths")
    program = tmp_path / ("long-install-path-" * 4)
    program.mkdir()
    shutil.copyfile(PKG_DIR / "setup.ps1", program / "setup.ps1")
    result = subprocess.run([str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"),
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(program / "setup.ps1"), "-NoBrowser"],
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert "short folder" in result.stdout and "No system setting was changed" in result.stdout
    assert not (program / ".venv").exists()


@pytest.mark.skipif(os.name != "nt" or os.environ.get("CODEGPT_BOOTSTRAP_E2E") != "1",
                    reason="Explicit isolated first-install acceptance")
def test_real_first_install_without_codex_and_repeated_launch(tmp_path):
    program = tmp_path / "distribution"
    program.mkdir()
    for name in ("config.py", "errors.py", "projects.py", "runtime.py", "security.py", "intel.py",
                 "tools.py", "tools_services.py", "writer.py", "server.py", "setup.py", "startup.py", "setup.ps1", "requirements.txt"):
        shutil.copyfile(PKG_DIR / name, program / name)
    for name in ("local_agents", "contracts", "web"):
        shutil.copytree(PKG_DIR / name, program / name, ignore=shutil.ignore_patterns("__pycache__"))
    lock = program / "docs/development-baseline/runtime-requirements-lock.txt"
    lock.parent.mkdir(parents=True)
    shutil.copyfile(PKG_DIR / "docs/development-baseline/runtime-requirements-lock.txt", lock)
    path = program / "config.toml"
    assert not path.exists() and not (program / ".venv").exists()
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    env["PATH"] = os.pathsep.join(part for part in env["PATH"].split(os.pathsep)
        if not any((Path(part) / name).is_file() for name in ("codex.exe", "codex.cmd", "codex.ps1")))
    assert shutil.which("codex", path=env["PATH"]) is None
    setup_port, mcp_port = free_port(), free_port()
    argv = [str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"),
            "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(program / "setup.ps1"),
            "-Config", str(path), "-SetupPort", str(setup_port), "-Port", str(mcp_port), "-Tunnel", "off", "-NoBrowser"]
    # Reuse the existing Windows Job ownership seam for the entire test launcher tree.
    owner = Runtime(path, tmp_path / "launcher-owner")
    owner._server = owner._spawn("server", argv, env)
    client = httpx.Client(base_url=f"http://127.0.0.1:{setup_port}", trust_env=False, timeout=3)
    started = time.monotonic()
    try:
        deadline = started + 240
        while time.monotonic() < deadline:
            if owner._server.poll() is not None:
                pytest.fail((owner.state_dir / "server.log").read_text(encoding="utf-8"))
            try:
                status = client.get("/api/status").json()
                if status.get("local", {}).get("state") == "online":
                    break
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(.25)
        else:
            pytest.fail("isolated first install did not reach real MCP online")
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        assert data["agent"]["enabled"] is False and data["projects"] == []
        assert status["agent"]["availability"] == "disabled"
        assert not (program / ".setup/agent").exists()
        version = subprocess.run([str(program / ".venv/Scripts/python.exe"), "-B", "-c",
            'import importlib.metadata as m;print(m.version("mcp"))'], capture_output=True, text=True)
        assert version.returncode == 0 and version.stdout.strip() == "2.2.0"
        before = path.read_bytes()
        repeated = subprocess.run(argv, env=env, capture_output=True, timeout=25)
        assert repeated.returncode == 0, repeated.stdout.decode(errors="replace")
        assert path.read_bytes() == before
        assert client.get("/api/status").json()["owner_pid"] == status["owner_pid"]
        (tmp_path / "bootstrap-result.json").write_text(json.dumps({"installed": True,
            "codex_discoverable": False, "agent_enabled": False, "mcp_online": True,
            "repeated_start_reused_owner": True, "mcp_version": version.stdout.strip(),
            "elapsed_seconds": round(time.monotonic() - started, 2)}), encoding="utf-8")
        session = client.get("/api/session").json()
        client.post("/api/stop", json={}, headers={"X-Setup-Token": session["token"]})
        owner._server.wait(timeout=15)
        assert owner._server.returncode == 0
    finally:
        owner.stop()
        client.close()
