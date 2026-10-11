"""Explicit offline C0 config conversion; no service, tunnel or credential control.

The caller must stop its owned candidate public tunnel before downgrade. Writing
tunnel.mode=off does not stop an existing tunnel process. Never point this CLI at
the live configuration without a separately authorized cutover window.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import tomllib
from pathlib import Path

from config import (ConfigError, ConfigStore, _atomic_write, _file_lock,
                    config_from_data, config_to_dict, dump_toml)
from local_agents.contracts import validation_errors

C0_REVISION = "51b30e42be82290d9f6ab3b44616063bd4c708ff"
_RECORD_KEYS = {"record_version", "state", "source_software_revision", "target_software_revision",
                "config_contract_version", "source_revision", "converted_revision", "restored_revision",
                "agent", "project_agents", "prior_tunnel", "config_path"}


class MigrationError(ConfigError):
    """Safe migration error; no original values or secrets in the message."""


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_source(store: ConfigStore, expected_revision: str) -> tuple[bytes, dict]:
    if store.pending_path.exists():
        raise MigrationError("CONFIG_TRANSACTION_PENDING: resolve the existing config transaction first")
    raw = store.path.read_bytes()
    if _digest(raw) != expected_revision:
        raise MigrationError("REVISION_CONFLICT: configuration changed; reread before conversion")
    try:
        data = tomllib.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, tomllib.TOMLDecodeError):
        raise MigrationError("INVALID_CONFIG: source TOML is invalid; original bytes retained") from None
    if any(key in data for key in ("auth", "authentication", "mcp_oauth")):
        raise MigrationError("AUTH_SCHEMA_UNSUPPORTED: authentication migration schema is not frozen; original bytes retained")
    try:
        config_from_data(data, store.path)
    except ConfigError:
        raise MigrationError("INVALID_CONFIG: source settings fail current schema; original bytes retained") from None
    return raw, data


def _paths(config_path, record_path, source_revision: str) -> tuple[ConfigStore, Path]:
    if not isinstance(source_revision, str) or not re.fullmatch(r"[a-f0-9]{40}", source_revision):
        raise MigrationError("INVALID_SOFTWARE_REVISION: supply the actual source commit separately from config revision")
    store = ConfigStore(config_path)
    record = Path(record_path).resolve()
    if (record.suffix.lower() != ".json" or record.name.casefold() in {"auth.json", "credentials.json"}
            or record in (
            store.path, store.last_good_path, store.lock_path, store.pending_path, store.rejected_path)):
        raise MigrationError("INVALID_RECORD_PATH: migration record must be a separate JSON file")
    return store, record


def _commit(store: ConfigStore, record_path: Path, config_bytes: bytes, record: dict) -> None:
    """Reuse atomic writes; roll back this transaction's three files on failure."""
    paths = (record_path, store.last_good_path, store.path)
    before = {path: path.read_bytes() if path.exists() else None for path in paths}
    values = (json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
              config_bytes, config_bytes)
    try:
        for path, value in zip(paths, values):
            _atomic_write(path, value)
    except OSError:
        recovered = True
        for path in reversed(paths):
            old = before[path]
            try:
                current = path.read_bytes() if path.exists() else None
                if current == old:
                    continue
                if old is None:
                    path.unlink(missing_ok=True)
                else:
                    _atomic_write(path, old)
            except OSError:
                recovered = False
        message = "STORAGE_ERROR: conversion failed; original files restored" if recovered else "RECOVERY_REQUIRED: conversion failed; use the caller's verified original-byte backup"
        raise MigrationError(message) from None


def downgrade(config_path, record_path, expected_revision: str, *, source_revision: str) -> dict:
    """Export only C0-supported settings; independently preserve nonsecret new prefs.

    No old whole-config snapshot is restored. Authentication extensions with no
    frozen migration schema are refused, not guessed or silently stripped.
    """
    store, record_path = _paths(config_path, record_path, source_revision)
    with _file_lock(store.lock_path):
        raw, data = _read_source(store, expected_revision)
        if record_path.exists():
            raise MigrationError("RECORD_CONFLICT: existing migration record must not be overwritten")
        normalized = config_to_dict(config_from_data(data, store.path))
        agent = normalized.pop("agent")
        project_agents = {p["id"]: {"path": p["path"], "agent": p.pop("agent")} for p in normalized["projects"]}
        prior_tunnel = copy.deepcopy(normalized["tunnel"])
        normalized["tunnel"] = {"mode": "off", "public_url": ""}
        converted = dump_toml(normalized).encode("utf-8")
        record = {"record_version": "1.0.0", "state": "downgraded",
                  "config_path": str(store.path),
                  "source_software_revision": source_revision, "target_software_revision": C0_REVISION,
                  "config_contract_version": "2.0.0", "source_revision": _digest(raw),
                  "converted_revision": _digest(converted), "restored_revision": None,
                  "agent": agent, "project_agents": project_agents, "prior_tunnel": prior_tunnel}
        _commit(store, record_path, converted, record)
        return {"revision": _digest(converted), "record": str(record_path), "target_software_revision": C0_REVISION,
                "public_tunnel_configuration": "off", "public_process_stopped": False}


def _read_record(path: Path) -> dict:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise MigrationError("INVALID_RECORD: restore the verified nonsecret migration record") from None
    if not isinstance(record, dict) or set(record) != _RECORD_KEYS:
        raise MigrationError("INVALID_RECORD: unknown or missing migration record fields")
    if (record["record_version"] != "1.0.0" or record["state"] != "downgraded"
            or record["target_software_revision"] != C0_REVISION or record["config_contract_version"] != "2.0.0"):
        raise MigrationError("RECORD_UNSUPPORTED: migration version/target/state is unsupported")
    if validation_errors("agent_config", record["agent"]):
        raise MigrationError("INVALID_RECORD: preserved agent configuration fails the frozen schema")
    if not isinstance(record["project_agents"], dict):
        raise MigrationError("INVALID_RECORD: project policies must be a mapping")
    for value in record["project_agents"].values():
        if (not isinstance(value, dict) or set(value) != {"path", "agent"}
                or not isinstance(value["path"], str) or validation_errors("project_agent", value["agent"])):
            raise MigrationError("INVALID_RECORD: preserved project policy fails schema")
    if (not isinstance(record["config_path"], str)
            or not re.fullmatch(r"[a-f0-9]{40}", str(record["source_software_revision"]))
            or any(not re.fullmatch(r"[a-f0-9]{64}", str(record[key])) for key in ("source_revision", "converted_revision"))
            or record["restored_revision"] is not None):
        raise MigrationError("INVALID_RECORD: source and configuration revision metadata is malformed")
    try:
        config_from_data({"projects": [], "tunnel": record["prior_tunnel"]})
    except ConfigError:
        raise MigrationError("INVALID_RECORD: preserved tunnel metadata fails the nonsecret schema") from None
    return record


def upgrade(config_path, record_path, expected_revision: str, *, source_revision: str) -> dict:
    """Merge preferences into latest C0 settings; missing project IDs stay deleted."""
    store, record_path = _paths(config_path, record_path, source_revision)
    with _file_lock(store.lock_path):
        _, data = _read_source(store, expected_revision)
        if "agent" in data or any("agent" in p for p in data["projects"]):
            raise MigrationError("ALREADY_UPGRADED: current new settings must not be overwritten")
        record = _read_record(record_path)
        if Path(record["config_path"]).resolve() != store.path:
            raise MigrationError("RECORD_BINDING_CONFLICT: migration record belongs to another configuration path")
        data["agent"] = copy.deepcopy(record["agent"])
        for project in data["projects"]:
            previous = record["project_agents"].get(project["id"])
            if previous is not None:
                project["agent"] = copy.deepcopy(previous["agent"])
                if Path(previous["path"]).resolve() != Path(project["path"]).resolve():
                    # Preferences remain in the record; a changed root needs fresh opt-in.
                    project["agent"]["context_enabled"] = False
        data["tunnel"] = {"mode": "off", "public_url": ""}
        try:
            normalized = config_to_dict(config_from_data(data, store.path))
        except ConfigError:
            raise MigrationError("MERGE_CONFLICT: latest old settings and preserved preferences conflict; originals retained") from None
        converted = dump_toml(normalized).encode("utf-8")
        record = copy.deepcopy(record)
        record["state"] = "upgraded"
        record["restored_revision"] = _digest(converted)
        _commit(store, record_path, converted, record)
        return {"revision": _digest(converted), "record": str(record_path), "source_software_revision": source_revision,
                "public_tunnel_configuration": "off", "public_process_stopped": False}


def main() -> int:
    parser = argparse.ArgumentParser(description="Explicit nonsecret config conversion; stop candidate public tunnel first")
    parser.add_argument("operation", choices=("downgrade", "upgrade"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--record", type=Path, required=True)
    parser.add_argument("--revision", required=True, help="Expected disk-byte SHA256 for CAS")
    parser.add_argument("--source-revision", required=True, help="Actual source software commit, separate from disk revision")
    args = parser.parse_args()
    try:
        operation = downgrade if args.operation == "downgrade" else upgrade
        result = operation(args.config, args.record, args.revision, source_revision=args.source_revision)
    except MigrationError as error:
        print(str(error))
        return 1
    except OSError:
        print("STORAGE_ERROR: migration I/O failed; use the verified original-byte backup")
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
