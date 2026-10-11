import json
import os
import subprocess
import pytest
from datetime import datetime

import setup
from config import ServerConfig, dump_toml


def test_setup_freezes_packaged_build_identity_once_and_keeps_legacy_version_shape(tmp_path, monkeypatch):
    from projects import Project

    source = tmp_path / "archive"
    source.mkdir()
    manifest = {"product_version": "1.2.0", "source_commit": "a" * 40,
                "build_id": "build-rc-fixture", "built_at_utc": "2026-10-10T00:00:00Z",
                "worktree_dirty": False}
    (source / "build-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(setup, "BASE_DIR", source)
    calls = 0
    real_reader = setup.read_build_manifest

    def counted_reader(root):
        nonlocal calls
        calls += 1
        return real_reader(root)

    monkeypatch.setattr(setup, "read_build_manifest", counted_reader)
    project = tmp_path / "project"
    project.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(dump_toml({"projects": [{"id": "sample", "name": "Sample", "path": str(project)}],
        "server": {"host": "127.0.0.1", "port": 8768, "path": "/mcp"},
        "tunnel": {"mode": "off", "public_url": ""}}), encoding="utf-8")

    app = setup.SetupApplication(config, tmp_path / "state", setup_port=8769)
    first = app.status()
    (source / "build-manifest.json").write_text("{}", encoding="utf-8")
    second = app.status()

    assert calls == 1
    assert first["version"] == {"release": "1.2.0", "source_commit": "a" * 40,
                                 "worktree_dirty": False, "build_id": "build-rc-fixture",
                                 "built_at_utc": "2026-10-10T00:00:00Z"}
    assert second["version"] == first["version"]
    assert first["setup"]["pid"] == os.getpid()
    assert first["setup"]["build_id"] == "build-rc-fixture"
    assert first["setup"]["manifest_state"] == "valid"
    assert first["setup"]["effective_config_version"] is None
    datetime.fromisoformat(first["setup"]["started_at"].replace("Z", "+00:00"))
    assert first["mcp"]["pid"] is None


def test_setup_does_not_claim_git_head_when_running_without_build_manifest(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(dump_toml({"projects": [{"id": "sample", "name": "Sample", "path": str(project)}],
        "server": {"host": "127.0.0.1", "port": 8770, "path": "/mcp"},
        "tunnel": {"mode": "off", "public_url": ""}}), encoding="utf-8")
    monkeypatch.setattr(setup, "BASE_DIR", tmp_path / "no-git-copy")
    original_run = subprocess.run
    git_calls = []

    def track_git(args, *positional, **kwargs):
        if args and str(args[0]).casefold() == "git":
            git_calls.append(args)
            pytest.fail("status must not query a mutable Git checkout")
        return original_run(args, *positional, **kwargs)

    monkeypatch.setattr(subprocess, "run", track_git)

    app = setup.SetupApplication(config, tmp_path / "state", setup_port=8771)
    status = app.status()
    app.status()

    assert status["version"]["source_commit"] is None
    assert status["version"]["worktree_dirty"] is None
    assert status["version"]["build_id"] is None
    assert git_calls == []
    app.runtime.stop()


def test_setup_does_not_report_effective_config_when_mcp_is_stopped(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(dump_toml({"projects": [{"id": "sample", "name": "Sample", "path": str(project)}],
        "server": {"host": "127.0.0.1", "port": 8772, "path": "/mcp"},
        "tunnel": {"mode": "off", "public_url": ""}}), encoding="utf-8")
    monkeypatch.setattr(setup, "BASE_DIR", tmp_path / "no-git-copy")
    app = setup.SetupApplication(config, tmp_path / "state", setup_port=8773)
    try:
        app.runtime.start()
        assert app.status()["setup"]["effective_config_version"]
        app.runtime.stop()
        status = app.status()
        assert status["mcp"]["pid"] is None
        assert status["mcp"]["effective_config_version"] is None
        assert status["setup"]["effective_config_version"] is None
    finally:
        app.runtime.stop()
