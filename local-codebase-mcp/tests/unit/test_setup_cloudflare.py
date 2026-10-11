"""Real HTTP/canonical transactions for a newly owned Cloudflare MCP tunnel."""
from __future__ import annotations

import concurrent.futures
import copy
import os
import threading
import time
import tomllib
from pathlib import Path

import httpx2 as httpx
import pytest

import config as config_mod
import setup
from config import dump_toml, ensure_config, load_config


class CloudflareRuntime:
    """Stable lifecycle/protocol seam; real cloudflared is tested separately."""
    def __init__(self, config_path, state_dir):
        self.config_path = Path(config_path)
        self.calls = []
        self.public_calls = []
        self.local_verified = True
        self.public_result = {"public_verified": True, "public_url": "https://fixture-new.trycloudflare.com/mcp",
                              "public_latency_ms": 15.6, "private_log": "must-never-return"}
        self.local_failure = None
        self.public_failure = None
        self.external_edit = None
        self.delay = 0
        self.active = 0
        self.max_active = 0
        self.current = load_config(self.config_path)

    def apply(self, config, force_tunnel_restart=False):
        # The callback must observe a real complete canonical file already on disk.
        assert load_config(self.config_path) == config
        self.calls.append((config, force_tunnel_restart))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self.delay)
            if self.local_failure is not None:
                failure, self.local_failure = self.local_failure, None
                raise failure
            self.current = config
            verified, self.local_verified = self.local_verified, True
            return {"verified": verified, "action": "hot_reload", "argv": ["private-command"], "token": "private-value"}
        finally:
            self.active -= 1

    def verify_public(self, config, timeout=30):
        self.public_calls.append(config)
        assert config.tunnel.mode == "cloudflare" and config.tunnel.public_url == ""
        if self.external_edit is not None:
            self.config_path.write_bytes(self.external_edit)
        if self.public_failure is not None:
            raise self.public_failure
        return copy.deepcopy(self.public_result)

    def status(self):
        return {"config": {}, "local": {"state": "online"}}

    def stop(self):
        pass


@pytest.fixture
def cloudflare_http(tmp_path, monkeypatch):
    cfg = ensure_config(tmp_path / "config.toml")
    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / ("cloudflared.exe" if os.name == "nt" else "cloudflared")).write_bytes(b"fixture; never executed")
    monkeypatch.setattr(setup, "BASE_DIR", agent)
    monkeypatch.setattr(setup.shutil, "which", lambda _: None)
    monkeypatch.setattr(setup, "Runtime", CloudflareRuntime)
    app = setup.SetupApplication(cfg, tmp_path / "state", setup_port=0)
    server = setup.SetupServer(("127.0.0.1", 0), app)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    client = httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", timeout=10, trust_env=False)
    client.headers["X-Setup-Token"] = client.get("/api/session").json()["token"]
    try:
        yield {"app": app, "client": client, "config": cfg, "base": tmp_path,
               "agent": agent, "runtime": app.runtime}
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def enable(env, **extra):
    revision = env["client"].get("/api/config").json()["revision"]
    return env["client"].post("/api/cloudflare", json={"revision": revision, **extra})


def set_previous(env, mode="cloudflare", public_url=""):
    snapshot = env["app"].store.read()
    data = copy.deepcopy(snapshot["data"])
    data["tunnel"] = {"mode": mode, "public_url": public_url}
    env["app"].store.save(data, snapshot["revision"], env["runtime"].apply)
    env["runtime"].calls.clear()


def test_enable_changes_canonical_and_requires_local_and_public_verification(cloudflare_http):
    env = cloudflare_http
    old = env["config"].read_bytes()
    response = enable(env)
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and body["data"]["tunnel"] == {"mode": "cloudflare", "public_url": ""}
    assert body["applied"] == {"verified": True, "action": "hot_reload", "public_verified": True,
                               "public_url": "https://fixture-new.trycloudflare.com/mcp", "public_latency_ms": 15.6}
    assert env["config"].read_bytes() != old
    assert tomllib.loads(env["config"].read_text(encoding="utf-8"))["tunnel"] == body["data"]["tunnel"]
    assert env["app"].store.last_good_path.read_bytes() == env["config"].read_bytes()
    assert env["runtime"].calls == [(load_config(env["config"]), True)]
    assert env["runtime"].public_calls == [load_config(env["config"])]
    assert not env["app"].store.pending_path.exists()
    assert not env["app"].ngrok_token_path.exists() and not env["app"].ngrok_recovery_path.exists()
    assert "private" not in response.text


def test_saved_cloudflare_without_config_changes_still_forces_new_tunnel_and_verifies(cloudflare_http):
    env = cloudflare_http
    set_previous(env)
    before = env["app"].store.read()
    response = enable(env)
    assert response.status_code == 200
    assert response.json()["revision"] == before["revision"]
    assert len(env["runtime"].calls) == len(env["runtime"].public_calls) == 1
    assert env["runtime"].calls[0][1] is True


@pytest.mark.parametrize("previous", ["ngrok", "cloudflare"])
def test_old_public_url_is_cleared_and_existing_credentials_are_untouched(cloudflare_http, previous):
    env = cloudflare_http
    secret = b"existing-local-secret-must-stay-private"
    env["app"].ngrok_token_path.write_bytes(secret)
    set_previous(env, previous, "https://old-ngrok.example/mcp")
    response = enable(env)
    assert response.status_code == 200
    assert load_config(env["config"]).tunnel.public_url == ""
    assert env["app"].ngrok_token_path.read_bytes() == secret
    assert secret.decode() not in response.text and "old-ngrok" not in response.text


def test_status_returns_only_agent_availability_and_checks_path(cloudflare_http, monkeypatch):
    env = cloudflare_http
    assert env["client"].get("/api/cloudflare").json() == {"agent_available": True}
    for path in env["agent"].iterdir():
        path.unlink()
    assert env["client"].get("/api/cloudflare").json() == {"agent_available": False}
    monkeypatch.setattr(setup.shutil, "which", lambda name: "trusted-path/cloudflared" if name == "cloudflared" else None)
    assert env["client"].get("/api/cloudflare").json() == {"agent_available": True}


def test_missing_agent_fails_before_mutation_with_official_install_advice(cloudflare_http):
    env = cloudflare_http
    for path in env["agent"].iterdir():
        path.unlink()
    before = env["config"].read_bytes()
    response = enable(env)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "CLOUDFLARE_NOT_INSTALLED"
    assert "https://developers.cloudflare.com/" in response.json()["error"]["hint"]
    assert env["config"].read_bytes() == before and env["runtime"].calls == []


@pytest.mark.parametrize("body", [{}, [], {"revision": None}, {"revision": "0" * 63}, {"revision": "g" * 64},
                                  {"revision": "0" * 64, "public_url": "https://attacker.test/mcp"},
                                  {"revision": "0" * 64, "command": "whoami"},
                                  {"revision": "0" * 64, "path": "other.toml"}])
def test_strict_request_cannot_choose_paths_addresses_or_commands(cloudflare_http, body):
    env = cloudflare_http
    before = env["config"].read_bytes()
    response = env["client"].post("/api/cloudflare", json=body)
    assert response.status_code == 400
    assert env["config"].read_bytes() == before and env["runtime"].calls == []


def test_stale_revision_never_starts_tunnel_or_writes_canonical(cloudflare_http):
    env = cloudflare_http
    before = env["config"].read_bytes()
    response = env["client"].post("/api/cloudflare", json={"revision": "0" * 64})
    assert response.status_code == 409
    assert env["config"].read_bytes() == before and env["runtime"].calls == []


@pytest.mark.parametrize("stage", ["local", "public", "local_unverified", "public_unverified"])
@pytest.mark.parametrize("previous", ["off", "cloudflare"])
def test_failure_rolls_back_exact_last_good_and_reapplies_old_runtime(cloudflare_http, stage, previous):
    env = cloudflare_http
    set_previous(env, previous)
    before, good = env["config"].read_bytes(), env["app"].store.last_good_path.read_bytes()
    if stage == "local":
        env["runtime"].local_failure = ValueError("private process traceback")
    elif stage == "public":
        env["runtime"].public_failure = ValueError("private process traceback")
    elif stage == "local_unverified":
        env["runtime"].local_verified = False
    else:
        env["runtime"].public_result["public_verified"] = False
    response = enable(env)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "CLOUDFLARE_SETUP_FAILED"
    assert "private" not in response.text and "Traceback" not in response.text
    assert env["config"].read_bytes() == before and env["app"].store.last_good_path.read_bytes() == good
    assert env["runtime"].current == load_config(env["config"])
    assert len(env["runtime"].calls) == 2 and all(force for _, force in env["runtime"].calls)
    assert env["runtime"].calls[-1][0].tunnel.mode == previous
    assert len(env["runtime"].public_calls) <= 1
    assert not env["app"].store.pending_path.exists()


@pytest.mark.parametrize("url", ["http://fixture.trycloudflare.com/mcp", "https://old-ngrok.example/mcp",
                                 "https://fixture.trycloudflare.com/api/config", "https://fixture.trycloudflare.com/mcp?secret=x",
                                 "https://user:secret@fixture.trycloudflare.com/mcp", "https://fixture.trycloudflare.com:443/mcp"])
def test_success_requires_https_owned_cloudflare_url_with_exact_mcp_path(cloudflare_http, url):
    env = cloudflare_http
    before = env["config"].read_bytes()
    env["runtime"].public_result["public_url"] = url
    response = enable(env)
    assert response.status_code == 503
    assert env["config"].read_bytes() == before


def test_commit_io_failure_after_public_verification_restores_last_good(cloudflare_http, monkeypatch):
    env = cloudflare_http
    before = env["config"].read_bytes()
    original = config_mod._atomic_write
    injected = False
    def fail_commit(path, content):
        nonlocal injected
        if Path(path) == env["app"].store.last_good_path and tomllib.loads(content.decode("utf-8"))["tunnel"]["mode"] == "cloudflare" and not injected:
            injected = True
            raise OSError("private commit failure")
        return original(path, content)
    monkeypatch.setattr(config_mod, "_atomic_write", fail_commit)
    response = enable(env)
    assert injected and response.status_code == 503
    assert env["config"].read_bytes() == before and env["app"].store.last_good_path.read_bytes() == before
    assert env["runtime"].calls[-1][0].tunnel.mode == "off" and env["runtime"].calls[-1][1] is True
    assert not env["app"].store.pending_path.exists()


def test_candidate_atomic_write_failure_restores_old_backend_without_public_enable(cloudflare_http, monkeypatch):
    env = cloudflare_http
    before = env["config"].read_bytes()
    original = config_mod._atomic_write
    injected = False
    def fail_replace(path, content):
        nonlocal injected
        if Path(path) == env["config"] and tomllib.loads(content.decode("utf-8"))["tunnel"]["mode"] == "cloudflare" and not injected:
            injected = True
            raise OSError("candidate atomic replace unavailable")
        return original(path, content)
    monkeypatch.setattr(config_mod, "_atomic_write", fail_replace)
    response = enable(env)
    assert injected and response.status_code == 503
    assert env["config"].read_bytes() == before and env["app"].store.last_good_path.read_bytes() == before
    assert len(env["runtime"].calls) == 1 and env["runtime"].calls[-1][0].tunnel.mode == "off"
    assert env["runtime"].calls[-1][1] is True and env["runtime"].public_calls == []
    assert not env["app"].store.pending_path.exists()


def test_same_config_atomic_write_failure_recovers_locally_while_public_is_unavailable(cloudflare_http, monkeypatch):
    env = cloudflare_http
    set_previous(env)
    before = env["config"].read_bytes()
    env["runtime"].public_failure = setup.ManagedRuntimeError("Cloudflare 公网暂时不可达；检查外网连接。")
    original = config_mod._atomic_write
    injected = False
    def fail_replace(path, content):
        nonlocal injected
        if Path(path) == env["config"] and not injected:
            injected = True
            raise OSError("same-config atomic replacement unavailable")
        return original(path, content)
    monkeypatch.setattr(config_mod, "_atomic_write", fail_replace)
    response = enable(env)
    assert injected and response.status_code == 503
    assert env["config"].read_bytes() == before and env["app"].store.last_good_path.read_bytes() == before
    assert len(env["runtime"].calls) == 1 and env["runtime"].calls[0][1] is True
    assert env["runtime"].public_calls == []
    assert not env["app"].store.pending_path.exists()
    assert "后端恢复尚未确认" not in response.json()["error"]["hint"]
    assert env["runtime"].current == load_config(env["config"])


def test_external_edit_during_public_verification_is_preserved_in_rejected(cloudflare_http):
    env = cloudflare_http
    before = env["config"].read_bytes()
    data = copy.deepcopy(env["app"].store.read()["data"])
    data["server"]["port"] = 8277
    external = dump_toml(data).encode("utf-8")
    env["runtime"].external_edit = external
    response = enable(env)
    assert response.status_code == 503
    assert env["config"].read_bytes() == before
    assert env["app"].store.rejected_path.read_bytes() == external
    assert env["runtime"].current.server.port == load_config(env["config"]).server.port


@pytest.mark.parametrize("headers", [{"X-Setup-Token": ""}, {"Host": "attacker.test"},
                                     {"Origin": "https://attacker.test"}, {"X-Forwarded-For": "127.0.0.1"},
                                     {"Forwarded": "for=127.0.0.1"}, {"Sec-Fetch-Site": "cross-site"}])
def test_cloudflare_mutation_has_same_local_session_host_and_origin_guard(cloudflare_http, headers):
    env = cloudflare_http
    before = env["config"].read_bytes()
    revision = env["client"].get("/api/config").json()["revision"]
    response = env["client"].post("/api/cloudflare", json={"revision": revision}, headers=headers)
    assert response.status_code == 403
    assert env["config"].read_bytes() == before and env["runtime"].calls == []


def test_cloudflare_request_size_remains_bounded(cloudflare_http):
    env = cloudflare_http
    response = env["client"].post("/api/cloudflare", content="x" * 65537, headers={"Content-Type": "application/json"})
    assert response.status_code == 413 and env["runtime"].calls == []


def test_incomplete_ngrok_credential_recovery_blocks_provider_switch(cloudflare_http):
    env = cloudflare_http
    env["app"].ngrok_recovery_path.parent.mkdir(parents=True, exist_ok=True)
    env["app"].ngrok_recovery_path.write_bytes(b"private pending record")
    before = env["config"].read_bytes()
    response = enable(env)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "CREDENTIAL_RECOVERY_REQUIRED"
    assert env["config"].read_bytes() == before and env["runtime"].calls == []
    assert env["app"].ngrok_recovery_path.read_bytes() == b"private pending record"


def test_same_revision_cloudflare_retries_are_serialized_and_each_reconnects(cloudflare_http):
    env = cloudflare_http
    set_previous(env)
    revision = env["app"].store.read()["revision"]
    env["runtime"].delay = 0.08
    def submit(_):
        return env["client"].post("/api/cloudflare", json={"revision": revision})
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(submit, range(2)))
    assert [response.status_code for response in responses] == [200, 200]
    assert all(response.json()["revision"] == revision for response in responses)
    assert env["runtime"].max_active == 1 and len(env["runtime"].calls) == len(env["runtime"].public_calls) == 2
    assert all(force for _, force in env["runtime"].calls)


def test_managed_runtime_error_keeps_only_safe_actionable_cloudflare_advice(cloudflare_http):
    env = cloudflare_http
    env["runtime"].public_failure = setup.ManagedRuntimeError("Cloudflare 连接尚未建立；检查外网与防火墙后重试。")
    response = enable(env)
    assert response.status_code == 503
    assert response.json()["error"]["hint"] == str(env["runtime"].public_failure)
