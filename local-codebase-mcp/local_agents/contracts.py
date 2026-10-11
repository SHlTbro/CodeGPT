"""Offline agent contract 2.0.0 validation, with no runtime readiness claims.

Use definition names from contracts/agent/contract-bundle.json. Validation does
not normalize old config, resolve ACLs, access credentials, or invoke Codex.
Errors contain paths and rules only, never user-supplied values.
"""
from __future__ import annotations

import json
import ipaddress
import re
from pathlib import Path
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker, validators

CONTRACT_VERSION = "2.0.0"
CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contracts/agent/contract-bundle.json"
_BUNDLE = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

# Python bool is an int; JSON integers must also reject integral floats at
# this Python API boundary, matching the existing config validator.
_TYPES = Draft202012Validator.TYPE_CHECKER.redefine("integer", lambda _, v: type(v) is int)
_Validator = validators.extend(Draft202012Validator, type_checker=_TYPES)
Draft202012Validator.check_schema(_BUNDLE)
_FORMAT_CHECKER = FormatChecker()
if not {"date-time", "uri"} <= _FORMAT_CHECKER.checkers.keys():
    raise RuntimeError(
        "Agent contract format dependencies missing: install rfc3339-validator==0.1.4 "
        "and rfc3986-validator==0.1.1 with their runtime dependencies "
        "in the candidate environment before startup.")
_SECRET_FIELDS = frozenset(("api_key", "access_token", "refresh_token", "authorization",
                            "token", "password", "client_secret", "authtoken", "auth_token", "id_token"))


def validation_errors(definition: str, value: object) -> list[str]:
    """Return safe structural and semantic violations; unknown definitions raise KeyError."""
    if definition not in _BUNDLE["$defs"]:
        raise KeyError(definition)
    try:
        _wire(value).encode("utf-8")
    except (ValueError, TypeError, UnicodeError):
        return ["$: finite UTF-8 JSON value required"]
    schema = {"$defs": _BUNDLE["$defs"], "$ref": f"#/$defs/{definition}"}
    checker = _Validator(schema, format_checker=_FORMAT_CHECKER)
    errors = [f"$.{'.'.join(map(str, e.absolute_path))}: {e.validator}" for e in checker.iter_errors(value)]
    if errors:
        return errors
    return _semantic_errors(definition, value)


def _endpoint_allowed(url: object, provider: str) -> bool:
    """Syntax/literal-address check only; runtime DNS approval remains T27/T14."""
    try:
        u = urlsplit(url)
        if (u.scheme != "https" or not u.hostname or u.username is not None
                or u.password is not None or u.query or u.fragment):
            return False
        # Access validates the port's numeric syntax and range without DNS/I/O.
        u.port
        if provider == "openai" and url.rstrip("/") != "https://api.openai.com/v1":
            return False
        host = u.hostname.lower()
        if host in ("localhost", "localhost.localdomain") or host.endswith(".localhost"):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return True
    except (ValueError, TypeError, AttributeError):
        return False


def _wire(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _semantic_errors(definition: str, value: object) -> list[str]:
    errors = []
    if definition.endswith("_request"):
        if len(_wire(value).encode("utf-8")) > 65536:
            errors.append("$: input UTF-8 byte budget")
        for key in ("project_id", "task_id", "report_id", "request_id", "profile", "question", "section", "cursor", "reason"):
            if isinstance(value.get(key), str) and not value[key].strip():
                errors.append(f"$.{key}: nonblank business string required")

    def visit(v, path="$"):
        if isinstance(v, list):
            for i, item in enumerate(v):
                visit(item, f"{path}[{i}]")
            return
        if not isinstance(v, dict):
            return
        def require(condition, rule):
            if not condition:
                errors.append(f"{path}: {rule}")

        if {"codex", "profiles"} <= v.keys():
            codex = v["codex"]
            enabled = v.get("enabled", False) and codex.get("enabled", False)
            require(not enabled or codex.get("auth_mode", "unselected") in ("chatgpt", "api_key"), "enabled capability requires selected authentication")
            require(_endpoint_allowed(codex.get("api_base_url", "https://api.openai.com/v1"), codex.get("api_provider", "openai")), "approved API endpoint required")
            for profile in v["profiles"].values():
                require(not enabled or bool(profile.get("model", "").strip()), "enabled profile requires explicit model")
                for key in ("model", "reasoning_effort", "service_tier"):
                    option = profile.get(key, "")
                    require(not option or not any(c.isspace() for c in option), "model options cannot contain whitespace")
                require(profile.get("service_tier", "standard") != "", "service tier cannot be empty")

        if {"auth_mode", "billing_source", "provider", "credential_context_id", "requested_service_tier"} <= v.keys():
            expected = "chatgpt_plan" if v["auth_mode"] == "chatgpt" else ("openai_api" if v["provider"] == "openai" else "custom_api")
            require(v["billing_source"] == expected, "billing source must match selected identity")
            if v["auth_mode"] == "chatgpt":
                require(v["provider"] == "openai" and v.get("endpoint") is None, "subscription cannot bind an API endpoint")
            else:
                require(_endpoint_allowed(v.get("endpoint"), v["provider"]), "API execution requires approved endpoint binding")

        if {"configured", "credential_revision", "storage", "status", "bound_endpoint"} <= v.keys():
            require(v["configured"] or (v["status"] != "configured" and v["bound_endpoint"] is None), "credential presence mismatch")
            require(not v["configured"] or bool(v["bound_endpoint"]), "stored credential requires endpoint binding")

        if {"model_id", "availability", "verified_selection_fingerprint", "reasoning_effort_status"} <= v.keys():
            require(v["availability"] != "verified" or (bool(v.get("verified_selection_fingerprint")) and bool(v.get("checked_at")) and v.get("compatibility") == "supported"), "verified model requires exact selection evidence")
            efforts = v.get("reasoning_efforts", [])
            default = v.get("default_reasoning_effort")
            require(default is None or default in efforts, "default reasoning effort must be supported")
            require(v["reasoning_effort_status"] != "not_supported" or (not efforts and default is None), "unsupported reasoning cannot advertise levels")

        if {"models", "catalog_revision", "auth_mode"} <= v.keys():
            require(all(m["auth_mode"] == v["auth_mode"] for m in v["models"]), "catalog cannot cross authentication contexts")
            ids = [m["model_id"] for m in v["models"]]
            require(len(ids) == len(set(ids)), "duplicate catalog model IDs")

        if {"model_id", "reasoning_effort", "service_tier", "option"} <= v.keys():
            opt = v["option"]
            if opt is None:
                require(not v["reasoning_effort"] and v["service_tier"] == "standard", "manual unverified model cannot assume extra parameters")
            else:
                require(v["model_id"] == opt["model_id"], "selected model must match catalog option")
                require(not v["reasoning_effort"] or (opt["reasoning_effort_status"] == "known" and v["reasoning_effort"] in opt["reasoning_efforts"]), "unsupported selected reasoning effort")
                tiers = opt["service_tiers"]
                require(v["service_tier"] in tiers if tiers else v["service_tier"] == "standard", "unsupported or unknown advanced service tier")

        if "verified" in v and "checks" in v:
            require(not v["verified"] or (v["checks"].get("config_loaded") is True and v["checks"].get("local_health") is True), "verified save requires loaded config and healthy local service")
        if v.get("connection_state") == "ready":
            require(v.get("public_verified") is True and v.get("public_reachable") is True, "ready requires actual MCP verification and reachability")
        if v.get("connection_state") == "auth_required":
            require(v.get("public_verified") is False and v.get("public_reachable") is True and v.get("auth_discovery_verified") is True, "auth_required requires reachable verified discovery")
        if {"state", "mechanisms", "tested_boundaries"} <= v.keys():
            require(v["state"] != "verified" or (bool(v["mechanisms"]) and bool(v["tested_boundaries"])), "verified enforcement requires tested mechanisms")

        if "response_chars" in v:
            wire = _wire(v)
            require(v["response_chars"] == len(wire), "response_chars must count complete compact JSON")
            require(len(wire) <= 32000 and len(wire.encode("utf-8")) <= 131072, "MCP output budget")
            if "pagination" in v:
                p = v["pagination"]
                text = v["page"]["text"] if isinstance(v.get("page"), dict) else ""
                require(p["returned_chars"] == len(text), "returned_chars must count page text")
                require((p["next_cursor"] is not None) == p["has_more"], "pagination continuation mismatch")
                if v.get("page") is None:
                    require(p["returned_chars"] == p["total_chars"] == p["remaining_chars"] == 0, "no page requires zero body counts")
        if {"task_id", "status", "finished_at"} <= v.keys():
            terminal = v["status"] in ("succeeded", "failed", "cancelled", "timed_out", "interrupted")
            require(terminal == (v["finished_at"] is not None), "terminal status requires finished_at; active status forbids it")
            require(terminal == bool(v["finish_reason"]), "terminal requires finish reason; active status forbids it")
            require(bool(v["requested_model"].strip()), "accepted task requires explicit requested model")
            require(v["cost_basis"] != "unknown" or v["cost"] is None, "unknown cost must remain null")
        if {"evidence_id", "path", "line_start", "line_end"} <= v.keys():
            start, end = v["line_start"], v["line_end"]
            require(start is None or end is None or start <= end, "evidence line range reversed")
            p = v["path"]
            require(not p.startswith(("/", "\\")) and not re.match(r"^[A-Za-z]:", p) and ".." not in p.replace("\\", "/").split("/"), "evidence path must remain project relative")

        for key, item in v.items():
            if key in ("created_at", "started_at", "finished_at", "updated_at", "checked_at", "generated_at", "fetched_at", "last_verified_at") and isinstance(item, str):
                require(item.endswith(("Z", "z", "+00:00")), "timestamps must be RFC3339 UTC")
            require(key not in _SECRET_FIELDS or (definition == "setup_credential_put_request" and path == "$" and key == "api_key"), "secret field outside dedicated credential input")
            visit(item, f"{path}.{key}")

    visit(value)
    return errors
