"""Real SQLite scheduling; executor is a controlled fixture, never a CLI."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import os
import threading
import subprocess
import sys
from pathlib import Path
from contextlib import contextmanager

import pytest

from local_agents.store import Store
from local_agents.scheduler import Scheduler, SchedulerError


def selection(auth="chatgpt"):
    return {"profile": "reader", "requested_model": "fixture-model", "config_revision": "a" * 64,
        "execution_context": {"auth_mode": auth, "billing_source": "chatgpt_plan" if auth == "chatgpt" else "openai_api",
            "provider": "openai", "endpoint": None if auth == "chatgpt" else "https://api.openai.com/v1",
            "credential_context_id": "fixture-" + auth, "credential_revision": "cred-r1",
            "requested_reasoning_effort": "high", "resolved_reasoning_effort": None,
            "requested_service_tier": "standard", "resolved_service_tier": None,
            "selection_fingerprint": "b" * 64, "effective_auth_verified": False}}


class ControlledExecutor:
    """Records trusted callbacks; cleanup proof comes only from this fixture."""
    def __init__(self):
        self.started = []
        self.stopped = []
        self.cleaned = set()
        self.fail_start = False
        self.lock = threading.Lock()

    def start(self, task, task_input):
        with self.lock:
            self.started.append((deepcopy(task), deepcopy(task_input)))
        if self.fail_start:
            raise RuntimeError("controlled start failure")

    def request_stop(self, task_id):
        with self.lock:
            self.stopped.append(task_id)

    def cleanup_confirmed(self, task_id):
        return task_id in self.cleaned


def make(tmp_path, executor=None, **limits):
    store = Store(tmp_path / "tasks.sqlite", tmp_path / "results")
    executor = executor or ControlledExecutor()
    return Scheduler(store, executor, enabled=True, **limits), store, executor


def submit(scheduler, i=1, project=None, session=None, auth="chatgpt"):
    return scheduler.submit(owner_id="owner", operation="project_context",
        request={"project_id": project or f"project-{i}", "request_id": f"request-{i}"},
        selection=selection(auth), session_resource=session)


def test_acceptance_is_committed_before_start_and_replay_never_dispatches(tmp_path):
    scheduler, store, executor = make(tmp_path)
    first = submit(scheduler)
    assert first.created and first.value["status"] == "queued"
    assert executor.started == []
    assert scheduler.dispatch() == [first.value["task_id"]]
    reopened = Store(store.database_path, store.results_dir)
    assert reopened.get_task("owner", first.value["task_id"])["status"] == "running"
    assert not submit(scheduler).created
    assert scheduler.dispatch() == []
    assert len(executor.started) == 1


def test_two_instances_atomically_bound_capacity_and_queue(tmp_path):
    scheduler, store, executor = make(tmp_path, max_running=2, max_queued=3)
    second = Scheduler(Store(store.database_path, store.results_dir), executor, max_running=2, max_queued=3)
    barrier = threading.Barrier(8)
    def accept(i):
        barrier.wait()
        try:
            return submit(scheduler if i % 2 else second, i)
        except SchedulerError as error:
            assert error.code == "QUEUE_FULL" and error.retriable
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        accepted = list(pool.map(accept, range(8)))
    assert sum(item is not None for item in accepted) == 3
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda s: s.dispatch(), [scheduler, second]))
    assert len(executor.started) == 2
    assert len(store.list_tasks(statuses={"queued"})) == 1
    assert len(store.list_tasks(statuses={"running"})) == 2


def test_project_and_private_session_are_exclusive_across_instances(tmp_path):
    scheduler, store, executor = make(tmp_path, max_running=4)
    second = Scheduler(Store(store.database_path, store.results_dir), executor, max_running=4)
    a = submit(scheduler, 1, project="same", session="opaque-session-resource")
    b = submit(second, 2, project="same")
    c = submit(second, 3, session="opaque-session-resource")
    d = submit(second, 4)
    scheduler.dispatch()
    second.dispatch()
    assert {t["task_id"] for t, _ in executor.started} == {a.value["task_id"], d.value["task_id"]}
    assert store.get_task("owner", b.value["task_id"])["status"] == "queued"
    assert store.get_task("owner", c.value["task_id"])["status"] == "queued"
    assert "opaque-session-resource" not in str(store.get_task("owner", a.value["task_id"]))


def test_cancel_holds_locks_until_executor_cleanup_proof(tmp_path):
    scheduler, store, executor = make(tmp_path)
    a = submit(scheduler, 1, project="same")
    b = submit(scheduler, 2, project="same")
    scheduler.dispatch()
    result = scheduler.cancel("owner", a.value["task_id"])
    assert result.cancel_requested and result.task["status"] == "cancelling"
    assert not scheduler.finish("owner", a.value["task_id"], "succeeded")
    held = store.get_task("owner", a.value["task_id"])
    assert held["status"] == "cancelling" and held["error"]["code"] == "CLEANUP_FAILED"
    assert scheduler.dispatch() == []
    executor.cleaned.add(a.value["task_id"])
    assert scheduler.finish("owner", a.value["task_id"], "succeeded")
    assert store.get_task("owner", a.value["task_id"])["status"] == "cancelled"
    assert scheduler.dispatch() == [b.value["task_id"]]


def test_cancel_and_success_race_has_one_durable_terminal(tmp_path):
    scheduler, store, executor = make(tmp_path)
    a = submit(scheduler)
    scheduler.dispatch()
    task_id = a.value["task_id"]
    executor.cleaned.add(task_id)
    second = Scheduler(Store(store.database_path, store.results_dir), executor)
    barrier = threading.Barrier(2)
    def cancel():
        barrier.wait()
        return scheduler.cancel("owner", task_id)
    def success():
        barrier.wait()
        return second.finish("owner", task_id, "succeeded")
    with ThreadPoolExecutor(max_workers=2) as pool:
        c, f = pool.submit(cancel), pool.submit(success)
        cancelled, _ = c.result(), f.result()
    scheduler.finish("owner", task_id, "succeeded")
    final = store.get_task("owner", task_id)
    assert final["status"] == ("cancelled" if cancelled.cancel_requested else "succeeded")
    snapshot = deepcopy(final)
    assert not scheduler.finish("owner", task_id, "failed")
    assert not scheduler.cancel("owner", task_id).cancel_requested
    assert store.get_task("owner", task_id) == snapshot


def test_off_cancels_queue_and_blocks_all_instances_before_start(tmp_path):
    scheduler, store, executor = make(tmp_path, max_running=1)
    a, b = submit(scheduler, 1), submit(scheduler, 2)
    scheduler.dispatch()
    second = Scheduler(Store(store.database_path, store.results_dir), executor, max_running=1)
    scheduler.set_enabled(False)
    assert store.get_task("owner", b.value["task_id"])["status"] == "cancelled"
    assert store.get_task("owner", a.value["task_id"])["status"] == "cancelling"
    assert second.dispatch() == []
    with pytest.raises(SchedulerError) as error:
        submit(second, 3)
    assert error.value.code == "CAPABILITY_DISABLED"
    assert len(executor.started) == 1
    assert len(store.list_tasks()) == 2


def test_start_failure_is_not_replayed_and_retains_cleanup_claim(tmp_path):
    scheduler, store, executor = make(tmp_path)
    executor.fail_start = True
    a = submit(scheduler)
    scheduler.dispatch()
    assert store.get_task("owner", a.value["task_id"])["status"] == "cancelling"
    executor.fail_start = False
    second = Scheduler(Store(store.database_path, store.results_dir), executor)
    assert second.dispatch() == []
    assert not submit(second).created
    assert len(executor.started) == 1
    executor.cleaned.add(a.value["task_id"])
    assert second.finish("owner", a.value["task_id"], "failed")
    assert store.get_task("owner", a.value["task_id"])["status"] == "failed"


def test_auth_context_is_independent_and_process_environment_is_untouched(tmp_path):
    scheduler, store, executor = make(tmp_path)
    before = dict(os.environ)
    a, b = submit(scheduler, 1), submit(scheduler, 2, auth="api_key")
    scheduler.dispatch()
    assert dict(os.environ) == before
    contexts = {t["task_id"]: t["execution_context"] for t, _ in executor.started}
    assert contexts[a.value["task_id"]]["auth_mode"] == "chatgpt"
    assert contexts[b.value["task_id"]]["auth_mode"] == "api_key"
    contexts[a.value["task_id"]]["credential_revision"] = "changed-fixture"
    assert store.get_task("owner", a.value["task_id"])["execution_context"]["credential_revision"] == "cred-r1"
    assert contexts[b.value["task_id"]]["credential_revision"] == "cred-r1"


def test_default_off_rejects_zero_acceptance_but_existing_replay_survives_off(tmp_path):
    store = Store(tmp_path / "tasks.sqlite", tmp_path / "results")
    scheduler = Scheduler(store, ControlledExecutor())
    with pytest.raises(SchedulerError) as error:
        submit(scheduler)
    assert error.value.code == "CAPABILITY_DISABLED"
    assert store.list_tasks() == []
    scheduler.set_enabled(True)
    first = submit(scheduler)
    scheduler.set_enabled(False)
    assert submit(scheduler).value["task_id"] == first.value["task_id"]


def test_executor_sees_committed_running_and_recovery_does_not_replay(tmp_path):
    scheduler, store, executor = make(tmp_path, max_running=1)
    a, b = submit(scheduler, 1), submit(scheduler, 2)
    original = executor.start
    def verified_start(task, task_input):
        # Separate connection reads claim before the controlled external effect.
        reopened = Store.__new__(Store)
        reopened.database_path, reopened.results_dir = store.database_path, store.results_dir
        reopened._local = threading.local()
        assert reopened.get_task("owner", task["task_id"])["status"] == "running"
        original(task, task_input)
    executor.start = verified_start
    scheduler.dispatch()
    assert len(executor.started) == 1
    second = Scheduler(Store(store.database_path, store.results_dir), executor)
    second.recover()
    assert store.get_task("owner", a.value["task_id"])["status"] == "cancelling"
    assert store.get_task("owner", b.value["task_id"])["status"] == "interrupted"
    assert second.dispatch() == []
    executor.cleaned.add(a.value["task_id"])
    assert second.finish("owner", a.value["task_id"], "succeeded")
    assert store.get_task("owner", a.value["task_id"])["status"] == "interrupted"


def test_recovery_releases_slot_when_owned_cleanup_is_already_proven(tmp_path):
    scheduler, store, executor = make(tmp_path, max_running=1)
    running, queued = submit(scheduler, 1), submit(scheduler, 2)
    scheduler.dispatch()
    running_id = running.value["task_id"]
    assert [item[0]["task_id"] for item in executor.started] == [running_id]

    # Model Runner.recover() proving that the old process tree and Job are gone.
    executor.cleaned.add(running_id)
    reopened = Scheduler(Store(store.database_path, store.results_dir), executor)
    reopened.recover()

    assert store.get_task("owner", running_id)["status"] == "interrupted"
    assert store.get_task("owner", queued.value["task_id"])["status"] == "interrupted"
    assert reopened.dispatch() == []
    assert [item[0]["task_id"] for item in executor.started] == [running_id]


def test_recovery_preserves_cancel_intent_after_cleanup_is_proven(tmp_path):
    scheduler, store, executor = make(tmp_path)
    first = submit(scheduler)
    scheduler.dispatch()
    task_id = first.value["task_id"]
    scheduler.cancel("owner", task_id)
    executor.cleaned.add(task_id)

    reopened = Scheduler(Store(store.database_path, store.results_dir), executor)
    reopened.recover()

    assert store.get_task("owner", task_id)["status"] == "cancelled"
    assert [item[0]["task_id"] for item in executor.started] == [task_id]


def test_off_races_admission_and_launch_without_any_after_off_start(tmp_path):
    scheduler, store, executor = make(tmp_path, max_running=4)
    for i in range(4):
        submit(scheduler, i)
    launched_entered, permit_return, off_done = threading.Event(), threading.Event(), threading.Event()
    original = executor.start
    def blocked_start(task, task_input):
        assert not off_done.is_set()
        original(task, task_input)
        launched_entered.set()
        assert permit_return.wait(5)
    executor.start = blocked_start
    second = Scheduler(Store(store.database_path, store.results_dir), executor)
    with ThreadPoolExecutor(max_workers=2) as pool:
        dispatch = pool.submit(scheduler.dispatch)
        assert launched_entered.wait(5)
        def off():
            second.set_enabled(False)
            off_done.set()
        disable = pool.submit(off)
        permit_return.set()
        disable.result(timeout=10)
        dispatch.result(timeout=10)
    starts_at_off = len(executor.started)
    scheduler.dispatch()
    assert len(executor.started) == starts_at_off
    assert not store.list_tasks(statuses={"queued", "running"})


def _worker(tmp_path, code, *args):
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = os.pathsep.join([str(Path(__file__).parent), env.get("PYTHONPATH", "")])
    return subprocess.Popen([sys.executable, "-B", "-c", code, str(tmp_path), *map(str, args)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def test_two_real_python_processes_share_sqlite_admission_and_claims(tmp_path):
    scheduler, store, executor = make(tmp_path, max_running=2, max_queued=3)
    code = '''
import sys, time
from pathlib import Path
from test_agent_scheduler import ControlledExecutor, submit
from local_agents.store import Store
from local_agents.scheduler import Scheduler, SchedulerError
p=Path(sys.argv[1]); index=int(sys.argv[2]); phase=sys.argv[3]
s=Scheduler(Store(p/'tasks.sqlite',p/'results'), ControlledExecutor())
(p/(phase+'-ready-'+str(index))).write_text('ready')
deadline=time.monotonic()+10
while not (p/(phase+'-go')).exists():
    if time.monotonic()>deadline: raise RuntimeError('fixture barrier timeout')
    time.sleep(.01)
if phase=='accept':
    for i in range(index*4,index*4+4):
        try: submit(s,i)
        except SchedulerError as e:
            if e.code!='QUEUE_FULL': raise
else:
    s.dispatch()
    for task,_ in s.executor.started:
        with (p/'starts.txt').open('a',encoding='utf8') as f: f.write(task['task_id']+'\\n')
'''
    import time
    for phase in ("accept", "dispatch"):
        workers = [_worker(tmp_path, code, i, phase) for i in range(2)]
        deadline = time.monotonic() + 15
        while not all((tmp_path / f"{phase}-ready-{i}").exists() for i in range(2)):
            assert time.monotonic() < deadline
            time.sleep(.02)
        (tmp_path / f"{phase}-go").write_text("go")
        for worker in workers:
            stdout, stderr = worker.communicate(timeout=20)
            assert worker.returncode == 0, stdout + stderr
    assert len(store.list_tasks()) == 3
    assert len(store.list_tasks(statuses={"running"})) == 2
    starts = (tmp_path / "starts.txt").read_text().splitlines()
    assert len(starts) == len(set(starts)) == 2


def test_actual_worker_exit_after_claim_never_replays_that_task(tmp_path):
    scheduler, store, executor = make(tmp_path)
    first = submit(scheduler)
    code = '''
import sys, os
from pathlib import Path
from test_agent_scheduler import ControlledExecutor
from local_agents.store import Store
from local_agents.scheduler import Scheduler
class Crash(ControlledExecutor):
    def start(self,task,task_input): os._exit(73)
p=Path(sys.argv[1]); Scheduler(Store(p/'tasks.sqlite',p/'results'),Crash()).dispatch()
'''
    worker = _worker(tmp_path, code)
    worker.communicate(timeout=15)
    assert worker.returncode == 73
    assert store.get_task("owner", first.value["task_id"])["status"] == "running"
    scheduler.recover()
    assert scheduler.dispatch() == [] and executor.started == []
    assert not submit(scheduler).created


def test_stop_failure_is_visible_and_keeps_project_claim(tmp_path):
    scheduler, store, executor = make(tmp_path)
    a = submit(scheduler, 1, project="same")
    submit(scheduler, 2, project="same")
    scheduler.dispatch()
    def failed_stop(task_id):
        raise RuntimeError("fixture cleanup failure")
    executor.request_stop = failed_stop
    scheduler.cancel("owner", a.value["task_id"])
    assert store.get_task("owner", a.value["task_id"])["error"]["code"] == "CLEANUP_FAILED"
    assert scheduler.dispatch() == []


@pytest.mark.parametrize("queue_limit", [0, 20])
def test_off_accepted_in_gap_after_durable_claim_prevents_launch(tmp_path, queue_limit):
    claimed, continue_launch = threading.Event(), threading.Event()
    class PausedStore(Store):
        armed = False
        @contextmanager
        def atomic(self):
            with super().atomic():
                yield self
            if self.armed and not claimed.is_set() and self.list_tasks(statuses={"running"}):
                claimed.set()
                assert continue_launch.wait(5)
    store = PausedStore(tmp_path / "tasks.sqlite", tmp_path / "results")
    executor = ControlledExecutor()
    scheduler = Scheduler(store, executor, enabled=True, max_queued=queue_limit)
    if queue_limit:
        submit(scheduler)
    second_store = Store(store.database_path, store.results_dir)
    second = Scheduler(second_store, executor)
    store.armed = True
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(scheduler.dispatch if queue_limit else lambda: submit(scheduler))
        assert claimed.wait(5)
        task_id = second_store.list_tasks()[0]["task_id"]
        assert second_store.get_task("owner", task_id)["status"] == "running"
        second.set_enabled(False)  # OFF accepted only when this method returns.
        assert second_store.get_scheduler_control()["enabled"] is False
        continue_launch.set()
        pending.result(timeout=10)
    assert executor.started == []
    assert second_store.get_task("owner", task_id)["status"] == "cancelling"


def test_storage_refuses_claim_before_any_executor_side_effect(tmp_path):
    import sqlite3
    from local_agents.store import StoreError
    scheduler, store, executor = make(tmp_path)
    first = submit(scheduler)
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("""CREATE TRIGGER deny_claim BEFORE UPDATE OF status ON tasks
            WHEN NEW.status='running' BEGIN SELECT RAISE(ABORT,'controlled storage failure'); END""")
    with pytest.raises(StoreError) as error:
        scheduler.dispatch()
    assert error.value.code == "STORAGE_UNAVAILABLE"
    assert executor.started == []
    assert store.get_task("owner", first.value["task_id"])["status"] == "queued"
    # Service recovery deliberately interrupts accepted queue; it does not retry.
    scheduler.recover()
    assert not submit(scheduler).created
    assert scheduler.dispatch() == []
    assert store.get_task("owner", first.value["task_id"])["status"] == "interrupted"


@pytest.mark.parametrize("corruption", ["pending_queued", "blob_session", "missing_resource", "missing_queued_resource", "orphan_resource"])
def test_corrupt_private_resource_fails_closed_without_relaunch_or_release(tmp_path, corruption):
    import sqlite3
    from local_agents.store import StoreError
    scheduler, store, executor = make(tmp_path)
    a = submit(scheduler, 1, session="same-resource")
    b = submit(scheduler, 2, session="same-resource")
    scheduler.dispatch()
    task_id = a.value["task_id"]
    executor.cleaned.add(task_id)
    with sqlite3.connect(store.database_path) as connection:
        if corruption == "pending_queued":
            connection.execute("UPDATE scheduler_resources SET pending_terminal='queued' WHERE task_id=?", (task_id,))
        elif corruption == "blob_session":
            connection.execute("UPDATE scheduler_resources SET session_resource=? WHERE task_id=?", (sqlite3.Binary(b"same-resource"), task_id))
        elif corruption == "missing_resource":
            connection.execute("DELETE FROM scheduler_resources WHERE task_id=?", (task_id,))
        elif corruption == "missing_queued_resource":
            connection.execute("DELETE FROM scheduler_resources WHERE task_id=?", (b.value["task_id"],))
        else:
            connection.execute("INSERT INTO scheduler_resources VALUES('orphan-task','opaque-resource',NULL)")
    with pytest.raises(StoreError) as error:
        scheduler.finish("owner", task_id, "succeeded")
    assert error.value.code == "STORAGE_UNAVAILABLE"
    with pytest.raises(StoreError):
        scheduler.dispatch()
    assert len(executor.started) == 1
    assert store.get_task("owner", task_id)["status"] == "running"
    assert store.get_task("owner", b.value["task_id"])["status"] == "queued"


@pytest.mark.parametrize("before,after", [("running", "queued"), ("cancelling", "queued"),
    ("succeeded", "running"), ("cancelled", "succeeded")])
def test_central_transition_table_rejects_regression_even_when_schema_allows_it(tmp_path, before, after):
    scheduler, store, executor = make(tmp_path)
    first = submit(scheduler)
    scheduler.dispatch()
    task_id = first.value["task_id"]
    if before == "cancelling":
        scheduler.cancel("owner", task_id)
    elif before in {"succeeded", "cancelled"}:
        if before == "cancelled":
            scheduler.cancel("owner", task_id)
        executor.cleaned.add(task_id)
        scheduler.finish("owner", task_id, "succeeded")
    task = store.get_task("owner", task_id)
    with pytest.raises(SchedulerError) as error:
        scheduler._persist(task, after)
    assert error.value.code == "REQUEST_CONFLICT"
    assert store.get_task("owner", task_id)["status"] == before


def test_zero_wait_queue_admits_only_free_conflictless_slots_and_replays_once(tmp_path):
    scheduler, store, executor = make(tmp_path, max_running=2, max_queued=0)
    first = submit(scheduler, 1, session="same-resource")
    assert first.value["status"] == "running" and len(executor.started) == 1
    for i, project, session in [(2, "project-1", None), (3, "other", "same-resource")]:
        with pytest.raises(SchedulerError) as error:
            submit(scheduler, i, project=project, session=session)
        assert error.value.code == "QUEUE_FULL"
    second = submit(scheduler, 4)
    assert second.value["status"] == "running"
    with pytest.raises(SchedulerError) as error:
        submit(scheduler, 5)
    assert error.value.code == "QUEUE_FULL"
    assert not submit(scheduler, 1, session="same-resource").created
    assert len(executor.started) == 2 and len(store.list_tasks()) == 2
    assert store.list_tasks(statuses={"queued"}) == []


def test_zero_wait_start_failure_keeps_accepted_claim_and_never_replays(tmp_path):
    scheduler, store, executor = make(tmp_path, max_queued=0)
    executor.fail_start = True
    first = submit(scheduler)
    assert first.value["status"] == "cancelling"
    assert not submit(scheduler).created
    scheduler.dispatch()
    assert len(executor.started) == 1


def test_zero_wait_storage_rejects_acceptance_before_launch(tmp_path):
    import sqlite3
    from local_agents.store import StoreError
    scheduler, store, executor = make(tmp_path, max_queued=0)
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("CREATE TRIGGER reject_binding BEFORE INSERT ON bindings BEGIN SELECT RAISE(ABORT,'fixture storage error'); END")
    with pytest.raises(StoreError) as error:
        submit(scheduler)
    assert error.value.code == "STORAGE_UNAVAILABLE"
    assert store.list_tasks() == [] and executor.started == []


def test_zero_wait_cached_report_binding_never_starts_executor(tmp_path):
    import hashlib
    from test_agent_store import report
    scheduler, store, executor = make(tmp_path, max_queued=0)
    first = submit(scheduler)
    body = store.results_dir / "fixture-report.json"
    body.write_bytes(b'{"fixture":"controlled report"}')
    metadata = report(first.value)
    store.publish_report("owner", first.value["task_id"], metadata, content_ref=body.name,
        content_sha256=hashlib.sha256(body.read_bytes()).hexdigest())
    cached = scheduler.submit(owner_id="owner", operation="project_context",
        request={"project_id": "project-1", "request_id": "cached-request"}, cached_report_id=metadata["report_id"])
    assert cached.kind == "report" and cached.created
    assert len(executor.started) == 1 and len(store.list_tasks()) == 1


def test_unbound_legacy_queue_is_never_dispatched_and_recovery_interrupts_it(tmp_path):
    from local_agents.store import StoreError
    store = Store(tmp_path / "tasks.sqlite", tmp_path / "results")
    legacy = store.accept(owner_id="owner", operation="project_context",
        request={"project_id": "project-1", "request_id": "request-1"}, selection=selection())
    executor = ControlledExecutor()
    scheduler = Scheduler(store, executor, enabled=True)
    with pytest.raises(StoreError) as error:
        scheduler.dispatch()
    assert error.value.code == "STORAGE_UNAVAILABLE" and executor.started == []
    scheduler.recover()
    assert store.get_task("owner", legacy.value["task_id"])["status"] == "interrupted"
    assert not submit(scheduler).created
    assert scheduler.dispatch() == []


@pytest.mark.parametrize("phase,pending", [("queued", "succeeded"), ("running", "succeeded"),
    ("failed", "succeeded"), ("cancelled", "failed")])
def test_private_pending_must_match_task_phase_and_cannot_invent_success(tmp_path, phase, pending):
    import sqlite3
    from local_agents.store import StoreError
    scheduler, store, executor = make(tmp_path)
    first = submit(scheduler)
    task_id = first.value["task_id"]
    if phase != "queued":
        scheduler.dispatch()
    executor.cleaned.add(task_id)
    if phase == "failed":
        scheduler.finish("owner", task_id, "failed")
    elif phase == "cancelled":
        scheduler.cancel("owner", task_id)
        scheduler.finish("owner", task_id, "failed")
    before = store.get_task("owner", task_id)
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("UPDATE scheduler_resources SET pending_terminal=? WHERE task_id=?", (pending, task_id))
    with pytest.raises(StoreError) as error:
        store.get_scheduler_resources()
    assert error.value.code == "STORAGE_UNAVAILABLE"
    if phase == "running":
        with pytest.raises(StoreError) as error:
            scheduler.finish("owner", task_id, "failed")
        assert error.value.code == "STORAGE_UNAVAILABLE"
    assert store.get_task("owner", task_id) == before


def test_start_failure_with_confirmed_native_cleanup_becomes_terminal(tmp_path):
    scheduler,store,executor=make(tmp_path)
    accepted=submit(scheduler)
    executor.fail_start=True
    executor.cleaned.add(accepted.value['task_id'])
    assert scheduler.dispatch()==[]
    assert store.get_task('owner',accepted.value['task_id'])['status']=='failed'
    assert scheduler.dispatch()==[] and len(executor.started)==1
