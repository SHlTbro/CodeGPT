"""Real C0 parser/server roundtrip; all files, ports and processes are isolated."""
import asyncio
from copy import deepcopy
import hashlib
import importlib
import json
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import socket
import subprocess
import sys
import time
import tomllib

import pytest
from fastmcp import Client

from config import ConfigError, config_from_data, config_to_dict, dump_toml, load_config

ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT.parent / "agent-v1-baseline-test"
C0 = "51b30e42be82290d9f6ab3b44616063bd4c708ff"
SOURCE_REVISION = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()


def migration():
    assert (ROOT / "local_agents/config_migration.py").is_file(), "migration not implemented"
    return importlib.import_module("local_agents.config_migration")


def revision(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def old_parser(path):
    code = "import sys;sys.path.insert(0,sys.argv[1]);from config import load_config;load_config(sys.argv[2]);print('C0 parser accepted')"
    return subprocess.run([sys.executable, "-B", "-c", code, str(BASELINE), str(path)],
                          cwd=path.parent, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
                          env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1"))


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def sample(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    raw = {"projects": [{"id": "retained", "name": "Changed after upgrade", "path": str(project),
                         "write_enabled": True, "write_roots": ["docs", "wiki"],
                         "agent": {"context_enabled": True}}],
           "server": {"host": "127.0.0.1", "port": free_port(), "path": "/mcp"},
           "tunnel": {"mode": "named", "public_url": "https://synthetic-public.example/mcp"},
           "agent": {"enabled": True, "codex": {"enabled": True, "auth_mode": "api_key"},
                     "profiles": {"cheap_reader": {"model": "synthetic-selected-model"}}}}
    cfg = tmp_path / "config.toml"
    cfg.write_text(dump_toml(raw), encoding="utf-8")
    return cfg, tmp_path / "migration.json", raw


def export(sample):
    cfg, record, _ = sample
    return migration().downgrade(cfg, record, revision(cfg), source_revision=SOURCE_REVISION)


def test_actual_c0_parser_rejects_even_disabled_agent_and_export_accepts(sample):
    cfg, record, raw = sample
    raw["agent"]["enabled"] = False
    raw["agent"]["codex"]["enabled"] = False
    raw["projects"][0].pop("agent")  # Isolate the global disabled-agent incompatibility.
    cfg.write_text(dump_toml(raw), encoding="utf-8")
    before = old_parser(cfg)
    assert before.returncode != 0 and "agent" in before.stderr
    export(sample)
    after = old_parser(cfg)
    assert after.returncode == 0, after.stderr
    converted = load_config(cfg)
    assert converted.projects[0].name == "Changed after upgrade"
    assert converted.projects[0].write_roots == ("docs", "wiki")
    assert converted.tunnel.mode == "off" and converted.tunnel.public_url == ""
    assert old_parser(cfg.with_name("config.last-good.toml")).returncode == 0
    assert record.exists()


def test_reupgrade_uses_latest_old_settings_and_does_not_revive_removed_ids(sample):
    cfg, record, raw = sample
    raw["projects"].append({"id": "removed", "name": "Removed", "path": raw["projects"][0]["path"], "agent": {"context_enabled": True}})
    cfg.write_text(dump_toml(raw), encoding="utf-8")
    original_agent = config_to_dict(load_config(cfg))["agent"]
    export(sample)
    downgraded = tomllib.loads(cfg.read_text(encoding="utf-8"))
    downgraded["projects"] = [downgraded["projects"][0], {"id": "new", "name": "New during C0", "path": raw["projects"][0]["path"]}]
    downgraded["projects"][0]["name"] = "Changed during downgrade"
    downgraded["projects"][0]["write_roots"] = ["changed-docs"]
    downgraded["limits"]["max_chars"] = 24000
    cfg.write_text(dump_toml(downgraded), encoding="utf-8")
    assert old_parser(cfg).returncode == 0
    migration().upgrade(cfg, record, revision(cfg), source_revision=SOURCE_REVISION)
    current = load_config(cfg)
    assert [p.id for p in current.projects] == ["retained", "new"]
    assert current.projects[0].name == "Changed during downgrade"
    assert current.projects[0].write_roots == ("changed-docs",)
    assert current.projects[0].agent.context_enabled is True
    assert current.projects[1].agent.context_enabled is False
    assert current.limits["max_chars"] == 24000
    assert current.agent == original_agent
    assert current.tunnel.mode == "off" and current.tunnel.public_url == ""
    ledger = json.loads(record.read_text(encoding="utf-8"))
    assert ledger["state"] == "upgraded"
    assert ledger["target_software_revision"] == C0
    assert ledger["source_software_revision"] == SOURCE_REVISION


def test_source_revision_conflict_keeps_all_original_bytes(sample):
    cfg, record, _ = sample
    before = cfg.read_bytes()
    with pytest.raises(migration().MigrationError, match="REVISION_CONFLICT"):
        migration().downgrade(cfg, record, "0" * 64, source_revision=SOURCE_REVISION)
    assert cfg.read_bytes() == before and not record.exists()


def test_record_write_failure_keeps_configuration_and_last_good(sample, monkeypatch):
    cfg, record, _ = sample
    last_good = cfg.with_name("config.last-good.toml")
    last_good.write_bytes(cfg.read_bytes())
    before = cfg.read_bytes()
    module = migration()
    atomic = module._atomic_write
    def fail_once(path, content):
        if path == cfg:
            raise OSError("synthetic conversion failure")
        atomic(path, content)
    monkeypatch.setattr(module, "_atomic_write", fail_once)
    with pytest.raises(module.MigrationError):
        export(sample)
    assert cfg.read_bytes() == before and last_good.read_bytes() == before
    assert not record.exists()


def test_undefined_auth_schema_is_rejected_without_stripping_or_archiving(sample):
    cfg, record, raw = sample
    raw["auth"] = {"mode": "oauth", "issuer": "https://synthetic-issuer.example"}
    cfg.write_text(dump_toml(raw), encoding="utf-8")
    before = cfg.read_bytes()
    with pytest.raises(migration().MigrationError, match="AUTH_SCHEMA_UNSUPPORTED"):
        export(sample)
    assert cfg.read_bytes() == before and not record.exists()


def test_external_nonsecret_store_sentinels_are_not_read_or_restored(sample):
    cfg, record, _ = sample
    # Synthetic markers, not real credential/report/index services or data.
    state = cfg.parent / "synthetic-independent-state"
    state.mkdir()
    marker = state / "deleted-credential-marker.txt"
    marker.write_text("synthetic nonsecret revoked revision", encoding="utf-8")
    index = state / "idempotency-marker.txt"
    index.write_text("synthetic tombstone", encoding="utf-8")
    export(sample)
    marker.unlink()
    migration().upgrade(cfg, record, revision(cfg), source_revision=SOURCE_REVISION)
    assert not marker.exists()
    assert index.read_text(encoding="utf-8") == "synthetic tombstone"


def test_corrupt_record_keeps_downgraded_configuration(sample):
    cfg, record, _ = sample
    export(sample)
    before = cfg.read_bytes()
    record.write_text('{"api_key":"synthetic-do-not-echo"}', encoding="utf-8")
    with pytest.raises(migration().MigrationError) as error:
        migration().upgrade(cfg, record, revision(cfg), source_revision=SOURCE_REVISION)
    assert "synthetic-do-not-echo" not in str(error.value)
    assert cfg.read_bytes() == before


def test_actual_c0_server_starts_and_lists_projects_after_conversion(sample):
    cfg, _, _ = sample
    export(sample)
    port = load_config(cfg).server.port
    code = "import sys;sys.path.insert(0,sys.argv[1]);import server;from pathlib import Path;server.LOG_PATH=Path(sys.argv[3]);sys.argv=['server.py','--config',sys.argv[2],'--state-dir',sys.argv[4]];server.main()"
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    output = (cfg.parent / "C0-startup-output.log").open("w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-B", "-c", code, str(BASELINE), str(cfg),
                             str(cfg.parent / "C0-owned.log"), str(cfg.parent / "C0-state")],
                            cwd=cfg.parent, env=env, stdout=output, stderr=subprocess.STDOUT)
    async def list_projects():
        # Match C0 runtime.py::_mcp_probe's actual legacy protocol mode.
        async with Client(f"http://127.0.0.1:{port}/mcp", mode="legacy") as client:
            await client.ping()
            assert len(await client.list_tools()) == 15
            results = [(await client.call_tool("list_projects", {})).data["projects"] for _ in range(2)]
            assert results[0] == results[1]
            return results[0]
    try:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            assert proc.poll() is None, "owned C0 test server exited before MCP ready"
            try:
                result = asyncio.run(list_projects())
                break
            except (OSError, ConnectionError):
                time.sleep(0.2)
            except Exception as error:
                if "connect" not in str(error).lower():
                    raise
                time.sleep(0.2)
        else:
            pytest.fail("owned C0 test server not ready")
        assert result == [{"id": "retained", "name": "Changed after upgrade"}]
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)
        output.close()


def test_record_cannot_be_applied_to_another_config_path(sample):
    cfg, record, _ = sample
    export(sample)
    other = cfg.parent / "another.toml"
    other.write_bytes(cfg.read_bytes())
    before = other.read_bytes()
    with pytest.raises(migration().MigrationError, match="RECORD_BINDING_CONFLICT"):
        migration().upgrade(other, record, revision(other), source_revision=SOURCE_REVISION)
    assert other.read_bytes() == before


def test_secret_fields_in_preserved_tunnel_metadata_are_rejected(sample):
    cfg, record, _ = sample
    export(sample)
    value = json.loads(record.read_text(encoding="utf-8"))
    value["prior_tunnel"]["token"] = "synthetic-sensitive-value"
    record.write_text(json.dumps(value), encoding="utf-8")
    before = cfg.read_bytes()
    with pytest.raises(migration().MigrationError) as error:
        migration().upgrade(cfg, record, revision(cfg), source_revision=SOURCE_REVISION)
    assert "synthetic-sensitive-value" not in str(error.value)
    assert cfg.read_bytes() == before


def test_changed_project_root_requires_fresh_opt_in_but_keeps_preference_record(sample):
    cfg, record, _ = sample
    export(sample)
    value = tomllib.loads(cfg.read_text(encoding="utf-8"))
    new_root = cfg.parent / "changed-root"
    new_root.mkdir()
    value["projects"][0]["path"] = str(new_root)
    cfg.write_text(dump_toml(value), encoding="utf-8")
    migration().upgrade(cfg, record, revision(cfg), source_revision=SOURCE_REVISION)
    assert not load_config(cfg).projects[0].agent.context_enabled
    assert json.loads(record.read_text(encoding="utf-8"))["project_agents"]["retained"]["agent"]["context_enabled"] is True


def test_module_cli_is_a_real_conversion_entrypoint(sample):
    cfg, record, _ = sample
    command = [sys.executable, "-B", "-m", "local_agents.config_migration", "downgrade",
               "--config", str(cfg), "--record", str(record), "--revision", revision(cfg),
               "--source-revision", SOURCE_REVISION]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["public_process_stopped"] is False
    assert old_parser(cfg).returncode == 0
    command[4] = "upgrade"
    command[command.index("--revision") + 1] = revision(cfg)
    restored = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
    assert restored.returncode == 0, restored.stderr
    assert load_config(cfg).agent["profiles"]["cheap_reader"]["model"] == "synthetic-selected-model"


def test_concurrent_migrations_accept_only_one_config_revision(sample):
    cfg, _, _ = sample
    expected = revision(cfg)
    def attempt(number):
        record = cfg.parent / f"parallel-{number}.json"
        try:
            migration().downgrade(cfg, record, expected, source_revision=SOURCE_REVISION)
            return "accepted"
        except migration().MigrationError as error:
            assert "REVISION_CONFLICT" in str(error)
            assert not record.exists()
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(attempt, (1, 2)))
    assert sorted(results) == ["accepted", "conflict"]


def test_upgrade_does_not_overwrite_already_upgraded_settings(sample):
    cfg, record, _ = sample
    export(sample)
    migration().upgrade(cfg, record, revision(cfg), source_revision=SOURCE_REVISION)
    before = cfg.read_bytes()
    with pytest.raises(migration().MigrationError, match="ALREADY_UPGRADED"):
        migration().upgrade(cfg, record, revision(cfg), source_revision=SOURCE_REVISION)
    assert cfg.read_bytes() == before


def test_auth_json_cannot_be_used_as_a_migration_record(sample, monkeypatch):
    cfg, record, _ = sample
    export(sample)
    protected = cfg.parent / "auth.json"
    original = Path.read_text
    def guarded_read(path, *args, **kwargs):
        if path == protected:
            pytest.fail("migration must not read auth.json")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", guarded_read)
    with pytest.raises(migration().MigrationError, match="INVALID_RECORD_PATH"):
        migration().upgrade(cfg, protected, revision(cfg), source_revision=SOURCE_REVISION)
