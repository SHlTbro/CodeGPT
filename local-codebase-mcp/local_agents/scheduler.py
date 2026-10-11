"""Single-database admission and monotonic scheduling for trusted services.

Executor.start(task, private_input) must return after owned launch, never wait
for model completion. request_stop and cleanup_confirmed are T10 seams, not
client assertions. This module chooses no credentials and mutates no env.
"""
from dataclasses import dataclass
from datetime import datetime, timezone

from local_agents.store import StoreError, Acceptance

TERMINAL = frozenset({"succeeded", "failed", "cancelled", "timed_out", "interrupted"})
ACTIVE = frozenset({"running", "cancelling"})
TRANSITIONS = {"queued": {"running", "cancelled", "interrupted"},
               "running": TERMINAL | {"cancelling"}, "cancelling": TERMINAL | {"cancelling"}}


class SchedulerError(StoreError):
    def __init__(self, code):
        super().__init__(code)
        self.retriable = code in {"QUEUE_FULL", "STORAGE_UNAVAILABLE"}


@dataclass(frozen=True)
class Cancellation:
    task: dict
    cancel_requested: bool


def _error(code, task_id):
    return {"code": code, "message": "受管执行等待清理确认。", "recoverable": True,
            "hint": "确认所属进程清理后再发布终态。", "task_id": task_id, "retriable": False}


class Scheduler:
    def __init__(self, store, executor, *, enabled=False, max_running=None, max_queued=None):
        self.store, self.executor = store, executor
        with store.atomic():
            control = store.get_scheduler_control()
            if control is None:
                store.set_scheduler_control({"enabled": enabled,
                    "max_running": 2 if max_running is None else max_running,
                    "max_queued": 20 if max_queued is None else max_queued})
            elif any(value is not None and value != control[key] for key, value in (
                    ("max_running", max_running), ("max_queued", max_queued))):
                raise SchedulerError("REQUEST_CONFLICT")

    def submit(self, *, owner_id, operation, request, selection=None, cached_report_id=None, session_resource=None):
        """Rollback acceptance on admission failure; replays need no new slot.

        All service consumers must submit here, rather than Store.accept.
        session_resource is a trusted opaque T11 key, never a client argument.
        """
        direct = None
        with self.store.atomic():
            active = self.store.list_tasks(statuses=ACTIVE | {"queued"})
            resources = self._resources(active)
            accepted = self.store.accept(owner_id=owner_id, operation=operation, request=request,
                selection=selection, cached_report_id=cached_report_id)
            if accepted.kind != "task":
                return accepted
            task_id = accepted.value["task_id"]
            if not accepted.created:
                if accepted.value["status"] not in TERMINAL:
                    resources = self._resources([accepted.value])
                if session_resource is not None and resources.get(task_id, {}).get("session_resource") != session_resource:
                    raise SchedulerError("REQUEST_CONFLICT")
                return accepted
            control = self.store.get_scheduler_control()
            if not control["enabled"]:
                raise SchedulerError("CAPABILITY_DISABLED")
            self.store.set_scheduler_resource(owner_id, task_id, session_resource=session_resource)
            if control["max_queued"] == 0:
                running = [task for task in active if task["status"] in ACTIVE]
                if len(running) >= control["max_running"] or any(task["project_id"] == accepted.value["project_id"] or (
                        session_resource is not None and resources[task["task_id"]]["session_resource"] == session_resource) for task in running):
                    raise SchedulerError("QUEUE_FULL")
                direct = self._persist(accepted.value, "running")
            elif len(self.store.list_tasks(statuses={"queued"})) > control["max_queued"]:
                raise SchedulerError("QUEUE_FULL")
        if direct is not None:
            self._launch(direct)
            return Acceptance("task", self.store.get_task(owner_id, direct["task_id"]), True)
        return accepted

    def _resources(self, tasks):
        return self.store.get_scheduler_resources(required_task_ids=[task["task_id"] for task in tasks])

    def _persist(self, task, status, *, error=None):
        previous = task["status"]
        if status not in TRANSITIONS.get(previous, ()):
            raise SchedulerError("REQUEST_CONFLICT")
        now = datetime.now(timezone.utc).isoformat()
        task["status"] = status
        phase = "finished" if status in TERMINAL else "starting" if status == "running" else "cleaning"
        task["progress"] = {"phase": phase, "message": "状态已持久化", "updated_at": now}
        task["error"] = error
        if status == "running":
            task["started_at"] = now
        if status in TERMINAL:
            task["finished_at"] = now
            task["finish_reason"] = {"succeeded": "completed", "failed": "EXECUTION_FAILED",
                "cancelled": "cancelled", "timed_out": "timeout", "interrupted": "interrupted"}[status]
        return self.store.replace_task(task["owner_id"], task, expected_status=previous)

    def _pending(self, task, outcome):
        resource = self._resources([task])[task["task_id"]]
        self.store.set_scheduler_resource(task["owner_id"], task["task_id"],
            session_resource=resource.get("session_resource"), pending_terminal=outcome)

    def dispatch(self):
        """Claim/commit before launch; OFF and launch serialize on SQLite.

        Claimed running tasks are never dispatched again after a callback or
        storage failure. Startup consumer must call recover before dispatch.
        """
        launched = []
        while True:
            with self.store.atomic():
                control = self.store.get_scheduler_control()
                tasks = self.store.list_tasks()
                active = [t for t in tasks if t["status"] in ACTIVE]
                if not control["enabled"] or len(active) >= control["max_running"]:
                    return launched
                resources = self._resources([t for t in tasks if t["status"] in ACTIVE | {"queued"}])
                projects = {t["project_id"] for t in active}
                sessions = {resources[t["task_id"]]["session_resource"] for t in active} - {None}
                candidate = next((t for t in tasks if t["status"] == "queued" and t["project_id"] not in projects
                    and resources[t["task_id"]]["session_resource"] not in sessions), None)
                if candidate is None:
                    return launched
                task = self._persist(candidate, "running")
            if self._launch(task):
                launched.append(task["task_id"])

    def _launch(self, task):
        # Both zero-queue admission and dispatch commit their claim first.
        with self.store.atomic():
            current = self.store.get_task(task["owner_id"], task["task_id"])
            if not self.store.get_scheduler_control()["enabled"] or current["status"] != "running":
                return False
            self._resources(self.store.list_tasks(statuses=ACTIVE | {"queued"}))
            task_input = self.store.get_task_input(task["owner_id"], task["task_id"])
            try:
                self.executor.start(current, task_input)
                return True
            except Exception:
                self._pending(current, "failed")
                self._persist(current, "cancelling", error=_error("CLEANUP_FAILED", task["task_id"]))
                self._stop(task["owner_id"], task["task_id"])
                self.finish(task["owner_id"],task["task_id"],"failed")
                return False

    def _stop(self, owner_id, task_id):
        try:
            self.executor.request_stop(task_id)
        except Exception:
            with self.store.atomic():
                task = self.store.get_task(owner_id, task_id)
                if task["status"] == "cancelling":
                    self._persist(task, "cancelling", error=_error("CLEANUP_FAILED", task_id))

    def cancel(self, owner_id, task_id):
        with self.store.atomic():
            task = self.store.get_task(owner_id, task_id)
            if task["status"] in TERMINAL:
                return Cancellation(task, False)
            self._resources([task])
            if task["status"] == "queued":
                return Cancellation(self._persist(task, "cancelled"), True)
            self._pending(task, "cancelled")
            task = self._persist(task, "cancelling")
        self._stop(owner_id, task_id)
        return Cancellation(task, True)

    def finish(self, owner_id, task_id, outcome):
        """A trusted result still requires executor-owned cleanup confirmation."""
        if outcome not in TERMINAL:
            raise SchedulerError("INVALID_ARGUMENT")
        with self.store.atomic():
            task = self.store.get_task(owner_id, task_id)
            if task["status"] in TERMINAL:
                return False
            if task["status"] == "queued":
                raise SchedulerError("REQUEST_CONFLICT")
            pending = self._resources([task])[task_id]["pending_terminal"]
            if task["status"] == "cancelling" and pending is not None:
                outcome = pending
            try:
                cleaned = self.executor.cleanup_confirmed(task_id) is True
            except Exception:
                cleaned = False
            if not cleaned:
                self._pending(task, outcome)
                self._persist(task, "cancelling", error=_error("CLEANUP_FAILED", task_id))
                return False
            self._persist(task, outcome)
            return True

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise SchedulerError("INVALID_ARGUMENT")
        stops = []
        with self.store.atomic():
            control = self.store.get_scheduler_control()
            control["enabled"] = enabled
            self.store.set_scheduler_control(control)
            if not enabled:
                tasks = self.store.list_tasks(statuses={"queued", "running", "cancelling"})
                self._resources(tasks)
                for task in tasks:
                    if task["status"] == "queued":
                        self._persist(task, "cancelled")
                    else:
                        self._pending(task, "cancelled")
                        self._persist(task, "cancelling")
                        stops.append((task["owner_id"], task["task_id"]))
        for owner_id, task_id in stops:
            self._stop(owner_id, task_id)

    def recover(self):
        """Exclusive service-start recovery: never replay accepted work.

        T19 owns exclusive startup; T10 must recover owned handles to prove
        cleanup. Merely opening a second Scheduler does not imply a restart.
        """
        stops = []
        with self.store.atomic():
            tasks = self.store.list_tasks(statuses={"queued", "running", "cancelling"})
            # Legacy layout-1 accepted queue had no resource bindings. It is
            # interrupted before checking active locks, never auto-dispatched.
            tasks.sort(key=lambda task: task["status"] != "queued")
            for task in tasks:
                if task["status"] == "queued":
                    self._persist(task, "interrupted")
                else:
                    existing = self._resources([task])[task["task_id"]]["pending_terminal"]
                    self._pending(task, existing or "interrupted")
                    self._persist(task, "cancelling", error=_error("CLEANUP_FAILED", task["task_id"]))
                    stops.append((task["owner_id"], task["task_id"]))
        for owner_id, task_id in stops:
            self._stop(owner_id, task_id)
            # Startup Runner.recover may already have proved the owned process
            # tree is gone. Finalize only through finish(), which rechecks that
            # proof; otherwise keep the task cancelling and never replay it.
            self.finish(owner_id, task_id, "interrupted")
