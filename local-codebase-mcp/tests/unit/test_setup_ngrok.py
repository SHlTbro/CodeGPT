"""Write-only ngrok HTTP API, credential rollback and crash recovery behavior."""
from __future__ import annotations

import concurrent.futures
import copy
import json
import os
import subprocess
import sys
import tomllib
import threading
import time
from pathlib import Path

import httpx2 as httpx
import pytest

import config as config_mod
import setup
from config import ConfigError, ConfigConflictError, ConfigStore, config_to_dict, dump_toml, ensure_config, load_config

NEW_TOKEN = "test_authtoken_ABCDEFGHIJKLMNOP012345"
OLD_TOKEN = "old_authtoken_ABCDEFGHIJKLMNOP012345"


class FakeRuntime:
    """Stable process/protocol seam; real runtime protocol checks have separate tests."""
    def __init__(self, config_path, state_dir):
        self.config_path = Path(config_path)
        self.calls = []
        self.public_calls = []
        self.public_failure = False
        self.local_verified = True
        self.public_verified = True
        self.public_scheme = "https"
        self.delay = 0
        self.active = 0
        self.max_active = 0

    def apply(self, config, force_tunnel_restart=False):
        token_path = self.config_path.parent / "ngrok-token.txt"
        credential = token_path.read_bytes() if token_path.exists() else None
        self.calls.append((config.tunnel.mode, force_tunnel_restart, credential))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self.delay)
            return {"verified": self.local_verified or config.tunnel.mode == "off", "action": "restart",
                    "argv": ["must-never-return"], "token": NEW_TOKEN}
        finally:
            self.active -= 1

    def verify_public(self, config, timeout=30):
        self.public_calls.append(config)
        if self.public_failure:
            raise RuntimeError("Library accidentally echoed " + NEW_TOKEN)
        return {"public_verified": self.public_verified,
                "public_url": f"{self.public_scheme}://fixture-ngrok.invalid/mcp", "public_latency_ms": 23.4,
                "raw_log": NEW_TOKEN}

    def status(self):
        return {"config": {}, "local": {"state": "online"}}

    def stop(self):
        pass


@pytest.fixture
def ngrok_http(tmp_path, monkeypatch):
    cfg = ensure_config(tmp_path / "config.toml")
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / ("ngrok.exe" if os.name == "nt" else "ngrok")).write_bytes(b"fixture; never launched")
    monkeypatch.setattr(setup, "BASE_DIR", agent_dir)
    monkeypatch.setattr(setup.shutil, "which", lambda _: None)
    monkeypatch.setattr(setup, "Runtime", FakeRuntime)
    app = setup.SetupApplication(cfg, tmp_path / "state", setup_port=0)
    server = setup.SetupServer(("127.0.0.1", 0), app)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    client = httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", timeout=10, trust_env=False)
    client.headers["X-Setup-Token"] = client.get("/api/session").json()["token"]
    try:
        yield {"app": app, "client": client, "config": cfg, "base": tmp_path, "server": server,
               "agent": agent_dir, "runtime": app.runtime}
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def enable(env, token=NEW_TOKEN, **extra):
    revision = env["client"].get("/api/config").json()["revision"]
    return env["client"].post("/api/ngrok", json={"revision": revision, "token": token, **extra})


def test_status_exposes_only_two_booleans_and_never_a_credential(ngrok_http):
    env = ngrok_http
    assert env["client"].get("/api/ngrok").json() == {"credential_saved": False, "agent_available": True}
    env["app"].ngrok_token_path.write_text(OLD_TOKEN, encoding="ascii")
    response = env["client"].get("/api/ngrok")
    assert response.json() == {"credential_saved": True, "agent_available": True}
    assert OLD_TOKEN not in response.text
    assert env["client"].get("/ngrok-token.txt").status_code == 404
    assert env["client"].get("/api/ngrok/token").status_code == 404


def test_enable_commits_fixed_credential_toml_and_public_verification(ngrok_http):
    env = ngrok_http
    response = enable(env)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["ok"] is True and result["credential_saved"] is True
    assert result["applied"] == {"verified": True, "action": "restart", "public_verified": True,
                                  "public_url": "https://fixture-ngrok.invalid/mcp", "public_latency_ms": 23.4}
    assert env["app"].ngrok_token_path.read_text(encoding="ascii") == NEW_TOKEN
    assert load_config(env["config"]).tunnel.mode == "ngrok"
    assert load_config(env["config"]).tunnel.public_url == ""
    assert env["runtime"].calls == [("ngrok", True, NEW_TOKEN.encode("ascii"))]
    assert len(env["runtime"].public_calls) == 1
    assert NEW_TOKEN not in response.text and NEW_TOKEN not in env["config"].read_text(encoding="utf-8")
    assert env["app"].store.last_good_path.read_bytes() == env["config"].read_bytes()
    if os.name != "nt":
        assert env["app"].ngrok_token_path.stat().st_mode & 0o777 == 0o600


def test_fixed_dashboard_command_is_parsed_without_execution(ngrok_http):
    response = enable(ngrok_http, "ngrok config add-authtoken " + NEW_TOKEN)
    assert response.status_code == 200, response.text
    assert ngrok_http["app"].ngrok_token_path.read_text(encoding="ascii") == NEW_TOKEN


@pytest.mark.parametrize("value", [None, 42, [], {}, "short", "a" * 513, "x" * 22 + ";whoami",
                                   NEW_TOKEN + "\nwhoami", NEW_TOKEN + "\n", NEW_TOKEN + " extra",
                                   'ngrok config add-authtoken "' + NEW_TOKEN + '"',
                                   "ngrok config add-authtoken " + NEW_TOKEN + " --config elsewhere",
                                   "ngrok config add-authtoken " + NEW_TOKEN + "\n"])
def test_bad_tokens_commands_and_controls_do_not_mutate_or_echo(ngrok_http, value):
    before = ngrok_http["config"].read_bytes()
    response = enable(ngrok_http, value)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_NGROK_TOKEN"
    assert "Traceback" not in response.text and NEW_TOKEN not in response.text
    assert ngrok_http["config"].read_bytes() == before
    assert not ngrok_http["app"].ngrok_token_path.exists()
    assert ngrok_http["runtime"].calls == []


def test_empty_input_requires_a_saved_credential(ngrok_http):
    assert enable(ngrok_http, "").status_code == 422
    ngrok_http["app"].ngrok_token_path.write_text(OLD_TOKEN, encoding="ascii")
    response = enable(ngrok_http, "")
    assert response.status_code == 200, response.text
    assert ngrok_http["app"].ngrok_token_path.read_text(encoding="ascii") == OLD_TOKEN
    assert ngrok_http["runtime"].calls == [("ngrok", True, OLD_TOKEN.encode("ascii"))]


def test_stale_revision_never_replaces_an_existing_credential(ngrok_http):
    env = ngrok_http
    env["app"].ngrok_token_path.write_text(OLD_TOKEN, encoding="ascii")
    before = env["config"].read_bytes()
    response = env["client"].post("/api/ngrok", json={"revision": "0" * 64, "token": NEW_TOKEN})
    assert response.status_code == 409
    assert env["app"].ngrok_token_path.read_text(encoding="ascii") == OLD_TOKEN
    assert env["config"].read_bytes() == before and env["runtime"].calls == []


def test_missing_agent_provides_official_install_action_without_writing(ngrok_http):
    env = ngrok_http
    for path in env["agent"].iterdir():
        path.unlink()
    response = enable(env)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "NGROK_NOT_INSTALLED"
    assert "https://ngrok.com/download" in response.json()["error"]["hint"]
    assert not env["app"].ngrok_token_path.exists() and env["runtime"].calls == []


@pytest.mark.parametrize("previous_exists", [True, False])
def test_public_failure_restores_previous_secret_toml_last_good_and_runtime(ngrok_http, previous_exists):
    env = ngrok_http
    if previous_exists:
        env["app"].ngrok_token_path.write_text(OLD_TOKEN, encoding="ascii")
    before, good = env["config"].read_bytes(), env["app"].store.last_good_path.read_bytes()
    env["runtime"].public_failure = True
    response = enable(env)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "NGROK_SETUP_FAILED"
    assert NEW_TOKEN not in response.text and "Library" not in response.text and "Traceback" not in response.text
    assert env["config"].read_bytes() == before and env["app"].store.last_good_path.read_bytes() == good
    expected_secret = OLD_TOKEN.encode("ascii") if previous_exists else None
    assert (env["app"].ngrok_token_path.read_bytes() if previous_exists else None) == expected_secret
    assert env["app"].ngrok_token_path.exists() is previous_exists
    assert env["runtime"].calls[-1] == ("off", True, expected_secret)
    assert not env["app"].store.pending_path.exists()


@pytest.mark.parametrize("failure", ["local", "public", "scheme"])
def test_only_verified_local_and_https_public_mcp_can_return_success(ngrok_http, failure):
    env = ngrok_http
    if failure == "local":
        env["runtime"].local_verified = False
    elif failure == "public":
        env["runtime"].public_verified = False
    else:
        env["runtime"].public_scheme = "http"
    response = enable(env)
    assert response.status_code == 503
    assert load_config(env["config"]).tunnel.mode == "off"
    assert not env["app"].ngrok_token_path.exists()


def test_commit_io_failure_restores_secret_and_backend(ngrok_http, monkeypatch):
    env = ngrok_http
    env["app"].ngrok_token_path.write_text(OLD_TOKEN, encoding="ascii")
    before = env["config"].read_bytes()
    original = config_mod._atomic_write
    injected = False
    def fail_one_commit(path, content):
        nonlocal injected
        if Path(path) == env["app"].store.last_good_path and tomllib.loads(content.decode("utf-8")).get("tunnel", {}).get("mode") == "ngrok" and not injected:
            injected = True
            raise OSError("fixture commit failure")
        return original(path, content)
    monkeypatch.setattr(config_mod, "_atomic_write", fail_one_commit)
    response = enable(env)
    assert response.status_code == 503 and injected
    assert env["app"].ngrok_token_path.read_text(encoding="ascii") == OLD_TOKEN
    assert env["config"].read_bytes() == before
    assert env["runtime"].calls[-1] == ("off", True, OLD_TOKEN.encode("ascii"))


@pytest.mark.parametrize("extra", [{"path": "elsewhere"}, {"command": "whoami"}, {"shell": True}])
def test_browser_cannot_choose_secret_paths_or_commands(ngrok_http, extra):
    response = enable(ngrok_http, **extra)
    assert response.status_code == 400
    assert not ngrok_http["app"].ngrok_token_path.exists()


@pytest.mark.parametrize("url", ["http://example.com/mcp", "https://user:pass@example.com/mcp", [], 123])
def test_invalid_public_url_is_rejected_before_credential_write(ngrok_http, url):
    before = ngrok_http["config"].read_bytes()
    response = enable(ngrok_http, public_url=url)
    assert response.status_code == 422
    assert ngrok_http["config"].read_bytes() == before and not ngrok_http["app"].ngrok_token_path.exists()


@pytest.mark.parametrize("headers", [{"X-Setup-Token": ""}, {"Host": "attacker.test"},
                                     {"Origin": "https://attacker.test"}, {"X-Forwarded-For": "127.0.0.1"},
                                     {"Forwarded": "for=127.0.0.1"}, {"Sec-Fetch-Site": "cross-site"}])
def test_credential_mutation_reuses_local_guard_and_session(ngrok_http, headers):
    revision = ngrok_http["client"].get("/api/config").json()["revision"]
    response = ngrok_http["client"].post("/api/ngrok", json={"revision": revision, "token": NEW_TOKEN}, headers=headers)
    assert response.status_code == 403
    assert not ngrok_http["app"].ngrok_token_path.exists() and ngrok_http["runtime"].calls == []


def test_request_limit_rejects_large_secret_bodies(ngrok_http):
    response = ngrok_http["client"].post("/api/ngrok", content="x" * 65537,
                                          headers={"Content-Type": "application/json"})
    assert response.status_code == 413
    assert not ngrok_http["app"].ngrok_token_path.exists()


def test_token_only_same_revision_requests_are_serialized_and_reconnect(ngrok_http):
    env = ngrok_http
    assert enable(env).status_code == 200
    env["runtime"].calls.clear()
    env["runtime"].delay = 0.08
    revision = env["client"].get("/api/config").json()["revision"]
    tokens = ["second_authtoken_ABCDEFGHIJKLMNOP012345", "third_authtoken_ABCDEFGHIJKLMNOP012345"]
    def submit(value):
        return env["client"].post("/api/ngrok", json={"revision": revision, "token": value})
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(submit, tokens))
    assert [response.status_code for response in responses] == [200, 200]
    assert all(response.json()["revision"] == revision for response in responses)
    assert env["runtime"].max_active == 1
    assert all(mode == "ngrok" and force for mode, force, _ in env["runtime"].calls)
    assert env["app"].ngrok_token_path.read_bytes() == env["runtime"].calls[-1][2]


def crash_transaction(env, stage="before_public", module_path=None):
    """Hard-exit a separate owner inside the public one-click application operation."""
    program = """
import importlib.util
import os
import sys
from pathlib import Path
import setup
from config import ConfigStore, dump_toml, read_config_data
if sys.argv[4]:
    spec = importlib.util.spec_from_file_location("historical_setup", sys.argv[4])
    historical = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(historical)
    setup = historical
class CrashRuntime:
    def __init__(self, *args):
        pass
    def apply(self, config, force_tunnel_restart=False):
        return {"verified": True, "action": "restart"}
    def verify_public(self, config, timeout=30):
        if sys.argv[3] in ("before_public", "external_edit"):
            os._exit(86)
        return {"public_verified": True, "public_url": "https://crash-fixture.invalid/mcp", "public_latency_ms": 1}
setup.Runtime = CrashRuntime
setup.BASE_DIR = Path(sys.argv[2])
app = setup.SetupApplication(Path(sys.argv[1]), Path(sys.argv[1]).parent / "state")
if sys.argv[3] == "external_edit":
    original_read = Path.read_bytes
    def external_editor(path):
        content = original_read(path)
        if Path(path) == app.ngrok_token_path:
            later = read_config_data(app.config_path)
            later["server"]["port"] = 8288
            app.config_path.write_bytes(dump_toml(later).encode("utf-8"))
        return content
    Path.read_bytes = external_editor
if sys.argv[3] == "after_commit":
    app._clear_ngrok_recovery = lambda: os._exit(87)
app.setup_ngrok({"revision": ConfigStore(app.config_path).read()["revision"],
                 "token": "test_authtoken_ABCDEFGHIJKLMNOP012345"})
os._exit(99)
"""
    proc = subprocess.run([sys.executable, "-c", program, str(env["config"]), str(env["agent"]), stage,
                           str(module_path or "")], cwd=setup.WEB_DIR.parent,
                          capture_output=True, encoding="utf-8", errors="replace", timeout=15)
    assert proc.returncode == (87 if stage == "after_commit" else 86), proc.stderr


@pytest.mark.parametrize("stage", ["before_public", "after_commit"])
@pytest.mark.parametrize("previous_exists", [True, False])
def test_hard_crash_restores_previous_verified_pair_before_first_read(ngrok_http, stage, previous_exists):
    env = ngrok_http
    before = env["config"].read_bytes()
    if previous_exists:
        env["app"].ngrok_token_path.write_text(OLD_TOKEN, encoding="ascii")
    crash_transaction(env, stage)
    assert env["app"].ngrok_token_path.read_text(encoding="ascii") == NEW_TOKEN
    assert env["app"].ngrok_recovery_path.exists()
    assert load_config(env["config"]).tunnel.mode == "ngrok"
    assert env["app"].recover_ngrok_credentials() is True
    assert env["config"].read_bytes() == before
    assert env["app"].store.last_good_path.read_bytes() == before
    assert env["app"].ngrok_token_path.exists() is previous_exists
    if previous_exists:
        assert env["app"].ngrok_token_path.read_text(encoding="ascii") == OLD_TOKEN
    assert not env["app"].ngrok_recovery_path.exists()
    assert not env["app"].store.pending_path.exists()
    assert env["client"].get("/api/ngrok").json()["credential_saved"] is previous_exists
    assert "恢复" in env["client"].get("/api/config").json()["config_error"]


def test_hard_crash_token_only_same_revision_recovers_previous_credential(ngrok_http):
    env = ngrok_http
    assert enable(env, OLD_TOKEN).status_code == 200
    before = env["config"].read_bytes()
    crash_transaction(env)
    assert env["config"].read_bytes() == before
    assert env["app"].ngrok_token_path.read_text(encoding="ascii") == NEW_TOKEN
    assert env["app"].recover_ngrok_credentials() is True
    assert env["config"].read_bytes() == before
    assert env["app"].ngrok_token_path.read_text(encoding="ascii") == OLD_TOKEN


def test_crash_recovery_preserves_a_later_external_toml_edit(ngrok_http):
    env = ngrok_http
    env["app"].ngrok_token_path.write_text(OLD_TOKEN, encoding="ascii")
    crash_transaction(env)
    later = config_to_dict(load_config(env["config"]))
    later["server"]["port"] = 8288
    later_bytes = dump_toml(later).encode("utf-8")
    env["config"].write_bytes(later_bytes)
    good = env["app"].store.last_good_path.read_bytes()
    with pytest.raises(ConfigConflictError):
        env["app"].recover_ngrok_credentials()
    assert env["config"].read_bytes() == later_bytes
    assert env["app"].store.last_good_path.read_bytes() == good
    assert env["app"].ngrok_token_path.read_text(encoding="ascii") == NEW_TOKEN
    assert env["app"].ngrok_recovery_path.exists()


def test_managed_ngrok_error_preserves_only_its_actionable_safe_advice(ngrok_http):
    env = ngrok_http
    def fail(config, timeout=30):
        raise setup.ManagedRuntimeError("ERR_NGROK_108：在线 agent 数量已达上限，请停止账户中不用的 agent。")
    env["runtime"].verify_public = fail
    response = enable(env)
    assert response.status_code == 503
    assert "ERR_NGROK_108" in response.json()["error"]["hint"]
    assert NEW_TOKEN not in response.text
    assert not env["app"].ngrok_recovery_path.exists()


def test_ngrok_guide_does_not_reuse_a_previous_cloudflare_address(ngrok_http):
    env = ngrok_http
    state = env["app"].store.read()
    desired = copy.deepcopy(state["data"])
    desired["tunnel"] = {"mode": "cloudflare", "public_url": "https://old-cloudflare.example/mcp"}
    env["app"].store.save(desired, state["revision"], lambda config: {"verified": True})
    response = enable(env)
    assert response.status_code == 200
    assert load_config(env["config"]).tunnel.public_url == ""


def test_symlink_credential_is_never_read_or_overwritten(ngrok_http):
    env = ngrok_http
    elsewhere = env["base"] / "external-token.txt"
    elsewhere.write_text(OLD_TOKEN, encoding="ascii")
    try:
        env["app"].ngrok_token_path.symlink_to(elsewhere)
    except OSError:
        pytest.skip("OS does not permit symlink creation")
    response = enable(env)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSAFE_CREDENTIAL_FILE"
    assert elsewhere.read_text(encoding="ascii") == OLD_TOKEN
    assert env["runtime"].calls == []


def test_pending_credential_never_has_a_get_or_static_file_interface(ngrok_http):
    env = ngrok_http
    env["app"].ngrok_token_path.write_text(OLD_TOKEN, encoding="ascii")
    crash_transaction(env)
    for path in ("/.setup/ngrok-credential.pending", "/api/ngrok/pending", "/api/credentials", "/ngrok-credential.pending"):
        response = env["client"].get(path)
        assert response.status_code == 404
        assert NEW_TOKEN not in response.text and OLD_TOKEN not in response.text
    env["app"].recover_ngrok_credentials()


def test_external_edit_before_journal_creation_is_not_claimed_by_this_transaction(ngrok_http):
    env = ngrok_http
    env["app"].ngrok_token_path.write_text(OLD_TOKEN, encoding="ascii")
    crash_transaction(env, "external_edit")
    later = env["config"].read_bytes()
    assert load_config(env["config"]).server.port == 8288
    with pytest.raises(ConfigConflictError):
        env["app"].recover_ngrok_credentials()
    assert env["config"].read_bytes() == later
    assert env["app"].ngrok_token_path.read_text(encoding="ascii") == NEW_TOKEN
    assert env["app"].ngrok_recovery_path.exists()


@pytest.mark.parametrize("guard_header", ["X-Setup-Token: invalid", "Origin: https://attacker.test"])
@pytest.mark.parametrize("fragmented", [False, True, "late_body"])
def test_guard_rejection_finishes_http_response_without_reset(ngrok_http, guard_header, fragmented):
    """Leave enough unread TCP data to reproduce WinError 10054 deterministically."""
    import socket
    env = ngrok_http
    before = env["config"].read_bytes()
    body = b"this forbidden body must never be parsed".ljust(65536, b"x")
    headers = (f"POST /api/ngrok HTTP/1.1\r\nHost: 127.0.0.1:{env['server'].server_port}\r\n"
               f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
               f"{guard_header}\r\nConnection: close\r\n\r\n").encode("ascii")
    with socket.create_connection(("127.0.0.1", env["server"].server_port), timeout=5) as wire:
        wire.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        prefix = b""
        if fragmented == "late_body":
            wire.sendall(headers)
            # The server has rejected the headers before this request body arrives.
            # Reading one response byte makes the old close-before-drain race stable.
            prefix = wire.recv(1)
            time.sleep(0.05)
            wire.sendall(body[:32768])
            wire.sendall(body[32768:])
        elif fragmented:
            wire.sendall(headers + body[:1024])
            wire.sendall(body[1024:32768])
            wire.sendall(body[32768:])
        else:
            wire.sendall(headers + body)
        chunks = [prefix]
        while True:
            chunk = wire.recv(8192)
            if not chunk:
                break
            chunks.append(chunk)
    response = b"".join(chunks)
    head, data = response.split(b"\r\n\r\n", 1)
    assert b" 403 " in head
    assert json.loads(data)["error"]["code"] == ("TOKEN_REQUIRED" if guard_header.startswith("X-") else "ORIGIN_FORBIDDEN")
    assert env["config"].read_bytes() == before
    assert not env["app"].ngrok_token_path.exists() and env["runtime"].calls == []


def raw_request(env, header_bytes, body, delayed=False, halfclose=True):
    import socket
    with socket.create_connection(("127.0.0.1", env["server"].server_port), timeout=4) as wire:
        wire.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        wire.sendall(header_bytes)
        prefix = b""
        if delayed:
            prefix = wire.recv(1)
            time.sleep(0.05)
        wire.sendall(body)
        if halfclose:
            wire.shutdown(socket.SHUT_WR)
        chunks = [prefix]
        while True:
            chunk = wire.recv(8192)
            if not chunk:
                break
            chunks.append(chunk)
    head, data = b"".join(chunks).split(b"\r\n\r\n", 1)
    return head, json.loads(data)


def test_late_chunked_forbidden_body_still_returns_complete_403(ngrok_http):
    env = ngrok_http
    body = b"1000\r\n" + b"x" * 4096 + b"\r\n0\r\n\r\n"
    headers = (f"POST /api/ngrok HTTP/1.1\r\nHost: 127.0.0.1:{env['server'].server_port}\r\n"
               "Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n"
               "Origin: https://attacker.test\r\nConnection: close\r\n\r\n").encode("ascii")
    head, result = raw_request(env, headers, body, delayed=True)
    assert b" 403 " in head and result["error"]["code"] == "ORIGIN_FORBIDDEN"
    assert env["runtime"].calls == [] and not env["app"].ngrok_token_path.exists()


@pytest.mark.parametrize("framing", ["short", "duplicate", "chunked"])
def test_invalid_body_framing_is_rejected_before_authorized_mutation(ngrok_http, framing):
    env = ngrok_http
    before = env["config"].read_bytes()
    revision = env["client"].get("/api/config").json()["revision"]
    body = json.dumps({"revision": revision, "token": NEW_TOKEN}).encode("ascii")
    if framing == "short":
        length_header = f"Content-Length: {len(body) + 20}\r\n"
    elif framing == "duplicate":
        length_header = f"Content-Length: {len(body)}\r\nContent-Length: {len(body) + 20}\r\n"
    else:
        length_header = f"Content-Length: {len(body)}\r\nTransfer-Encoding: chunked\r\n"
    headers = (f"POST /api/ngrok HTTP/1.1\r\nHost: 127.0.0.1:{env['server'].server_port}\r\n"
               f"Content-Type: application/json\r\nX-Setup-Token: {env['app'].token}\r\n"
               f"{length_header}Connection: close\r\n\r\n").encode("ascii")
    head, result = raw_request(env, headers, body)
    assert b" 400 " in head
    assert "error" in result and "Traceback" not in json.dumps(result)
    assert env["config"].read_bytes() == before
    assert env["runtime"].calls == [] and not env["app"].ngrok_token_path.exists()


def test_network_status_stays_responsive_during_config_transaction(ngrok_http):
    """Network monitoring must remain readable during a slow verified save."""
    env = ngrok_http
    env['runtime'].delay = 1.5
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        saving = pool.submit(enable, env)
        deadline = time.monotonic() + 2
        while not env['runtime'].calls and time.monotonic() < deadline:
            time.sleep(0.01)
        assert env['runtime'].calls
        response = env['client'].get('/api/status', timeout=0.7)
        assert response.status_code == 200
        assert isinstance(response.json()['startup']['verified'], bool)
        assert saving.result(timeout=5).status_code == 200


def test_status_observes_external_startup_preference_without_stale_snapshot(ngrok_http):
    env = ngrok_http
    assert env['client'].get('/api/status').json()['startup']['verified'] is True
    data = config_to_dict(load_config(env['config']))
    data['startup']['enabled'] = True
    env['config'].write_text(dump_toml(data), encoding='utf-8')
    response = env['client'].get('/api/status')
    assert response.status_code == 200
    assert response.json()['startup']['registered'] is False
    assert response.json()['startup']['verified'] is False
