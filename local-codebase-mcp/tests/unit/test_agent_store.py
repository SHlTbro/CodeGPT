"""Real SQLite files/connections; identity/model values are nonsecret fixtures."""
from copy import deepcopy
from pathlib import Path
import sqlite3
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
import subprocess
import threading
from contextlib import contextmanager

import pytest
from local_agents.contracts import validation_errors
from local_agents.store import Store, StoreError


def selection(profile="reader", model="fixture-model"):
    return {"profile": profile, "requested_model": model, "backend_version": None,
        "config_revision": "a" * 64, "execution_context": {
            "auth_mode": "chatgpt", "billing_source": "chatgpt_plan", "provider": "openai",
            "endpoint": None, "credential_context_id": "opaque-fixture-owner", "credential_revision": "cred-r1",
            "requested_reasoning_effort": "high", "resolved_reasoning_effort": None,
            "requested_service_tier": "standard", "resolved_service_tier": None,
            "selection_fingerprint": "b" * 64, "effective_auth_verified": False}}


def request(**changes):
    return {"project_id": "project p", "request_id": "request-r1", **changes}


def test_atomic_acceptance_and_control_rollback_together(tmp_path):
    store = Store(tmp_path / "db.sqlite", tmp_path / "results")
    with pytest.raises(RuntimeError):
        with store.atomic():
            store.set_scheduler_control({"enabled": True, "max_running": 2, "max_queued": 20})
            store.accept(owner_id="o", operation="project_context", request=request(), selection=selection())
            raise RuntimeError("fixture rollback")
    assert store.list_tasks() == []
    assert store.get_scheduler_control() is None


def test_caught_nested_failure_marks_atomic_transaction_rollback_only(tmp_path):
    store = Store(tmp_path / "db.sqlite", tmp_path / "results")
    with pytest.raises(StoreError) as error:
        with store.atomic():
            store.accept(owner_id="o", operation="project_context", request=request(), selection=selection())
            try:
                with store.atomic():
                    raise RuntimeError("fixture nested failure")
            except RuntimeError:
                pass
    assert error.value.code == "STORAGE_UNAVAILABLE"
    assert store.list_tasks() == []


def test_storage_v1_upgrade_preserves_task_and_binding_bytes(tmp_path):
    db, results = tmp_path / "db.sqlite", tmp_path / "results"
    store = Store(db, results)
    first = store.accept(owner_id="o", operation="project_context", request=request(), selection=selection())
    with sqlite3.connect(db) as connection:
        before = connection.execute("SELECT * FROM tasks").fetchall(), connection.execute("SELECT * FROM bindings").fetchall()
        connection.execute("DROP TABLE scheduler_resources")
        connection.execute("DROP TABLE scheduler_control")
        connection.execute("UPDATE store_meta SET value='1' WHERE key='storage_schema_version'")
    upgraded = Store(db, results)
    with sqlite3.connect(db) as connection:
        after = connection.execute("SELECT * FROM tasks").fetchall(), connection.execute("SELECT * FROM bindings").fetchall()
        assert connection.execute("SELECT value FROM store_meta WHERE key='storage_schema_version'").fetchone() == ("2",)
    assert before == after
    assert upgraded.get_task("o", first.value["task_id"]) == first.value
    assert not upgraded.accept(owner_id="o", operation="project_context", request=request()).created


@pytest.mark.parametrize("payload", ['{"enabled":true,"max_running":999,"max_queued":20}', 'null'])
def test_corrupt_private_scheduler_control_fails_closed(tmp_path, payload):
    store = Store(tmp_path / "db.sqlite", tmp_path / "results")
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("INSERT INTO scheduler_control VALUES(1,?)", (payload,))
    with pytest.raises(StoreError) as error:
        store.get_scheduler_control()
    assert error.value.code == "STORAGE_UNAVAILABLE"


def test_first_acceptance_is_committed_and_reopened_before_any_dispatch(tmp_path):
    db, results = tmp_path / "tasks.sqlite", tmp_path / "results"
    store = Store(db, results)
    accepted = store.accept(owner_id="opaque-owner", operation="project_context", request=request(), selection=selection())
    assert accepted is not None
    assert accepted.created and accepted.kind == "task"
    assert validation_errors("task", accepted.value) == []
    assert accepted.value["status"] == "queued"
    assert accepted.value["resolved_model"] is None and accepted.value["usage"] is None
    other = Store(db, results)
    assert other.get_task("opaque-owner", accepted.value["task_id"]) == accepted.value
    assert len(other.list_tasks("opaque-owner")) == 1


def report(task, report_id="report-r1"):
    return {"report_id": report_id, "project_id": task["project_id"], "parent_report_id": None,
        "report_schema_version": "2.0.0", "generated_at": "2026-10-07T12:00:00Z", "snapshot_id": "snapshot-r1",
        "config_revision": task["config_revision"], "backend": "codex", "backend_version": task["backend_version"],
        "profile": task["profile"], "freshness": {"state": "unknown", "checked_at": None, "reasons": []},
        "summary": "Metadata fixture", "sections": [], "completion": "complete", "verification": "not_run",
        "execution_context": deepcopy(task["execution_context"])}


def test_publish_expire_and_old_request_never_reaccepts_execution(tmp_path):
    store = Store(tmp_path / "db.sqlite", tmp_path / "results")
    accepted = store.accept(owner_id="o", operation="project_context", request=request(), selection=selection())
    metadata = report(accepted.value)
    body = store.results_dir / "report-r1.json"
    body.write_bytes(b'{"body":"simulated immutable report"}')
    digest = hashlib.sha256(body.read_bytes()).hexdigest()
    assert callable(getattr(store, "publish_report", None))
    assert store.publish_report("o", accepted.value["task_id"], metadata, content_ref=body.name, content_sha256=digest) == metadata
    assert store.get_report("o", "report-r1") == metadata
    store.expire_report("o", "report-r1")
    body.unlink()
    with pytest.raises(StoreError) as error:
        store.accept(owner_id="o", operation="project_context", request=request(), selection=selection(model="changed"))
    assert error.value.code == "RESULT_EXPIRED"
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM bindings").fetchone()[0] == 1
        assert connection.execute("SELECT payload,content_ref FROM reports").fetchone() == (None, None)


def test_many_connections_accept_exactly_one_frozen_binding(tmp_path):
    db, results = tmp_path / "db.sqlite", tmp_path / "results"
    stores = [Store(db, results) for _ in range(16)]
    barrier = threading.Barrier(len(stores))
    def concurrent_accept(i):
        barrier.wait()
        return stores[i].accept(owner_id="o", operation="project_context", request=request(max_chars=2048 + i),
            selection=selection(profile=f"reader-{i}", model=f"fixture-{i}"))
    with ThreadPoolExecutor(max_workers=16) as pool:
        accepted = list(pool.map(concurrent_accept, range(16)))
    assert sum(item.created for item in accepted) == 1
    assert len({item.value["task_id"] for item in accepted}) == 1
    assert all(item.value == accepted[0].value for item in accepted)
    assert len(stores[0].list_tasks("o")) == 1


def test_presentation_and_omitted_defaults_replay_without_current_selection(tmp_path):
    store = Store(tmp_path / "db.sqlite", tmp_path / "results")
    first = store.accept(owner_id="o", operation="project_context", request=request(), selection=selection())
    replay = store.accept(owner_id="o", operation="project_context", request=request(max_chars=32000))
    assert not replay.created and replay.value == first.value
    with pytest.raises(StoreError) as error:
        store.accept(owner_id="o", operation="project_context", request=request(profile="other-reader"), selection=selection())
    assert error.value.code == "REQUEST_CONFLICT"


def test_owner_project_and_operation_are_independent_binding_dimensions(tmp_path):
    store = Store(tmp_path / "db.sqlite", tmp_path / "results")
    values = [store.accept(owner_id=owner, operation="project_context", request=request(project_id=project), selection=selection())
              for owner, project in [("o1", "p1"), ("o2", "p1"), ("o1", "p2")]]
    assert len({v.value["task_id"] for v in values}) == 3
    with pytest.raises(StoreError) as error:
        store.get_task("o2", values[0].value["task_id"])
    assert error.value.code == "TASK_NOT_FOUND"
    trial = {"revision": "c" * 64, "request_id": "request-r1", "project_id": "p1", "profile": "reader",
             "expected_credential_revision": "cred-r1", "allow_model_request": True}
    test = store.accept(owner_id="o1", operation="model_test", request=trial, selection=selection())
    assert test.created and test.value["purpose"] == "model_test"
    trial["allow_model_request"] = False
    with pytest.raises(StoreError) as error:
        store.accept(owner_id="o1", operation="model_test", request=trial, selection=selection())
    assert error.value.code == "MODEL_TEST_CONSENT_REQUIRED"
    assert len(store.list_tasks()) == 4


def test_state_compare_and_swap_preserves_frozen_identity(tmp_path):
    store = Store(tmp_path / "db.sqlite", tmp_path / "results")
    first = store.accept(owner_id="o", operation="project_context", request=request(), selection=selection())
    running = deepcopy(first.value)
    running.update(status="running", started_at="2026-10-07T12:00:00Z")
    store.replace_task("o", running, expected_status="queued")
    assert store.get_task("o", running["task_id"])["status"] == "running"
    with pytest.raises(StoreError) as error:
        store.replace_task("o", running, expected_status="queued")
    assert error.value.code == "REQUEST_CONFLICT"
    running["execution_context"]["credential_revision"] = "different-credential"
    with pytest.raises(StoreError) as error:
        store.replace_task("o", running, expected_status="running")
    assert error.value.code == "REQUEST_CONFLICT"


def test_same_report_identifier_cannot_publish_for_another_execution(tmp_path):
    store = Store(tmp_path / "db.sqlite", tmp_path / "results")
    first = store.accept(owner_id="o", operation="project_context", request=request(), selection=selection())
    second = store.accept(owner_id="o", operation="project_context", request=request(request_id="new-id"), selection=selection())
    body = store.results_dir / "r.json"
    body.write_text("immutable fixture", encoding="utf-8")
    digest = hashlib.sha256(body.read_bytes()).hexdigest()
    metadata = report(first.value)
    store.publish_report("o", first.value["task_id"], metadata, content_ref=body.name, content_sha256=digest)
    with pytest.raises(StoreError) as error:
        store.publish_report("o", second.value["task_id"], metadata, content_ref=body.name, content_sha256=digest)
    assert error.value.code == "REQUEST_CONFLICT"


def publish_fixture(store, accepted):
    body = store.results_dir / "body.json"
    body.write_bytes(b'{"body":"immutable fixture"}')
    metadata = report(accepted.value)
    store.publish_report("o", accepted.value["task_id"], metadata,
        content_ref=body.name, content_sha256=hashlib.sha256(body.read_bytes()).hexdigest())
    return metadata, body


def test_cached_report_binding_has_no_new_task_and_survives_expiration(tmp_path):
    store = Store(tmp_path / "db.sqlite", tmp_path / "results")
    first = store.accept(owner_id="o", operation="project_context", request=request(), selection=selection())
    metadata, body = publish_fixture(store, first)
    cache = store.accept(owner_id="o", operation="project_context", request=request(request_id="cache-id"),
                         cached_report_id=metadata["report_id"])
    assert cache.kind == "report" and cache.value == metadata and cache.created
    assert len(store.list_tasks("o")) == 1
    reference, digest = store.get_content_reference("o", metadata["report_id"])
    assert reference == body.name and digest == hashlib.sha256(body.read_bytes()).hexdigest()
    with pytest.raises(StoreError) as error:
        store.get_report("different-owner", metadata["report_id"])
    assert error.value.code == "REPORT_NOT_FOUND"
    store.expire_report("o", metadata["report_id"])
    with pytest.raises(StoreError) as error:
        store.accept(owner_id="o", operation="project_context", request=request(request_id="cache-id"))
    assert error.value.code == "RESULT_EXPIRED"
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM bindings").fetchone()[0] == 2


def test_sqlite_commit_boundary_rolls_back_task_if_binding_write_fails(tmp_path):
    store = Store(tmp_path / "db.sqlite", tmp_path / "results")
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("CREATE TRIGGER refuse_binding BEFORE INSERT ON bindings BEGIN SELECT RAISE(ABORT,'controlled write failure'); END")
    with pytest.raises(StoreError) as error:
        store.accept(owner_id="o", operation="project_context", request=request(), selection=selection())
    assert error.value.code == "STORAGE_UNAVAILABLE"
    assert store.list_tasks() == []
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM bindings").fetchone()[0] == 0


@contextmanager
def deny_windows_write(path, owned_root):
    assert path.resolve().is_relative_to(owned_root.resolve())
    if os.name != "nt":
        pytest.skip("This evidence requires a real Windows DACL")
    principal = os.environ["USERDOMAIN"] + "\\" + os.environ["USERNAME"]
    rights = "(OI)(CI)(W)" if path.is_dir() else "(W)"
    result = subprocess.run(["icacls", str(path), "/deny", principal + ":" + rights], capture_output=True)
    if result.returncode:
        pytest.skip("Could not set owned fixture DACL")
    try:
        yield
    finally:
        restored = subprocess.run(["icacls", str(path), "/remove:d", principal], capture_output=True)
        assert restored.returncode == 0, "Owned fixture DACL restore failed"


@pytest.mark.parametrize("target", ["database", "results"])
def test_real_windows_unwritable_storage_accepts_zero_records(tmp_path, target):
    store = Store(tmp_path / "db.sqlite", tmp_path / "results")
    path = store.database_path if target == "database" else store.results_dir
    with deny_windows_write(path, tmp_path):
        with pytest.raises(StoreError) as error:
            store.accept(owner_id="o", operation="project_context", request=request(), selection=selection())
        assert error.value.code == "STORAGE_UNAVAILABLE"
    assert store.list_tasks() == []
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM bindings").fetchone()[0] == 0


def test_future_database_and_task_schema_fail_closed_without_reset(tmp_path):
    db, results = tmp_path / "db.sqlite", tmp_path / "results"
    store = Store(db, results)
    first = store.accept(owner_id="o", operation="project_context", request=request(), selection=selection())
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE tasks SET schema_version='9.0.0'")
    with pytest.raises(StoreError) as error:
        store.get_task("o", first.value["task_id"])
    assert error.value.code == "SCHEMA_UNSUPPORTED"
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE store_meta SET value='9.0.0' WHERE key='contract_version'")
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    with pytest.raises(StoreError) as error:
        Store(db, results)
    assert error.value.code == "SCHEMA_UNSUPPORTED"
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("kind", ["not_sqlite", "unrelated_sqlite"])
def test_corrupt_or_unrecognized_existing_database_is_not_overwritten(tmp_path, kind):
    db = tmp_path / "existing.sqlite"
    if kind == "not_sqlite":
        db.write_bytes(b"invalid SQLite fixture, must be preserved")
    else:
        with sqlite3.connect(db) as connection:
            connection.execute("CREATE TABLE user_history(value TEXT)")
            connection.execute("INSERT INTO user_history VALUES('preserve history')")
    before = db.read_bytes()
    with pytest.raises(StoreError) as error:
        Store(db, tmp_path / "results")
    assert error.value.code in ("SCHEMA_UNSUPPORTED", "STORAGE_UNAVAILABLE")
    assert db.read_bytes() == before


def test_secret_or_owner_override_never_enters_persistence(tmp_path):
    store = Store(tmp_path / "db.sqlite", tmp_path / "results")
    for override in ({"owner_id": "client-owned"}, {"argv": ["simulated-command"]}, {"api_key": "simulated-secret"}):
        with pytest.raises(StoreError):
            store.accept(owner_id="trusted-owner", operation="project_context", request=request(**override), selection=selection())
    bad_selection = selection()
    bad_selection["execution_context"]["access_token"] = "simulated-secret"
    with pytest.raises(StoreError) as error:
        store.accept(owner_id="trusted-owner", operation="project_context", request=request(), selection=bad_selection)
    assert "simulated-secret" not in str(error.value)
    assert store.list_tasks() == []


def test_changed_body_is_expired_not_reexecuted_and_partial_publish_is_atomic(tmp_path):
    store = Store(tmp_path / "db.sqlite", tmp_path / "results")
    first = store.accept(owner_id="o", operation="project_context", request=request(), selection=selection())
    metadata, body = publish_fixture(store, first)
    body.write_bytes(b"changed fixture body")
    with pytest.raises(StoreError) as error:
        store.accept(owner_id="o", operation="project_context", request=request())
    assert error.value.code == "RESULT_EXPIRED"
    assert len(store.list_tasks("o")) == 1
    second = store.accept(owner_id="o", operation="project_context", request=request(request_id="second"), selection=selection())
    with pytest.raises(StoreError):
        store.publish_report("o", second.value["task_id"], report(second.value, "missing-report"), content_ref="missing.json", content_sha256="c" * 64)
    assert store.get_task("o", second.value["task_id"])["report_id"] is None
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 1


def test_unrecognized_storage_layout_is_not_reset(tmp_path):
    store = Store(tmp_path / "db.sqlite", tmp_path / "results")
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("ALTER TABLE tasks ADD COLUMN future_field TEXT")
    before = store.database_path.read_bytes()
    with pytest.raises(StoreError) as error:
        Store(store.database_path, store.results_dir)
    assert error.value.code == "SCHEMA_UNSUPPORTED"
    assert store.database_path.read_bytes() == before


def test_terminal_task_cleanup_keeps_binding_and_active_task_cannot_expire(tmp_path):
    store = Store(tmp_path / "db.sqlite", tmp_path / "results")
    first = store.accept(owner_id="o", operation="project_context", request=request(), selection=selection())
    with pytest.raises(StoreError) as error:
        store.expire_task("o", first.value["task_id"])
    assert error.value.code == "POLICY_DENIED"
    terminal = deepcopy(first.value)
    terminal.update(status="cancelled", finished_at="2026-10-07T12:00:00Z", finish_reason="cancelled_before_start")
    store.replace_task("o", terminal, expected_status="queued")
    store.expire_task("o", first.value["task_id"])
    with pytest.raises(StoreError) as error:
        store.accept(owner_id="o", operation="project_context", request=request())
    assert error.value.code == "RESULT_EXPIRED"
    assert store.list_tasks("o") == []
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM bindings").fetchone()[0] == 1


def test_corrupt_binding_fingerprint_fails_closed(tmp_path):
    store = Store(tmp_path / "db.sqlite", tmp_path / "results")
    store.accept(owner_id="o", operation="project_context", request=request(), selection=selection())
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("UPDATE bindings SET fingerprint='corrupt-fixture'")
    with pytest.raises(StoreError) as error:
        store.accept(owner_id="o", operation="project_context", request=request())
    assert error.value.code == "STORAGE_UNAVAILABLE"


def test_report_reference_never_escapes_owned_results(tmp_path):
    store = Store(tmp_path / "db.sqlite", tmp_path / "results")
    first = store.accept(owner_id="o", operation="project_context", request=request(), selection=selection())
    outside = tmp_path / "outside-fixture.txt"
    outside.write_bytes(b"not an owned report")
    with pytest.raises(StoreError) as error:
        store.publish_report("o", first.value["task_id"], report(first.value),
            content_ref="../outside-fixture.txt", content_sha256=hashlib.sha256(outside.read_bytes()).hexdigest())
    assert error.value.code == "INVALID_ARGUMENT"
    assert store.get_task("o", first.value["task_id"])["report_id"] is None


def test_private_frozen_inputs_survive_reopen_then_cleanup_leaves_light_tombstone(tmp_path):
    store = Store(tmp_path / "db.sqlite", tmp_path / "results")
    original = request(intent="question", question="Fixture question that must not remain after expiry")
    first = store.accept(owner_id="o", operation="project_context", request=original, selection=selection())
    assert callable(getattr(store, "get_task_input", None))
    reopened = Store(store.database_path, store.results_dir)
    assert reopened.get_task_input("o", first.value["task_id"])["question"] == original["question"]
    metadata, _ = publish_fixture(store, first)
    store.expire_report("o", metadata["report_id"])
    with sqlite3.connect(store.database_path) as connection:
        params, digest = connection.execute("SELECT params,fingerprint FROM bindings").fetchone()
    assert params == "{}" and len(digest) == 64
    with pytest.raises(StoreError) as error:
        reopened.accept(owner_id="o", operation="project_context", request=original)
    assert error.value.code == "RESULT_EXPIRED"
