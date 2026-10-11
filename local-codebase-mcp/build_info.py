"""Build identity read once by each CodeGPT process; release data is shipped beside the code."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

CORE_RELEASE = "1.2.0"
MANIFEST_NAME = "build-manifest.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_build_manifest(root: Path | str) -> dict:
    """Return a validated immutable-by-convention manifest, or an explicit unknown identity."""
    unknown = {"product_version": CORE_RELEASE, "source_commit": None, "build_id": None,
               "built_at_utc": None, "worktree_dirty": None, "manifest_state": "missing"}
    try:
        value = json.loads((Path(root) / MANIFEST_NAME).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return unknown
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {**unknown, "manifest_state": "invalid"}
    if (not isinstance(value, dict)
            or not isinstance(value.get("product_version"), str) or not value["product_version"].strip()
            or not isinstance(value.get("source_commit"), str)
            or len(value["source_commit"]) != 40
            or any(char not in "0123456789abcdefABCDEF" for char in value["source_commit"])
            or not isinstance(value.get("build_id"), str) or not value["build_id"].strip()
            or not isinstance(value.get("built_at_utc"), str)):
        return {**unknown, "manifest_state": "invalid"}
    built_at = value["built_at_utc"]
    try:
        parsed_built_at = datetime.fromisoformat(built_at[:-1] + "+00:00" if built_at.endswith("Z") else built_at)
    except ValueError:
        return {**unknown, "manifest_state": "invalid"}
    if (parsed_built_at.tzinfo is None or parsed_built_at.utcoffset() != timedelta(0)
            or not (built_at.endswith("Z") or built_at.endswith("+00:00"))):
        return {**unknown, "manifest_state": "invalid"}
    return {"product_version": value["product_version"], "source_commit": value["source_commit"].lower(),
            "build_id": value["build_id"], "built_at_utc": value["built_at_utc"],
            "worktree_dirty": value.get("worktree_dirty") if type(value.get("worktree_dirty")) is bool else None,
            "manifest_state": "valid"}


def compatible_version(manifest: dict) -> dict:
    """Preserve the legacy /api/status.version keys and add the release build ID."""
    return {"release": manifest["product_version"], "source_commit": manifest["source_commit"],
            "worktree_dirty": manifest["worktree_dirty"], "build_id": manifest["build_id"],
            "built_at_utc": manifest["built_at_utc"]}


def process_identity(component: str, manifest: dict, pid: int, started_at: str,
                     effective_config_version: str | None) -> dict:
    return {"component": component, "pid": pid, "started_at": started_at,
            "build_id": manifest["build_id"], "manifest_state": manifest["manifest_state"],
            "effective_config_version": effective_config_version}
