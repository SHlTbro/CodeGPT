"""Nonsecret optional-agent configuration; syntax never probes a backend.

Defaults and validation come from the frozen contract. This module does not
import the host Config or Project, access credentials, or discover models.
"""
from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass

from local_agents.contracts import CONTRACT_PATH, validation_errors

_DEFAULTS = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))["config_defaults"]


class AgentConfigError(ValueError):
    """Safe syntax/consistency errors, never backend readiness failures."""


def _validate(definition: str, value: object) -> None:
    problems = validation_errors(definition, value)
    if problems:
        raise AgentConfigError("; ".join(problems))


def default_agent_config() -> dict:
    return copy.deepcopy(_DEFAULTS["agent"])


def normalize_agent_config(raw: dict) -> dict:
    """Validate optional raw fields, fill defaults, validate the effective object."""
    _validate("raw_agent_config", raw)
    result = default_agent_config()
    for key, value in raw.items():
        if key in ("codex", "limits", "retention"):
            result[key].update(copy.deepcopy(value))
        elif key == "profiles":
            template = _DEFAULTS["agent"]["profiles"]["cheap_reader"]
            result[key] = {name: {**copy.deepcopy(template), **copy.deepcopy(profile)}
                           for name, profile in value.items()}
        else:
            result[key] = copy.deepcopy(value)
    _validate("agent_config", result)
    if result["default_profile"] not in result["profiles"]:
        raise AgentConfigError("agent.default_profile: 必须引用已配置的阅读档案")
    if any(not name.strip() for name in result["profiles"]):
        raise AgentConfigError("agent.profiles: 档案名称不能为空白")
    return result


@dataclass(frozen=True)
class ProjectAgentConfig:
    context_enabled: bool = False
    allowed_profiles: tuple[str, ...] = ("cheap_reader",)


def project_agent_from_data(raw: dict) -> ProjectAgentConfig:
    _validate("raw_project_agent", raw)
    value = {**copy.deepcopy(_DEFAULTS["project_agent"]), **copy.deepcopy(raw)}
    _validate("project_agent", value)
    return ProjectAgentConfig(value["context_enabled"], tuple(value["allowed_profiles"]))


def project_agent_to_dict(agent: ProjectAgentConfig) -> dict:
    value = asdict(agent)
    value["allowed_profiles"] = list(agent.allowed_profiles)
    return value


def preserve_agent_settings(proposed: dict, current: dict) -> dict:
    """Only omission by legacy clients preserves fields; explicit values win.

    Projects retain existing whole-array replacement semantics. Removed IDs
    stay removed; newly named IDs do not inherit another project's policy.
    """
    result = copy.deepcopy(proposed)
    if "agent" not in result and "agent" in current:
        result["agent"] = copy.deepcopy(current["agent"])
    previous = {p["id"]: p for p in current.get("projects", [])}
    projects = result.get("projects", [])
    if not isinstance(projects, list):
        return result  # The existing validator reports malformed legacy fields.
    for project in projects:
        if not isinstance(project, dict) or not isinstance(project.get("id"), str):
            continue
        old = previous.get(project.get("id"))
        if old is not None and "agent" not in project and "agent" in old:
            project["agent"] = copy.deepcopy(old["agent"])
    return result
