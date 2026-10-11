"""SQLite task/report metadata for trusted services, never a model launcher.

owner_id is supplied by an authenticated service, not copied from a client
request. Acceptance.created authorizes dispatch only for kind=task after the
transaction commits. ACL/admission/state transitions belong to T09/T15;
schema, immutable identity and persistence/CAS boundaries are enforced here.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from pathlib import PurePosixPath
import sqlite3
import tempfile
import threading
import uuid

from local_agents.contracts import CONTRACT_VERSION, validation_errors

STORAGE_SCHEMA_VERSION = "2"


class StoreError(RuntimeError):
    """Safe error code; no SQL, paths, request values or credentials in errors."""
    def __init__(self, code, message=None):
        self.code = code
        self.retriable = code == "STORAGE_UNAVAILABLE"
        super().__init__(message or {
            "STORAGE_UNAVAILABLE": "任务存储不可用；未接受新的执行请求。",
            "SCHEMA_UNSUPPORTED": "持久数据版本不受支持；不会重置历史。",
            "REQUEST_CONFLICT": "请求 ID 已绑定不同参数，或状态已经改变。",
            "RESULT_EXPIRED": "结果已过期；原请求不会再次执行。",
            "TASK_NOT_FOUND": "当前身份下未找到任务。",
            "REPORT_NOT_FOUND": "当前身份下未找到报告。",
            "MODEL_TEST_CONSENT_REQUIRED": "模型试跑必须明确同意消费。",
        }.get(code, "任务存储输入不符合契约。"))


@dataclass(frozen=True)
class Acceptance:
    kind: str
    value: dict
    created: bool


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _validate(definition, value):
    if validation_errors(definition, value):
        raise StoreError("INVALID_ARGUMENT")


def _owner(owner_id):
    if not isinstance(owner_id, str) or not owner_id.strip() or len(owner_id) > 128:
        raise StoreError("INVALID_ARGUMENT")


class Store:
    def __init__(self, database_path, results_dir):
        self.database_path = Path(database_path).absolute()
        self.results_dir = Path(results_dir).absolute()
        self._local = threading.local()
        try:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            self.results_dir.mkdir(parents=True, exist_ok=True)
            self._initialize()
        except (OSError, sqlite3.Error):
            raise StoreError("STORAGE_UNAVAILABLE") from None

    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
        except sqlite3.Error:
            connection.close()
            raise
        return connection

    def _check_schema(self, connection, *, legacy=False):
        metadata = dict(connection.execute("SELECT key,value FROM store_meta"))
        if metadata != {"storage_schema_version": "1" if legacy else STORAGE_SCHEMA_VERSION,
                        "contract_version": CONTRACT_VERSION}:
            raise StoreError("SCHEMA_UNSUPPORTED")
        columns = {
            "store_meta": ("key", "value"),
            "tasks": ("task_id", "owner_id", "project_id", "schema_version", "status", "payload", "expired"),
            "reports": ("report_id", "task_id", "owner_id", "project_id", "schema_version", "payload", "content_ref", "content_sha256", "expired"),
            "bindings": ("owner_id", "project_id", "operation", "request_id", "params", "fingerprint", "kind", "object_id"),
        }
        primary_keys = {"store_meta": ("key",), "tasks": ("task_id",), "reports": ("report_id",),
                        "bindings": ("owner_id", "project_id", "operation", "request_id")}
        if not legacy:
            columns.update(scheduler_control=("singleton", "payload"),
                           scheduler_resources=("task_id", "session_resource", "pending_terminal"))
            primary_keys.update(scheduler_control=("singleton",), scheduler_resources=("task_id",))
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if tables != set(columns):
            raise StoreError("SCHEMA_UNSUPPORTED")
        for table, expected in columns.items():
            rows = list(connection.execute(f"PRAGMA table_info({table})"))  # Static internal names only.
            pk = tuple(row["name"] for row in sorted(rows, key=lambda row: row["pk"]) if row["pk"])
            if tuple(row["name"] for row in rows) != expected or pk != primary_keys[table]:
                raise StoreError("SCHEMA_UNSUPPORTED")

    def _initialize(self):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if tables:
                if "store_meta" not in tables:
                    raise StoreError("SCHEMA_UNSUPPORTED")
                legacy = dict(connection.execute("SELECT key,value FROM store_meta")).get("storage_schema_version") == "1"
                self._check_schema(connection, legacy=legacy)
            else:
                connection.execute("CREATE TABLE store_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
                connection.executemany("INSERT INTO store_meta VALUES(?,?)", [
                    ("storage_schema_version", STORAGE_SCHEMA_VERSION), ("contract_version", CONTRACT_VERSION)])
                connection.execute("""CREATE TABLE tasks(
                    task_id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,project_id TEXT NOT NULL,
                    schema_version TEXT NOT NULL,status TEXT NOT NULL,payload TEXT,expired INTEGER NOT NULL DEFAULT 0)""")
                connection.execute("""CREATE TABLE reports(
                    report_id TEXT PRIMARY KEY,task_id TEXT NOT NULL,owner_id TEXT NOT NULL,project_id TEXT NOT NULL,
                    schema_version TEXT NOT NULL,payload TEXT,content_ref TEXT,content_sha256 TEXT,
                    expired INTEGER NOT NULL DEFAULT 0)""")
                connection.execute("""CREATE TABLE bindings(
                    owner_id TEXT NOT NULL,project_id TEXT NOT NULL,operation TEXT NOT NULL,
                    request_id TEXT NOT NULL,params TEXT NOT NULL,fingerprint TEXT NOT NULL,
                    kind TEXT NOT NULL,object_id TEXT NOT NULL,
                    PRIMARY KEY(owner_id,project_id,operation,request_id))""")
                legacy = True
            if legacy:
                connection.execute("CREATE TABLE scheduler_control(singleton INTEGER PRIMARY KEY CHECK(singleton=1),payload TEXT NOT NULL)")
                connection.execute("""CREATE TABLE scheduler_resources(task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
                    session_resource TEXT,pending_terminal TEXT)""")
                connection.execute("UPDATE store_meta SET value=? WHERE key='storage_schema_version'", (STORAGE_SCHEMA_VERSION,))
            connection.commit()
        finally:
            connection.close()

    @contextmanager
    def _transaction(self, *, write=False):
        active = getattr(self._local, "atomic", None)
        if active is not None:
            try:
                yield active["connection"]
            except BaseException:
                active["rollback_only"] = True
                raise
            return
        connection = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            self._check_schema(connection)
            yield connection
            connection.commit()
        except (OSError, sqlite3.Error, ValueError, TypeError):
            raise StoreError("STORAGE_UNAVAILABLE") from None
        finally:
            if connection is not None:
                connection.close()  # Uncommitted writes roll back, including binding.

    @contextmanager
    def atomic(self):
        """Thread-local joined BEGIN IMMEDIATE; caught nested errors roll back all.

        Calls on other Store instances do not join. Never span async work.
        """
        active = getattr(self._local, "atomic", None)
        if active is not None:
            try:
                yield self
            except BaseException:
                active["rollback_only"] = True
                raise
            return
        with self._transaction(write=True) as connection:
            self._local.atomic = {"connection": connection, "rollback_only": False}
            try:
                yield self
                if self._local.atomic["rollback_only"]:
                    raise StoreError("STORAGE_UNAVAILABLE")
            finally:
                del self._local.atomic

    def get_scheduler_control(self):
        """Private service control, never public task metadata."""
        with self._transaction() as connection:
            row = connection.execute("SELECT payload FROM scheduler_control WHERE singleton=1").fetchone()
            control = json.loads(row[0]) if row else None
            if row is not None and not self._valid_scheduler_control(control):
                raise StoreError("STORAGE_UNAVAILABLE")
            return control

    @staticmethod
    def _valid_scheduler_control(control):
        return isinstance(control, dict) and set(control) == {"enabled", "max_running", "max_queued"} and type(control["enabled"]) is bool and all(
            type(control[k]) is int and minimum <= control[k] <= limit for k, minimum, limit in (("max_running", 1, 4), ("max_queued", 0, 100)))

    def set_scheduler_control(self, control):
        if not self._valid_scheduler_control(control):
            raise StoreError("INVALID_ARGUMENT")
        with self._transaction(write=True) as connection:
            connection.execute("INSERT INTO scheduler_control VALUES(1,?) ON CONFLICT(singleton) DO UPDATE SET payload=excluded.payload", (_json(control),))

    def get_scheduler_resources(self, *, required_task_ids=()):
        with self._transaction() as connection:
            resources = {}
            for row in connection.execute("SELECT * FROM scheduler_resources"):
                task_id, session, pending = row["task_id"], row["session_resource"], row["pending_terminal"]
                task = connection.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
                if type(task_id) is not str or not task_id or not self._valid_scheduler_resource(session, pending) or task is None:
                    raise StoreError("STORAGE_UNAVAILABLE")
                status = task["status"]
                if status not in {"queued", "running", "cancelling", "succeeded", "failed", "cancelled", "timed_out", "interrupted"} or (
                        pending is not None and (status in {"queued", "running"} or status != "cancelling" and pending != status)):
                    raise StoreError("STORAGE_UNAVAILABLE")
                resources[task_id] = {"session_resource": session, "pending_terminal": pending}
            active_ids = [row[0] for row in connection.execute(
                "SELECT task_id FROM tasks WHERE expired=0 AND status IN ('queued','running','cancelling')")]
            if any(task_id not in resources for task_id in [*active_ids, *required_task_ids]):
                raise StoreError("STORAGE_UNAVAILABLE")
            return resources

    @staticmethod
    def _valid_scheduler_resource(session, pending):
        return (session is None or type(session) is str and 1 <= len(session) <= 256) and (
            pending is None or type(pending) is str and pending in {"succeeded", "failed", "cancelled", "timed_out", "interrupted"})

    def set_scheduler_resource(self, owner_id, task_id, *, session_resource=None, pending_terminal=None):
        """Trusted opaque key, never native session ID, token or argv."""
        if not self._valid_scheduler_resource(session_resource, pending_terminal):
            raise StoreError("INVALID_ARGUMENT")
        with self._transaction(write=True) as connection:
            self._task(connection, owner_id, task_id)
            connection.execute("INSERT INTO scheduler_resources VALUES(?,?,?) ON CONFLICT(task_id) DO UPDATE SET session_resource=excluded.session_resource,pending_terminal=excluded.pending_terminal",
                               (task_id, session_resource, pending_terminal))

    def _probe_results(self):
        with tempfile.TemporaryFile(dir=self.results_dir) as probe:
            probe.write(b"T08 persistence check")
            probe.flush()

    @staticmethod
    def _params(operation, request, *, profile=None, frozen=None):
        if operation == "model_test":
            return {key: value for key, value in request.items() if key not in ("project_id", "request_id")}
        return {"intent": request.get("intent", frozen["intent"] if frozen else "overview"),
                "profile": request.get("profile", frozen["profile"] if frozen else profile),
                "question": request.get("question"), "report_id": request.get("report_id")}

    def _task(self, connection, owner_id, task_id):
        row = connection.execute("SELECT * FROM tasks WHERE task_id=? AND owner_id=?", (task_id, owner_id)).fetchone()
        if row is None:
            raise StoreError("TASK_NOT_FOUND")
        if row["schema_version"] != CONTRACT_VERSION:
            raise StoreError("SCHEMA_UNSUPPORTED")
        if row["expired"]:
            raise StoreError("RESULT_EXPIRED")
        task = json.loads(row["payload"])
        if validation_errors("task", task) or task["owner_id"] != owner_id or task["task_id"] != task_id or task["project_id"] != row["project_id"] or task["status"] != row["status"]:
            raise StoreError("STORAGE_UNAVAILABLE")
        return task

    def accept(self, *, owner_id, operation, request, selection=None, cached_report_id=None):
        """Commit one binding. Replays need no current selection/identity resolution.

        max_chars is presentation only. Missing intent/profile use the first
        effective binding; question/report_id remain actual request inputs.
        First-time selection is nonsecret resolved service data, not argv/keys.
        """
        _owner(owner_id)
        if operation not in ("project_context", "model_test"):
            raise StoreError("INVALID_ARGUMENT")
        if operation == "model_test" and (not isinstance(request, dict) or request.get("allow_model_request") is not True):
            raise StoreError("MODEL_TEST_CONSENT_REQUIRED")
        _validate("setup_model_test_request" if operation == "model_test" else "project_context_request", request)
        key = (owner_id, request["project_id"], operation, request["request_id"])
        with self._transaction(write=True) as connection:
            existing = connection.execute(
                "SELECT * FROM bindings WHERE owner_id=? AND project_id=? AND operation=? AND request_id=?", key).fetchone()
            if existing:
                if existing["kind"] == "report":
                    value = self._report(connection, owner_id, existing["object_id"])
                elif existing["kind"] == "task":
                    value = self._task(connection, owner_id, existing["object_id"])
                    if value["report_id"]:
                        self._report(connection, owner_id, value["report_id"])
                else:
                    raise StoreError("STORAGE_UNAVAILABLE")
                # Expired object/tombstone is checked before inputs, which may
                # have been compacted. An expired ID never becomes a new job.
                frozen = json.loads(existing["params"])
                if hashlib.sha256(_json(frozen).encode()).hexdigest() != existing["fingerprint"]:
                    raise StoreError("STORAGE_UNAVAILABLE")
                if self._params(operation, request, frozen=frozen) != frozen:
                    raise StoreError("REQUEST_CONFLICT")
                if existing["kind"] == "report":
                    report = value
                    if report["project_id"] != request["project_id"] or report["profile"] != frozen["profile"]:
                        raise StoreError("STORAGE_UNAVAILABLE")
                    return Acceptance("report", report, False)
                if existing["kind"] == "task":
                    task = value
                    if task["project_id"] != request["project_id"] or task["request_id"] != request["request_id"] or task["purpose"] != operation or task["profile"] != frozen["profile"]:
                        raise StoreError("STORAGE_UNAVAILABLE")
                    return Acceptance("task", task, False)
                raise StoreError("STORAGE_UNAVAILABLE")
            if cached_report_id is not None:
                if operation != "project_context":
                    raise StoreError("INVALID_ARGUMENT")
                report = self._report(connection, owner_id, cached_report_id)
                params = self._params(operation, request, profile=report["profile"])
                if report["project_id"] != request["project_id"] or params["profile"] != report["profile"]:
                    raise StoreError("REQUEST_CONFLICT")
                connection.execute("INSERT INTO bindings VALUES(?,?,?,?,?,?,?,?)", (*key, _json(params),
                    hashlib.sha256(_json(params).encode()).hexdigest(), "report", cached_report_id))
                return Acceptance("report", report, True)
            if not isinstance(selection, dict) or not {"profile", "requested_model", "config_revision", "execution_context"} <= selection.keys():
                raise StoreError("INVALID_ARGUMENT")
            if selection.keys() - {"profile", "requested_model", "config_revision", "execution_context", "backend_version",
                                   "parent_task_id", "continuation_type", "session_available"}:
                raise StoreError("INVALID_ARGUMENT")
            params = self._params(operation, request, profile=selection["profile"])
            if params["profile"] != selection["profile"]:
                raise StoreError("INVALID_ARGUMENT")
            context = selection["execution_context"]
            _validate("execution_context", context)
            if operation == "model_test" and request["expected_credential_revision"] != context["credential_revision"]:
                raise StoreError("CREDENTIAL_CONFLICT")
            created_at = _now()
            task = {"task_id": str(uuid.uuid4()), "project_id": request["project_id"], "request_id": request["request_id"],
                "owner_id": owner_id, "mode": "context", "backend": "codex", "profile": params["profile"],
                "backend_version": selection.get("backend_version"), "requested_model": selection["requested_model"],
                "resolved_model": None, "config_revision": selection["config_revision"],
                "parent_task_id": selection.get("parent_task_id"), "status": "queued", "created_at": created_at,
                "started_at": None, "finished_at": None, "finish_reason": None,
                "progress": {"phase": "queued", "message": "等待调度", "updated_at": created_at},
                "report_id": None, "summary": None, "completion": None, "verification": None,
                "continuation_type": selection.get("continuation_type", "fresh"),
                "session_available": selection.get("session_available"), "actual_models": None,
                "usage": None, "usage_scope": "unknown", "cost": None, "cost_basis": "unknown", "error": None,
                "execution_context": json.loads(_json(context)), "purpose": operation}
            _validate("task", task)
            self._probe_results()
            connection.execute("INSERT INTO tasks(task_id,owner_id,project_id,schema_version,status,payload) VALUES(?,?,?,?,?,?)",
                (task["task_id"], owner_id, task["project_id"], CONTRACT_VERSION, task["status"], _json(task)))
            connection.execute("INSERT INTO bindings VALUES(?,?,?,?,?,?,?,?)", (*key, _json(params),
                hashlib.sha256(_json(params).encode()).hexdigest(), "task", task["task_id"]))
            return Acceptance("task", task, True)

    def get_task(self, owner_id, task_id):
        _owner(owner_id)
        with self._transaction() as connection:
            return self._task(connection, owner_id, task_id)

    def get_task_input(self, owner_id, task_id):
        """Private dispatcher input, not part of the public MCP task projection."""
        _owner(owner_id)
        with self._transaction() as connection:
            task = self._task(connection, owner_id, task_id)
            if task["report_id"]:
                self._report(connection, owner_id, task["report_id"])
            row = connection.execute("SELECT * FROM bindings WHERE owner_id=? AND kind='task' AND object_id=?",
                (owner_id, task_id)).fetchone()
            if row is None or row["project_id"] != task["project_id"] or row["request_id"] != task["request_id"] or row["operation"] != task["purpose"]:
                raise StoreError("STORAGE_UNAVAILABLE")
            params = json.loads(row["params"])
            if hashlib.sha256(_json(params).encode()).hexdigest() != row["fingerprint"] or params["profile"] != task["profile"]:
                raise StoreError("STORAGE_UNAVAILABLE")
            return params

    def list_tasks(self, owner_id=None, *, statuses=None):
        """owner=None is privileged startup recovery, never a client parameter."""
        if owner_id is not None:
            _owner(owner_id)
        with self._transaction() as connection:
            rows = connection.execute("SELECT task_id,owner_id FROM tasks WHERE expired=0 ORDER BY rowid")
            return [task for row in rows if (owner_id is None or row["owner_id"] == owner_id)
                    if (task := self._task(connection, row["owner_id"], row["task_id"]))
                    if statuses is None or task["status"] in statuses]

    def replace_task(self, owner_id, task, *, expected_status):
        """Persist a T09-produced state using CAS; never choose a transition here."""
        _owner(owner_id)
        _validate("task", task)
        with self._transaction(write=True) as connection:
            previous = self._task(connection, owner_id, task["task_id"])
            immutable = ("task_id", "project_id", "request_id", "owner_id", "mode", "backend", "profile",
                "backend_version", "requested_model", "config_revision", "parent_task_id", "created_at",
                "continuation_type", "purpose", "report_id")
            context_keys = ("auth_mode", "billing_source", "provider", "endpoint", "credential_context_id",
                "credential_revision", "requested_reasoning_effort", "requested_service_tier", "selection_fingerprint")
            if previous["status"] != expected_status or any(task[k] != previous[k] for k in immutable) or any(
                    task["execution_context"][k] != previous["execution_context"][k] for k in context_keys):
                raise StoreError("REQUEST_CONFLICT")
            connection.execute("UPDATE tasks SET payload=?,status=? WHERE task_id=? AND owner_id=? AND status=?",
                (_json(task), task["status"], task["task_id"], owner_id, expected_status))
            return json.loads(_json(task))

    def _content_path(self, content_ref):
        if not isinstance(content_ref, str) or not content_ref or "\\" in content_ref or ":" in content_ref:
            raise StoreError("INVALID_ARGUMENT")
        reference = PurePosixPath(content_ref)
        if reference.is_absolute() or any(part in ("", ".", "..") for part in content_ref.split("/")):
            raise StoreError("INVALID_ARGUMENT")
        path = (self.results_dir / content_ref).resolve()
        if not path.is_relative_to(self.results_dir.resolve()) or not path.is_file():
            raise StoreError("RESULT_EXPIRED")
        return path

    def _content_matches(self, content_ref, expected_hash):
        digest = hashlib.sha256()
        with self._content_path(content_ref).open("rb") as body:
            for chunk in iter(lambda: body.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected_hash:
            raise StoreError("RESULT_EXPIRED")

    def _report_row(self, connection, owner_id, report_id):
        row = connection.execute("SELECT * FROM reports WHERE report_id=? AND owner_id=?", (report_id, owner_id)).fetchone()
        if row is None:
            raise StoreError("REPORT_NOT_FOUND")
        if row["schema_version"] != CONTRACT_VERSION:
            raise StoreError("SCHEMA_UNSUPPORTED")
        return row

    def _report(self, connection, owner_id, report_id):
        row = self._report_row(connection, owner_id, report_id)
        if row["expired"]:
            raise StoreError("RESULT_EXPIRED")
        report = json.loads(row["payload"])
        if validation_errors("report", report) or report["report_id"] != report_id or report["project_id"] != row["project_id"]:
            raise StoreError("STORAGE_UNAVAILABLE")
        self._content_matches(row["content_ref"], row["content_sha256"])
        return report

    def get_report(self, owner_id, report_id):
        """Public report projection; private body path/hash never enter its schema."""
        _owner(owner_id)
        with self._transaction() as connection:
            return self._report(connection, owner_id, report_id)

    def get_content_reference(self, owner_id, report_id):
        """Private T13 seam. Do not expose this reference through MCP responses."""
        _owner(owner_id)
        with self._transaction() as connection:
            self._report(connection, owner_id, report_id)
            row = self._report_row(connection, owner_id, report_id)
            return row["content_ref"], row["content_sha256"]

    def publish_report(self, owner_id, task_id, report, *, content_ref, content_sha256):
        """T13 must write/fsync/atomic-rename immutable body BEFORE this call.

        Publish metadata and task.report_id in one SQLite transaction. A crash
        before this commit can leave an unreferenced body, never an accepted
        reference to an incomplete body. Identical repeat publication is safe.
        """
        _owner(owner_id)
        if isinstance(report, dict) and report.get("report_schema_version") != CONTRACT_VERSION:
            raise StoreError("SCHEMA_UNSUPPORTED")
        _validate("report", report)
        if not isinstance(content_sha256, str) or len(content_sha256) != 64 or any(c not in "0123456789abcdef" for c in content_sha256):
            raise StoreError("INVALID_ARGUMENT")
        with self._transaction(write=True) as connection:
            task = self._task(connection, owner_id, task_id)
            if any(report[key] != task[key] for key in ("project_id", "config_revision", "backend", "backend_version", "profile", "execution_context")):
                raise StoreError("REQUEST_CONFLICT")
            if task["report_id"] not in (None, report["report_id"]):
                raise StoreError("REQUEST_CONFLICT")
            self._content_matches(content_ref, content_sha256)
            existing = connection.execute("SELECT * FROM reports WHERE report_id=?", (report["report_id"],)).fetchone()
            if existing:
                if existing["owner_id"] != owner_id or existing["task_id"] != task_id or existing["expired"] or existing["payload"] != _json(report) or existing["content_ref"] != content_ref or existing["content_sha256"] != content_sha256:
                    raise StoreError("REQUEST_CONFLICT")
            else:
                connection.execute("INSERT INTO reports(report_id,task_id,owner_id,project_id,schema_version,payload,content_ref,content_sha256) VALUES(?,?,?,?,?,?,?,?)",
                    (report["report_id"], task_id, owner_id, report["project_id"], CONTRACT_VERSION, _json(report), content_ref, content_sha256))
            task["report_id"] = report["report_id"]
            _validate("task", task)
            connection.execute("UPDATE tasks SET payload=? WHERE task_id=? AND owner_id=?", (_json(task), task_id, owner_id))
            return json.loads(_json(report))

    def expire_report(self, owner_id, report_id):
        """T13 cleanup marks the tombstone BEFORE deleting its body; keeps all bindings."""
        _owner(owner_id)
        with self._transaction(write=True) as connection:
            report = self._report_row(connection, owner_id, report_id)
            connection.execute("UPDATE reports SET expired=1,payload=NULL,content_ref=NULL,content_sha256=NULL WHERE report_id=? AND owner_id=?",
                (report_id, owner_id))
            connection.execute("UPDATE bindings SET params='{}' WHERE owner_id=? AND ((kind='report' AND object_id=?) OR (kind='task' AND object_id=?))",
                (owner_id, report_id, report["task_id"]))

    def expire_task(self, owner_id, task_id):
        """Retain the durable idempotency binding when terminal task metadata is cleaned."""
        _owner(owner_id)
        with self._transaction(write=True) as connection:
            row = connection.execute("SELECT * FROM tasks WHERE task_id=? AND owner_id=?", (task_id, owner_id)).fetchone()
            if row is None:
                raise StoreError("TASK_NOT_FOUND")
            if row["schema_version"] != CONTRACT_VERSION:
                raise StoreError("SCHEMA_UNSUPPORTED")
            if row["expired"]:
                return
            task = self._task(connection, owner_id, task_id)
            if task["status"] not in ("succeeded", "failed", "cancelled", "timed_out", "interrupted"):
                raise StoreError("POLICY_DENIED")
            connection.execute("UPDATE tasks SET expired=1,payload=NULL WHERE task_id=? AND owner_id=?", (task_id, owner_id))
            connection.execute("UPDATE bindings SET params='{}' WHERE owner_id=? AND kind='task' AND object_id=?", (owner_id, task_id))
