"""Optional agent settings through the real config/store/reload public seams."""
from copy import deepcopy
import hashlib
import subprocess
import tomllib

import pytest

from config import (ConfigError, ConfigStore, config_from_data, config_revision,
                    config_to_dict, dump_toml, ensure_config, load_config, validate_config)


def data(tmp_path, agent=None, project_agent=None):
    value = {"projects": [{"id": "p", "name": "Project", "path": str(tmp_path)}]}
    if agent is not None:
        value["agent"] = agent
    if project_agent is not None:
        value["projects"][0]["agent"] = project_agent
    return value


def enabled(model="manual-model", auth_mode="chatgpt"):
    return {"enabled": True, "codex": {"enabled": True, "auth_mode": auth_mode},
            "profiles": {"cheap_reader": {"model": model}}}


def test_legacy_config_defaults_off_and_preserves_old_project_permissions(tmp_path):
    value = data(tmp_path)
    value["projects"][0]["write_enabled"] = True
    parsed = config_from_data(value)
    projected = config_to_dict(parsed)
    assert projected["agent"]["enabled"] is False
    assert projected["agent"]["codex"]["enabled"] is False
    assert projected["agent"]["codex"]["auth_mode"] == "unselected"
    assert parsed.projects[0].write_enabled is True
    assert parsed.projects[0].agent.context_enabled is False
    assert parsed.projects[0].agent.allowed_profiles == ("cheap_reader",)


@pytest.mark.parametrize("auth_mode", ["chatgpt", "api_key"])
def test_explicit_model_identity_round_trips_without_backend_probe(tmp_path, monkeypatch, auth_mode):
    def forbidden(*args, **kwargs):
        pytest.fail("configuration must not start CLI or probe availability")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    parsed = config_from_data(data(tmp_path, enabled(auth_mode=auth_mode), {"context_enabled": True}))
    normalized = config_to_dict(parsed)
    assert normalized["agent"]["profiles"]["cheap_reader"]["model"] == "manual-model"
    assert normalized["agent"]["profiles"]["cheap_reader"]["service_tier"] == "standard"
    assert normalized["projects"][0]["agent"]["context_enabled"] is True
    assert config_from_data(tomllib.loads(dump_toml(normalized))) == parsed
    assert load_config(write_config(tmp_path / "isolated.toml", normalized)) == parsed


def write_config(path, value):
    path.write_text(dump_toml(value), encoding="utf-8")
    return path


@pytest.mark.parametrize("draft", [{}, {"profiles": {"cheap_reader": {"model": ""}}},
                                    {"codex": {"auth_mode": "api_key"}, "profiles": {"cheap_reader": {"model": "saved-model"}}}])
def test_disabled_incomplete_draft_is_savable(tmp_path, draft):
    parsed = config_from_data(data(tmp_path, draft))
    assert config_to_dict(parsed)["agent"]["enabled"] is False


@pytest.mark.parametrize("fragment", [
    {"enabled": True}, enabled(model=""), enabled(model="   "), enabled(auth_mode="unselected"),
    {"profiles": {"cheap_reader": {"backend": "claude"}}},
    {"profiles": {"cheap_reader": {"mode": "develop"}}},
    {"profiles": {"cheap_reader": {"timeout_seconds": True}}},
    {"profiles": {"cheap_reader": {"timeout_seconds": 59}}},
    {"codex": {"auth_mode": "disabled"}}, {"codex": {"api_key": "synthetic-secret"}},
    {"configured": True}, {"limits": {"max_running": True}},
    {"codex": {"api_base_url": "http://api.example/v1", "api_provider": "custom_responses"}},
    {"default_profile": "missing"},
])
def test_invalid_agent_is_rejected_without_secret_echo(tmp_path, fragment):
    problems = validate_config(data(tmp_path, fragment))
    assert problems and "synthetic-secret" not in repr(problems)
    with pytest.raises(ConfigError):
        config_from_data(data(tmp_path, fragment))


def test_explicit_null_agent_and_project_agent_are_invalid(tmp_path):
    value = data(tmp_path)
    value["agent"] = None
    assert validate_config(value)
    value = data(tmp_path)
    value["projects"][0]["agent"] = None
    assert validate_config(value)


def test_project_profile_references_must_exist(tmp_path):
    assert validate_config(data(tmp_path, project_agent={"allowed_profiles": ["missing"]}))


def test_config_projection_and_input_do_not_mutate_existing_revision(tmp_path):
    value = data(tmp_path, enabled())
    parsed = config_from_data(value)
    revision = config_revision(parsed)
    value["agent"]["profiles"]["cheap_reader"]["model"] = "input-mutated"
    projected = config_to_dict(parsed)
    projected["agent"]["profiles"]["cheap_reader"]["model"] = "output-mutated"
    assert config_revision(parsed) == revision
    assert config_revision(config_from_data(data(tmp_path, enabled("new-model")))) != revision


def test_store_invalid_agent_update_keeps_actual_disk_bytes(tmp_path):
    store = ConfigStore(ensure_config(tmp_path / "config.toml"))
    state = store.read()
    before = store.path.read_bytes()
    broken = deepcopy(state["data"])
    broken["agent"] = enabled(model="")
    with pytest.raises(ConfigError):
        store.save(broken, state["revision"], lambda _: pytest.fail("invalid config must not apply"))
    assert store.path.read_bytes() == before


def test_store_saved_agent_reaches_callback_and_distinct_revisions(tmp_path):
    store = ConfigStore(ensure_config(tmp_path / "config.toml"))
    old = store.read()
    proposed = data(tmp_path, enabled(), {"context_enabled": True})
    applied = []
    def apply(candidate):
        assert load_config(store.path) == candidate
        applied.append(candidate)
        return {"verified": True, "revision": config_revision(candidate)}
    result = store.save(proposed, old["revision"], apply)
    assert applied[0].agent["profiles"]["cheap_reader"]["model"] == "manual-model"
    assert applied[0].projects[0].agent.context_enabled
    assert result["revision"] == hashlib.sha256(store.path.read_bytes()).hexdigest()
    assert result["applied"]["revision"] == config_revision(load_config(store.path))
    assert result["revision"] != result["applied"]["revision"]


def test_old_client_save_preserves_agent_for_retained_project_and_deletes_removed(tmp_path):
    store = ConfigStore(ensure_config(tmp_path / "config.toml"))
    state = store.read()
    first = data(tmp_path, enabled(), {"context_enabled": True})
    first["projects"].append({"id": "removed", "name": "Removed", "path": str(tmp_path)})
    state = store.save(first, state["revision"], lambda _: True)
    legacy = data(tmp_path)
    legacy["projects"][0]["name"] = "Renamed by old client"
    store.save(legacy, state["revision"], lambda _: True)
    current = load_config(store.path)
    assert current.agent["enabled"] is True
    assert current.projects[0].agent.context_enabled is True
    assert [p.id for p in current.projects] == ["p"]
    assert current.projects[0].name == "Renamed by old client"


def test_explicit_disable_is_not_overwritten_by_preservation(tmp_path):
    store = ConfigStore(ensure_config(tmp_path / "config.toml"))
    old = store.read()
    saved = store.save(data(tmp_path, enabled(), {"context_enabled": True}), old["revision"], lambda _: True)
    proposed = deepcopy(saved["data"])
    proposed["agent"]["enabled"] = False
    proposed["agent"]["codex"]["enabled"] = False
    proposed["projects"][0]["agent"]["context_enabled"] = False
    store.save(proposed, saved["revision"], lambda _: True)
    current = load_config(store.path)
    assert current.agent["enabled"] is False and not current.projects[0].agent.context_enabled
    assert current.agent["profiles"]["cheap_reader"]["model"] == "manual-model"


@pytest.mark.parametrize("projects", [None, [{"id": [], "name": "Bad", "path": "bad"}]])
def test_malformed_legacy_save_returns_config_error_before_disk_update(tmp_path, projects):
    store = ConfigStore(ensure_config(tmp_path / "config.toml"))
    old = store.read()
    before = store.path.read_bytes()
    with pytest.raises(ConfigError):
        store.save({"projects": projects}, old["revision"], lambda _: pytest.fail("invalid config must not apply"))
    assert store.path.read_bytes() == before


def test_named_profiles_and_allowlist_round_trip_without_restricting_project_id(tmp_path):
    agent = enabled()
    agent["profiles"]["阅读档案"] = {"model": "manual-secondary", "reasoning_effort": "low", "timeout_seconds": 300}
    agent["default_profile"] = "阅读档案"
    proposed = data(tmp_path, agent, {"context_enabled": True, "allowed_profiles": ["阅读档案"]})
    proposed["projects"][0]["id"] = "项目 / existing:id"
    parsed = config_from_data(proposed)
    assert parsed.registry["项目 / existing:id"].agent.allowed_profiles == ("阅读档案",)
    normalized = config_to_dict(parsed)
    assert config_from_data(tomllib.loads(dump_toml(normalized))) == parsed


def test_real_reload_replaces_agent_settings_and_keeps_old_snapshot(tmp_path, monkeypatch):
    import server
    import tools
    path = write_config(tmp_path / "config.toml", data(tmp_path, enabled("old-model"), {"context_enabled": True}))
    previous = load_config(path)
    previous_revision = config_revision(previous)
    cell = [previous]
    monkeypatch.setitem(tools._holder, "config", previous)
    next_data = data(tmp_path, enabled("new-model"), {"context_enabled": False})
    write_config(path, next_data)
    assert server.reload_config(cell, path) is True
    assert tools._holder["config"] is cell[0]
    assert cell[0].agent["profiles"]["cheap_reader"]["model"] == "new-model"
    assert not cell[0].projects[0].agent.context_enabled
    assert previous.agent["profiles"]["cheap_reader"]["model"] == "old-model"
    assert config_revision(previous) == previous_revision
    assert config_revision(cell[0]) != previous_revision
    write_config(path, data(tmp_path, enabled("")))
    accepted = cell[0]
    assert server.reload_config(cell, path) is False
    assert cell[0] is accepted
