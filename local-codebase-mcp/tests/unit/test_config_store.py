"""Canonical TOML migration and public ConfigStore transaction behavior."""
from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest

import config as cfg_mod
from config import (
    ConfigApplyError, ConfigConflictError, ConfigError, ConfigStore,
    config_from_data, config_revision, config_to_dict, dump_toml,
    ensure_config, load_config, read_config_data, validate_config,
)
from conftest import PKG_DIR
from tools_services import list_projects


def project(root, pid="alpha", **extra):
    return {"id": pid, "name": pid, "path": str(root), **extra}


@pytest.fixture
def store(tmp_path):
    return ConfigStore(ensure_config(tmp_path / "config.toml"))


def test_first_install_has_empty_projects_loopback_8168_and_off_tunnel(store):
    state = store.read()
    config = load_config(store.path)
    assert list_projects(config)["projects"] == []
    assert config.server.port == 8168 and config.server.host == "127.0.0.1"
    assert config.tunnel.mode == "off"
    assert state["revision"] == hashlib.sha256(store.path.read_bytes()).hexdigest()
    assert store.last_good_path.read_bytes() == store.path.read_bytes()


def test_yaml_migration_preserves_order_permissions_and_only_runs_once(tmp_path):
    import yaml
    legacy = {"projects": [project(tmp_path, "中文", write_enabled=True, write_roots=["docs", "wiki"]),
                           project(tmp_path, "beta")],
              "server": {"host": "127.0.0.1", "port": 8123, "path": "/mcp"}}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(legacy, allow_unicode=True), encoding="utf-8")
    (tmp_path / "config.runtime.yaml").write_text(yaml.safe_dump({"projects": [], "server": {"port": 8999}}), encoding="utf-8")
    canonical = ensure_config(tmp_path / "config.toml")
    config = load_config(canonical)
    assert [p.id for p in config.projects] == ["中文", "beta"]
    assert config.projects[0].write_enabled and config.projects[0].write_roots == ("docs", "wiki")
    assert config.server.port == 8123
    original = canonical.read_bytes()
    (tmp_path / "config.yaml").write_text("projects: []", encoding="utf-8")
    ensure_config(canonical)
    assert canonical.read_bytes() == original


def test_invalid_legacy_does_not_create_partial_toml(tmp_path):
    (tmp_path / "config.yaml").write_text("projects: [{id: alpha}]", encoding="utf-8")
    with pytest.raises(ConfigError):
        ensure_config(tmp_path / "config.toml")
    assert not (tmp_path / "config.toml").exists()


def test_toml_round_trip_windows_escapes_unicode_and_intel(tmp_path):
    data = {
        "projects": [project(tmp_path, "中文 \"quoted\"", write_enabled=True, write_roots=["docs/sub", "wiki"])],
        "server": {"host": "::1", "port": 8123, "path": "/mcp"},
        "intel": {"servers": {"python": {"command": [r"C:\Program Files\LSP\python.exe", "--stdio"]}}},
        "tunnel": {"mode": "cloudflare", "public_url": ""},
    }
    assert tomllib.loads(dump_toml(data)) == data
    parsed = config_from_data(data)
    assert config_from_data(config_to_dict(parsed)) == parsed
    reordered = dict(reversed(list(config_to_dict(parsed).items())))
    assert config_revision(config_from_data(reordered)) == config_revision(parsed)


@pytest.mark.parametrize("fragment,field", [
    ({"unexpected": True}, "unexpected"),
    ({"projects": [{"id": "x", "name": "x", "path": ".", "write_enabled": "false"}]}, "write_enabled"),
    ({"server": {"host": ["127.0.0.1"]}}, "host"),
    ({"server": {"port": True}}, "port"),
    ({"server": {"port": "8168"}}, "port"),
    ({"server": {"path": "/mcp?admin=1"}}, "path"),
    ({"server": {"unexpected": True}}, "unexpected"),
    ({"limits": {"max_chars": True}}, "max_chars"),
    ({"limits": {"max_chars": 0}}, "max_chars"),
    ({"security": {"sensitive_patterns": "*.key"}}, "sensitive_patterns"),
    ({"security": {"sensitive_patterns": [1]}}, "sensitive_patterns"),
    ({"intel": {"extra": True}}, "extra"),
    ({"intel": {"servers": {"python": {"command": ["python"], "cwd": "/"}}}}, "cwd"),
    ({"intel": {"servers": {"python": {"command": [False]}}}}, "command"),
    ({"tunnel": {"mode": "unknown"}}, "mode"),
    ({"tunnel": {"token": "secret"}}, "token"),
    ({"tunnel": {"public_url": "https://user:secret@example.com"}}, "public_url"),
    ({"tunnel": {"public_url": "https://example.com/?token=secret"}}, "public_url"),
    ({"tunnel": {"public_url": "http://example.com"}}, "public_url"),
])
def test_strict_schema_fails_closed(fragment, field):
    problems = validate_config({"projects": [], **fragment})
    assert any(field in problem for problem in problems)
    with pytest.raises(ConfigError):
        config_from_data({"projects": [], **fragment})


@pytest.mark.parametrize("section", ["server", "limits", "security", "intel", "tunnel"])
def test_explicit_null_section_is_invalid(section):
    assert validate_config({"projects": [], section: None})


def test_save_changes_toml_then_applies_real_registry_and_verifies(store, tmp_path):
    current = store.read()
    proposed = copy.deepcopy(current["data"])
    proposed["projects"] = [project(tmp_path, "new-project", write_enabled=True, write_roots=["docs", "wiki"])]
    running = [load_config(store.path)]
    def apply(candidate):
        # The callback observes the durable candidate file before accepting it.
        assert load_config(store.path) == candidate
        running[0] = load_config(store.path)
        observed = list_projects(running[0])["projects"]
        assert [p["id"] for p in observed] == [p.id for p in candidate.projects]
        return {"verified": True, "projects": [p["id"] for p in observed]}
    result = store.save(proposed, current["revision"], apply)
    assert result["applied"]["projects"] == ["new-project"]
    assert list_projects(running[0])["projects"][0]["id"] == "new-project"
    assert read_config_data(store.path)["projects"] == proposed["projects"]
    assert result["revision"] != current["revision"]
    assert store.last_good_path.read_bytes() == store.path.read_bytes()
    assert not store.pending_path.exists()


def test_stale_revision_cannot_overwrite_or_call_backend(store):
    old = store.read()
    data = copy.deepcopy(old["data"])
    data["server"]["port"] += 1
    store.save(data, old["revision"], lambda config: True)
    latest = store.path.read_bytes()
    with pytest.raises(ConfigConflictError):
        store.save(old["data"], old["revision"], lambda config: pytest.fail("must not apply stale config"))
    assert store.path.read_bytes() == latest


def test_invalid_candidate_never_reaches_disk_or_callback(store):
    old = store.read()
    broken = copy.deepcopy(old["data"])
    broken["server"]["host"] = "0.0.0.0"
    with pytest.raises(ConfigError):
        store.save(broken, old["revision"], lambda config: pytest.fail("must not apply invalid config"))
    assert store.read() == old


@pytest.mark.parametrize("failed_result", [False, None, {}, {"ok": True}, {"verified": False}, {"verified": True, "ok": False}, {"verified": True, "applied": False}])
def test_backend_failure_restores_disk_and_running_last_good(store, failed_result):
    old = store.read()
    proposed = copy.deepcopy(old["data"])
    proposed["server"]["port"] += 1
    running = [load_config(store.path)]
    calls = []
    def apply(candidate):
        running[0] = candidate
        calls.append(candidate.server.port)
        return failed_result if len(calls) == 1 else {"verified": True}
    with pytest.raises(ConfigApplyError, match="未生效"):
        store.save(proposed, old["revision"], apply)
    assert calls == [8169, 8168] and running[0].server.port == 8168
    assert store.read() == old
    assert store.last_good_path.read_bytes() == store.path.read_bytes()


def test_exception_during_apply_restores_last_good(store):
    old = store.read()
    proposed = copy.deepcopy(old["data"])
    proposed["server"]["port"] = 9001
    calls = []
    def apply(candidate):
        calls.append(candidate.server.port)
        if candidate.server.port == 9001:
            raise RuntimeError("端口占用。请选择其他端口。")
        return True
    with pytest.raises(ConfigApplyError, match="端口占用"):
        store.save(proposed, old["revision"], apply)
    assert store.read() == old and calls == [9001, 8168]


def test_atomic_replace_failure_preserves_original(store, monkeypatch):
    old = store.read()
    original_replace = cfg_mod.os.replace
    def reject_candidate(source, target):
        if Path(target) == store.path:
            raise PermissionError("read-only destination")
        return original_replace(source, target)
    monkeypatch.setattr(cfg_mod.os, "replace", reject_candidate)
    with pytest.raises(ConfigApplyError):
        store.save(old["data"], old["revision"], lambda config: True)
    assert store.path.read_bytes() == store.last_good_path.read_bytes()
    assert not list(store.path.parent.glob("*.tmp"))


def test_invalid_external_edit_is_preserved_and_durable_last_good_restored(store):
    old = store.read()
    invalid = b'projects = ['
    store.path.write_bytes(invalid)
    result = ConfigStore(store.path).read()
    assert result["recovered"] is True and "已恢复" in result["warning"]
    assert result["revision"] == old["revision"] and result["data"] == old["data"]
    assert store.rejected_path.read_bytes() == invalid
    assert load_config(store.path).server.port == 8168


def test_interrupted_transaction_recovers_last_good_on_next_start(store):
    old = store.read()
    proposed = copy.deepcopy(old["data"])
    proposed["server"]["port"] = 9001
    store.pending_path.write_text(old["revision"], encoding="ascii")
    store.path.write_text(dump_toml(proposed), encoding="utf-8")
    ensure_config(store.path)
    assert store.read() == old
    assert not store.pending_path.exists()


def test_two_processes_serialize_save_and_conflict_instead_of_lost_update(store, tmp_path):
    old = store.read()
    signal = tmp_path / "in-callback"
    script = """
import sys, time
from pathlib import Path
from config import ConfigStore, ConfigConflictError
store = ConfigStore(sys.argv[1])
data = store.read()['data']
data['server']['port'] = int(sys.argv[3])
def apply(config):
    if sys.argv[4]:
        Path(sys.argv[4]).write_text('ready', encoding='utf-8')
        time.sleep(0.7)
    return True
try:
    store.save(data, sys.argv[2], apply)
    print('saved')
except ConfigConflictError:
    print('conflict')
"""
    first = subprocess.Popen(
        [sys.executable, "-c", script, str(store.path), old["revision"], "9001", str(signal)],
        cwd=PKG_DIR, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not signal.exists() and time.monotonic() < deadline and first.poll() is None:
            time.sleep(0.05)
        assert signal.exists()
        second = subprocess.run(
            [sys.executable, "-c", script, str(store.path), old["revision"], "9002", ""],
            cwd=PKG_DIR, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15,
        )
        stdout, stderr = first.communicate(timeout=15)
        assert first.returncode == 0 and stdout.strip() == "saved", stderr
        assert second.returncode == 0 and second.stdout.strip() == "conflict", second.stderr
        assert load_config(store.path).server.port == 9001
    finally:
        if first.poll() is None:
            first.terminate()
            first.wait(timeout=5)



def test_existing_valid_toml_initializes_last_good_without_rewriting(tmp_path):
    path = tmp_path / "config.toml"
    content = b'projects = []\n[server]\nport = 9123\n'
    path.write_bytes(content)
    ensure_config(path)
    store = ConfigStore(path)
    assert path.read_bytes() == content
    assert store.last_good_path.read_bytes() == content


def test_missing_canonical_is_restored_from_persisted_last_good(store):
    old = store.read()
    store.path.unlink()
    ensure_config(store.path)
    assert store.read() == old


def test_external_edit_during_backend_verify_is_preserved_before_rollback(store):
    old = store.read()
    proposed = copy.deepcopy(old["data"])
    proposed["server"]["port"] = 9001
    outside = b'projects = []\n[server]\nport = 9002\n'
    calls = []
    def apply(candidate):
        calls.append(candidate.server.port)
        if candidate.server.port == 9001:
            store.path.write_bytes(outside)
        return {"verified": True}
    with pytest.raises(ConfigApplyError, match="外部程序"):
        store.save(proposed, old["revision"], apply)
    assert store.rejected_path.read_bytes() == outside
    assert store.read() == old
    assert calls == [9001, 8168]


def test_verified_manual_edit_updates_persistent_recovery_point(store):
    old = store.read()
    proposed = copy.deepcopy(old["data"])
    proposed["server"]["port"] = 9001
    store.path.write_text(dump_toml(proposed), encoding="utf-8")
    running = load_config(store.path)
    assert store.mark_verified(running)
    good = store.path.read_bytes()
    store.path.write_bytes(b'projects = [')
    recovered = store.read()
    assert recovered["data"] == proposed
    assert store.path.read_bytes() == good


def test_mark_verified_rejects_stale_or_pending_configuration(store):
    running = load_config(store.path)
    original = store.last_good_path.read_bytes()
    edited = store.read()["data"]
    edited["server"]["port"] = 9001
    store.path.write_text(dump_toml(edited), encoding="utf-8")
    assert store.mark_verified(running) is False
    store.pending_path.write_text("pending", encoding="ascii")
    assert store.mark_verified(load_config(store.path)) is False
    assert store.last_good_path.read_bytes() == original


def test_mark_verified_does_not_wait_on_ui_transaction_thread_lock(store):
    import threading
    acquired, release = threading.Event(), threading.Event()
    def own_lock():
        with cfg_mod._file_lock(store.lock_path):
            acquired.set()
            release.wait(timeout=5)
    owner = threading.Thread(target=own_lock)
    owner.start()
    try:
        assert acquired.wait(timeout=5)
        started = time.monotonic()
        assert store.mark_verified(load_config(store.path), lock_timeout=0.1) is False
        assert time.monotonic() - started < 0.5
    finally:
        release.set()
        owner.join(timeout=5)
    assert not owner.is_alive()


def test_mark_verified_inside_pending_save_callback_skips_without_deadlock(store):
    old = store.read()
    def apply(candidate):
        assert store.mark_verified(candidate) is False
        return {"verified": True}
    assert store.save(old["data"], old["revision"], apply)["applied"]["verified"]


def test_save_failure_never_promotes_unverified_external_disk_to_last_good(store):
    good = store.path.read_bytes()
    external = copy.deepcopy(store.read()["data"])
    external["server"]["port"] = 9002
    store.path.write_text(dump_toml(external), encoding="utf-8")
    external_bytes = store.path.read_bytes()
    view = store.read()
    proposed = copy.deepcopy(view["data"])
    proposed["server"]["port"] = 9001
    calls = []
    def apply(candidate):
        calls.append(candidate.server.port)
        assert store.last_good_path.read_bytes() == good
        return {"verified": candidate.server.port == 8168}
    with pytest.raises(ConfigApplyError):
        store.save(proposed, view["revision"], apply)
    assert calls == [9001, 8168]
    assert store.path.read_bytes() == good
    assert store.last_good_path.read_bytes() == good
    assert store.rejected_path.read_bytes() == external_bytes


def test_successful_save_promotes_only_the_verified_candidate(store):
    good = store.last_good_path.read_bytes()
    external = copy.deepcopy(store.read()["data"])
    external["server"]["port"] = 9002
    store.path.write_text(dump_toml(external), encoding="utf-8")
    view = store.read()
    proposed = copy.deepcopy(view["data"])
    proposed["server"]["port"] = 9001
    def apply(candidate):
        assert store.last_good_path.read_bytes() == good
        assert candidate.server.port == 9001
        return {"verified": True}
    store.save(proposed, view["revision"], apply)
    assert store.last_good_path.read_bytes() == store.path.read_bytes()
    assert load_config(store.last_good_path).server.port == 9001


def test_restore_verified_external_failure_is_conditional_and_verified(store):
    previous = load_config(store.path)
    good = store.path.read_bytes()
    external = copy.deepcopy(store.read()["data"])
    external["server"]["port"] = 9001
    store.path.write_text(dump_toml(external), encoding="utf-8")
    bad_bytes = store.path.read_bytes()
    revision = hashlib.sha256(bad_bytes).hexdigest()
    calls = []
    def apply(candidate):
        calls.append(candidate.server.port)
        assert load_config(store.path) == previous
        return {"verified": True}
    result = store.restore_verified(previous, revision, apply)
    assert result["restored"] is True and result["verified"] is True
    assert calls == [8168]
    assert store.path.read_bytes() == store.last_good_path.read_bytes() == good
    assert store.rejected_path.read_bytes() == bad_bytes


def test_restore_verified_does_not_discard_newer_external_edit(store):
    previous = load_config(store.path)
    failed_revision = store.read()["revision"]
    newer = copy.deepcopy(store.read()["data"])
    newer["server"]["port"] = 9002
    store.path.write_text(dump_toml(newer), encoding="utf-8")
    current = store.path.read_bytes()
    result = store.restore_verified(previous, failed_revision, lambda config: pytest.fail("stale recovery must not apply"))
    assert result == {"restored": False, "reason": "revision_changed"}
    assert store.path.read_bytes() == current


def test_restore_verified_skips_pending_save_and_changed_last_good(store):
    previous = load_config(store.path)
    revision = store.read()["revision"]
    store.pending_path.write_text("pending", encoding="ascii")
    assert store.restore_verified(previous, revision, lambda config: pytest.fail("must skip"))["reason"] == "pending"
    store.pending_path.unlink()
    wrong_previous = copy.deepcopy(store.read()["data"])
    wrong_previous["server"]["port"] = 9001
    assert store.restore_verified(config_from_data(wrong_previous), revision, lambda config: pytest.fail("must skip"))["reason"] == "last_good_changed"


def test_restore_verified_backend_failure_keeps_durable_good_toml(store):
    previous = load_config(store.path)
    good = store.path.read_bytes()
    external = copy.deepcopy(store.read()["data"])
    external["server"]["port"] = 9001
    store.path.write_text(dump_toml(external), encoding="utf-8")
    revision = store.read()["revision"]
    with pytest.raises(ConfigApplyError, match="后端恢复未完成"):
        store.restore_verified(previous, revision, lambda config: {"verified": False})
    assert store.path.read_bytes() == store.last_good_path.read_bytes() == good


def test_startup_defaults_disabled_and_is_canonical_toml(store):
    cfg = load_config(store.path)
    assert cfg.startup.enabled is False
    assert config_to_dict(cfg)["startup"] == {"enabled": False}
    assert tomllib.loads(store.path.read_text(encoding="utf-8"))["startup"] == {"enabled": False}


@pytest.mark.parametrize("startup", [True, [], "true", None, {"enabled": "true"}, {"enabled": 1},
                                    {"enabled": None}, {"enabled": True, "command": "whoami"}])
def test_startup_schema_fails_closed_without_config_or_last_good_mutation(store, startup):
    state = store.read()
    before, good = store.path.read_bytes(), store.last_good_path.read_bytes()
    proposed = copy.deepcopy(state["data"])
    proposed["startup"] = startup
    with pytest.raises(ConfigError, match="startup"):
        store.save(proposed, state["revision"], lambda cfg: pytest.fail("invalid startup must never apply"))
    assert store.path.read_bytes() == before and store.last_good_path.read_bytes() == good


def test_startup_enable_round_trip_and_verified_rollback(store):
    initial = store.read()
    previous = load_config(store.path)
    desired = copy.deepcopy(initial["data"])
    desired["startup"] = {"enabled": True}
    seen = []
    def fail_enable(cfg):
        seen.append(cfg.startup.enabled)
        return {"verified": not cfg.startup.enabled}
    with pytest.raises(ConfigApplyError):
        store.save(desired, initial["revision"], fail_enable)
    assert seen == [True, False] and load_config(store.path) == previous
    changed = store.save(desired, initial["revision"], lambda cfg: {"verified": cfg.startup.enabled})
    assert load_config(store.path).startup.enabled is True
    assert changed["revision"] != initial["revision"]
    assert config_revision(load_config(store.path)) != config_revision(previous)
    assert config_from_data(tomllib.loads(dump_toml(config_to_dict(load_config(store.path))))) == load_config(store.path)


def test_explicit_rollback_callback_separates_candidate_validation_from_recovery(store):
    initial = store.read()
    before = store.path.read_bytes()
    desired = copy.deepcopy(initial["data"])
    desired["tunnel"] = {"mode": "cloudflare", "public_url": ""}
    candidate_calls, restore_calls = [], []
    def apply(cfg):
        candidate_calls.append(cfg.tunnel.mode)
        assert load_config(store.path) == cfg
        return {"verified": False}
    def restore(cfg):
        restore_calls.append(cfg.tunnel.mode)
        assert load_config(store.path) == cfg
        return {"verified": True}
    with pytest.raises(ConfigApplyError):
        store.save(desired, initial["revision"], apply, rollback_callback=restore)
    assert candidate_calls == ["cloudflare"] and restore_calls == ["off"]
    assert store.path.read_bytes() == before and store.last_good_path.read_bytes() == before
    assert not store.pending_path.exists()


@pytest.mark.parametrize("callback", [False, True, 0, "restore"])
def test_invalid_explicit_rollback_callback_fails_before_any_mutation(store, callback):
    snapshot = store.read()
    before = store.path.read_bytes()
    with pytest.raises(ConfigError):
        store.save(snapshot["data"], snapshot["revision"], lambda cfg: pytest.fail("invalid recovery must never apply"),
                   rollback_callback=callback)
    assert store.path.read_bytes() == before and store.last_good_path.read_bytes() == before
    assert not store.pending_path.exists()
