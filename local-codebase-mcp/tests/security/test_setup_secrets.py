from pathlib import Path
import pytest
from security import is_sensitive


def test_operational_secrets_cannot_be_read_even_with_custom_patterns():
    for name in ('ngrok-token.txt', 'tunnel-token.txt', '.setup/tunnel.log', 'nested/.setup/activity.json', '.setup-work/private.txt'):
        assert is_sensitive(name, [])
    assert not is_sensitive('src/main.py', [])


def test_private_credential_journal_cannot_be_read_when_setup_itself_is_project(tmp_path):
    import asyncio
    import json
    import socket
    from config import Config, ServerConfig, config_to_dict, dump_toml
    from projects import Project
    from runtime import Runtime
    from fastmcp import Client

    private = tmp_path / ".setup"
    private.mkdir()
    secret = "fixture_old_ngrok_secret_never_return_this"
    (private / "ngrok-credential.pending").write_text(json.dumps({"old_secret": secret}), encoding="utf-8")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    config = Config(projects=(Project("private", "Private State", private),),
                    server=ServerConfig(port=port), security={"sensitive_patterns": []})
    path = tmp_path / "config.toml"
    path.write_text(dump_toml(config_to_dict(config)), encoding="utf-8")
    runtime = Runtime(path, tmp_path / "runtime-state", probe_interval=60)
    try:
        runtime.start()
        async def read_public_tool():
            async with Client(runtime.status()["local"]["url"], mode="legacy") as client:
                return (await client.call_tool("read_file", {"project_id": "private", "path": "ngrok-credential.pending"})).data
        result = asyncio.run(read_public_tool())
        assert result.get("error", {}).get("code") == "FILE_NOT_VISIBLE"
        assert secret not in json.dumps(result)
        assert is_sensitive("ngrok-credential.pending", [])
    finally:
        runtime.stop()


@pytest.mark.parametrize(("root_name", "secret_name"), [
    (".setup", "ngrok-credential.pending"),
    (".", "ngrok-token.txt"),
])
def test_atomic_credential_temporary_file_cannot_be_read(tmp_path, monkeypatch, root_name, secret_name):
    import asyncio
    import json
    import socket
    import threading
    import config as configuration
    from config import Config, ServerConfig, config_to_dict, dump_toml
    from projects import Project
    from runtime import Runtime
    from fastmcp import Client

    private = tmp_path / root_name
    private.mkdir(exist_ok=True)
    destination = private / secret_name
    secret = b"fixture_atomic_ngrok_secret_never_return_this"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    cfg = Config(projects=(Project("private", "Private State", private),),
                 server=ServerConfig(port=port), security={"sensitive_patterns": []})
    path = tmp_path / "config.toml"
    path.write_text(dump_toml(config_to_dict(cfg)), encoding="utf-8")
    runtime = Runtime(path, tmp_path / "runtime-state", probe_interval=60)
    ready, release = threading.Event(), threading.Event()
    temporary, failures = [], []
    original_replace = configuration.os.replace
    writer = None
    def paused_replace(source, target):
        if Path(target) == destination:
            temporary.append(Path(source))
            ready.set()
            if not release.wait(timeout=15):
                raise OSError("fixture writer was not released")
        return original_replace(source, target)
    def write_secret():
        try:
            configuration._atomic_write(destination, secret)
        except Exception as error:
            failures.append(error)
    try:
        runtime.start()
        monkeypatch.setattr(configuration.os, "replace", paused_replace)
        writer = threading.Thread(target=write_secret, daemon=True)
        writer.start()
        assert ready.wait(timeout=5)
        # Actual atomic writer has flushed its real temporary secret file. Hold the
        # replacement seam so the MCP read deterministically covers this window.
        assert temporary[0].read_bytes() == secret
        async def read_public_tool():
            async with Client(runtime.status()["local"]["url"], mode="legacy") as client:
                return (await client.call_tool("read_file", {"project_id": "private", "path": temporary[0].name})).data
        result = asyncio.run(read_public_tool())
        assert result.get("error", {}).get("code") == "FILE_NOT_VISIBLE"
        assert secret.decode() not in json.dumps(result)
    finally:
        release.set()
        if writer:
            writer.join(timeout=5)
        runtime.stop()
    assert not failures
    assert destination.read_bytes() == secret
