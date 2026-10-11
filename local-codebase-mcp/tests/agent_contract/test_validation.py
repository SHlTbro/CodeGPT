"""Public contract validation; no service, CLI, credential, or model invocation."""
import importlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CASES = json.loads((ROOT / "contracts/agent/cases.json").read_text(encoding="utf-8"))["cases"]


def validator():
    # A missing implementation is an assertion failure, not a collection error.
    assert (ROOT / "local_agents/contracts.py").is_file(), "contract validator not implemented"
    return importlib.import_module("local_agents.contracts")


@pytest.mark.parametrize("case", CASES, ids=[c["case_id"] for c in CASES])
def test_frozen_positive_and_negative_cases(case):
    errors = validator().validation_errors(case["schema_def"], case["value"])
    assert (not errors) == case["valid"], errors


@pytest.mark.parametrize("value", [
    {"task_id": "t", "wait_seconds": True},
    {"task_id": "t", "max_chars": 2048.0},
    {"task_id": "t", "section": None},
    {"task_id": "t", "owner_id": "client-controlled"},
    {"report_id": "r", "wait_seconds": 1},
    {"task_id": " \t"},
])
def test_poll_rejects_bad_boundary_inputs(value):
    assert validator().validation_errors("task_poll_request", value)


def test_registered_project_id_is_not_artificially_narrowed():
    assert not validator().validation_errors("project_context_request", {
        "project_id": "项目 / existing:id", "request_id": "request-1"})


def test_validation_errors_do_not_echo_secret_values():
    errors = validator().validation_errors("setup_credential_put_request", {
        "revision": "bad", "request_id": "r", "expected_credential_revision": None,
        "api_key": "synthetic-secret-do-not-print", "token": "synthetic-secret-do-not-print"})
    assert errors and "synthetic-secret-do-not-print" not in repr(errors)


def response(case_id="FX09"):
    return deepcopy(next(c["value"] for c in CASES if c["case_id"] == case_id))


def recount(value):
    for _ in range(5):
        value["response_chars"] = len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    return value


@pytest.mark.parametrize("field,value", [
    ("requested_model", "  "),
    ("finish_reason", "already-finished"),
    ("created_at", "2026-10-07T09:00:00+08:00"),
])
def test_task_rejects_semantically_contradictory_metadata(field, value):
    payload = response("FX08")
    payload["task"][field] = value
    assert validator().validation_errors("project_context_response", recount(payload))


def test_unknown_cost_cannot_claim_known_amount():
    payload = response("FX08")
    payload["task"]["cost_basis"] = "unknown"
    payload["task"]["cost"] = {"amount": 0, "currency": "USD"}
    assert validator().validation_errors("project_context_response", recount(payload))


def test_no_page_cannot_claim_body_counts():
    payload = response("FX08")
    payload["pagination"]["total_chars"] = 8
    assert validator().validation_errors("project_context_response", recount(payload))


def test_output_budget_counts_unicode_codepoints_and_whole_envelope():
    payload = response()
    payload["page"]["text"] = "中文😀"
    payload["pagination"]["returned_chars"] = 3
    assert not validator().validation_errors("task_poll_response", recount(payload))
    payload["pagination"]["returned_chars"] = 4  # UTF-16 count would be wrong.
    assert validator().validation_errors("task_poll_response", recount(payload))


def test_nonfinite_money_is_rejected_without_exception():
    assert validator().validation_errors("cost", {"amount": float("nan"), "currency": "USD"})


@pytest.mark.parametrize("definition", sorted({c["schema_def"] for c in CASES if c["schema_def"].endswith("_request")}))
def test_each_request_rejects_unknown_fields(definition):
    value = deepcopy(next(c["value"] for c in CASES if c["schema_def"] == definition and c["valid"]))
    value["command"] = "client supplied command"
    assert validator().validation_errors(definition, value)


def test_opaque_identity_and_actual_model_are_not_client_inputs():
    for field in ("owner_id", "native_session_id", "model", "endpoint", "api_key", "argv", "cwd", "env"):
        value = {"project_id": "p", "request_id": "r", field: "forbidden"}
        assert validator().validation_errors("project_context_request", value)


@pytest.mark.parametrize("key", ["token", "password", "client_secret", "authtoken", "auth_token", "id_token"])
@pytest.mark.parametrize("case_id,definition", [
    ("FX16", "setup_config_get_response_overlay"),
    ("FX18", "setup_status_response_overlay"),
    ("FX19", "setup_info_response_overlay"),
])
def test_additive_setup_overlays_do_not_allow_known_secret_fields(key, case_id, definition):
    value = response(case_id)
    target = value["data"] if case_id == "FX16" else value
    target[key] = "synthetic-sensitive-value"
    errors = validator().validation_errors(definition, value)
    assert errors and "synthetic-sensitive-value" not in repr(errors)


def test_custom_api_endpoint_rejects_invalid_port():
    value = response("OPT10")
    value["codex"]["api_provider"] = "custom_responses"
    value["codex"]["api_base_url"] = "https://models.example:invalid/v1"
    assert validator().validation_errors("agent_config", value)


@pytest.mark.parametrize("tier,valid", [("standard", True), ("priority", False)])
def test_unknown_service_tiers_only_allow_unverified_standard(tier, valid):
    value = response("OPT53")
    value["option"]["service_tiers"] = []
    value["service_tier"] = tier
    assert (not validator().validation_errors("model_selection", value)) == valid


def test_explicitly_supported_advanced_service_tier_is_allowed():
    value = response("OPT53")
    value["option"]["service_tiers"] = ["standard", "priority"]
    value["service_tier"] = "priority"
    assert not validator().validation_errors("model_selection", value)


@pytest.mark.parametrize("timestamp", ["invalid-dateZ", "2026-02-30T10:00:00Z", "2026-10-07T25:00:00Z"])
def test_invalid_rfc3339_calendar_and_time_are_rejected(timestamp):
    value = response("FX08")
    value["task"]["created_at"] = timestamp
    assert validator().validation_errors("project_context_response", recount(value))


@pytest.mark.parametrize("url", ["https://models.example/with space", "https://models.example/bad%zz"])
def test_invalid_uri_spaces_and_percent_encoding_are_rejected(url):
    value = response("OPT10")
    value["codex"]["api_provider"] = "custom_responses"
    value["codex"]["api_base_url"] = url
    assert validator().validation_errors("agent_config", value)


def test_required_jsonschema_format_checkers_are_actually_registered():
    from jsonschema import FormatChecker
    assert {"date-time", "uri"} <= FormatChecker().checkers.keys(), "required format dependencies missing"


def test_missing_format_dependencies_fail_closed_at_bootstrap(monkeypatch):
    from jsonschema import FormatChecker
    module = validator()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(FormatChecker, "checkers", {
                name: checker for name, checker in FormatChecker.checkers.items()
                if name not in {"date-time", "uri"}})
            with pytest.raises(RuntimeError, match="rfc3339-validator.*rfc3986-validator"):
                importlib.reload(module)
    finally:
        importlib.reload(module)


def test_all_40_families_have_explicit_offline_scope():
    scopes = json.loads((ROOT / "contracts/agent/verification-scope.json").read_text(encoding="utf-8"))
    assert set(scopes) == {f"CT{i:02d}" for i in range(1, 41)}
    for family, scope in scopes.items():
        assert scope["runtime_status"] == "NOT_RUN"
        assert scope["runtime_owner"] and scope["boundary"]
        assert scope["fixtures"] == [c["case_id"] for c in CASES if family in c["contract_cases"]]
