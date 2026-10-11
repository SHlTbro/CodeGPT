"""Real owned Windows Python process trees, never Codex/model commands."""
from pathlib import Path
import json
import os
import sqlite3
import subprocess
import sys
import time

import pytest

from local_agents.runner import Runner, RunnerError, LaunchSpec, process_identity

pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows task Job lifecycle")


def child_env(**extra):
    # Do not copy model/login/session credentials from the parent environment.
    value = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH") if key in os.environ}
    return {**value, "PYTHONUTF8": "1", **extra}


def task(task_id="owned-task"):
    return {"task_id": task_id, "owner_id": "fixture-owner", "purpose": "project_context"}


def runner_for(tmp_path, program, *, timeout=10, env=None, redactions=(), on_output=None, fault_hook=None):
    spec = LaunchSpec((sys.executable, "-B", "-c", program), str(tmp_path), env or child_env(), timeout, redactions)
    return Runner(tmp_path / "owned-state", lambda t, i: spec, capability_gate=lambda t, s: True,
                  on_output=on_output, fault_hook=fault_hook)


def wait_file(path, timeout=5):
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < deadline, "owned fixture did not create its marker"
        time.sleep(.02)
    return json.loads(path.read_text())


def tree_program(marker):
    child = "import time; time.sleep(60)"
    return ("import subprocess,sys,os,time,json; from pathlib import Path; "
            "p=subprocess.Popen([sys.executable,'-B','-c'," + repr(child) + "]); "
            "Path(" + repr(str(marker)) + ").write_text(json.dumps({'parent':os.getpid(),'child':p.pid})); time.sleep(60)")


def assert_dead(pid):
    deadline = time.monotonic() + 5
    while True:
        identity = process_identity(pid)
        if identity is None or not identity["alive"]:
            return
        assert time.monotonic() < deadline, "owned process remained alive"
        time.sleep(.02)


def test_real_success_drains_both_pipes_and_redacts_split_secret(tmp_path):
    secret = "SIMULATED-ONLY-SECRET-abcdef123456"
    program = ("import sys,os;s=os.environ['T10_SIMULATED_LOG_SECRET']; "
        "sys.stdout.write('x'*150000+s[:27]);sys.stdout.flush();"
        "sys.stdout.write(s[27:]+'y'*150000);"
        "sys.stderr.write('z'*300000+s);sys.stderr.flush()")
    observed = {"stdout": [], "stderr": []}
    runner = runner_for(tmp_path, program, redactions=(secret,), env=child_env(T10_SIMULATED_LOG_SECRET=secret),
                        on_output=lambda task_id, stream, text: observed[stream].append(text))
    try:
        runner.start(task(), {})
        result = runner.wait("owned-task", 10)
        assert result["outcome"] == "succeeded" and result["exit_code"] == 0
        assert result["cleanup_confirmed"] and runner.cleanup_confirmed("owned-task")
        assert result["truncated"] and result["redacted"]
        assert secret not in json.dumps(result)
        assert all(secret not in "".join(chunks) and "[REDACTED]" in "".join(chunks) for chunks in observed.values())
        assert len(result["stdout_tail"]) <= 33000 and len(result["stderr_tail"]) <= 33000
    finally:
        runner.close()


def test_cancel_real_parent_and_child_without_stopping_unrelated_owned_fixture(tmp_path):
    marker = tmp_path / "tree.json"
    unrelated = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(60)"], env=child_env())
    unrelated_identity = process_identity(unrelated.pid)
    runner = runner_for(tmp_path, tree_program(marker))
    try:
        runner.start(task(), {})
        pids = wait_file(marker)
        owned = runner.identity("owned-task")
        assert owned["creation_time"] and owned["job_active_processes"] >= 2
        runner.request_stop("owned-task")
        result = runner.wait("owned-task", 10)
        assert result["outcome"] == "cancelled" and result["cleanup_confirmed"]
        assert_dead(pids["parent"])
        assert_dead(pids["child"])
        assert unrelated.poll() is None and process_identity(unrelated.pid) == unrelated_identity
    finally:
        runner.close()
        unrelated.terminate()
        unrelated.wait(timeout=5)


def test_timeout_real_tree_confirms_all_members_exited(tmp_path):
    marker = tmp_path / "timeout-tree.json"
    runner = runner_for(tmp_path, tree_program(marker), timeout=1)
    try:
        runner.start(task(), {})
        pids = wait_file(marker)
        result = runner.wait("owned-task", 10)
        assert result["outcome"] == "timed_out" and result["cleanup_confirmed"]
        for pid in pids.values():
            assert_dead(pid)
    finally:
        runner.close()


def test_default_capability_gate_and_direct_model_key_fail_before_any_process(tmp_path):
    marker = tmp_path / "must-not-run"
    spec = LaunchSpec((sys.executable, "-c", "from pathlib import Path;Path(" + repr(str(marker)) + ").touch()"),
                      str(tmp_path), child_env(), 5)
    runner = Runner(tmp_path / "gate", lambda t, i: spec)
    with pytest.raises(RunnerError) as error:
        runner.start(task(), {})
    assert error.value.code == "CAPABILITY_UNAVAILABLE"
    assert not marker.exists() and runner.cleanup_confirmed("owned-task")
    runner.close()
    runner = runner_for(tmp_path, "print('must not run')", env=child_env(OPENAI_API_KEY="simulated-only-not-real-key"))
    with pytest.raises(RunnerError) as error:
        runner.start(task(), {})
    assert error.value.code == "CREDENTIAL_UNAVAILABLE"
    assert runner.cleanup_confirmed("owned-task")
    runner.close()


def test_start_failure_proves_no_tree_and_cannot_replay_same_task(tmp_path):
    spec = LaunchSpec((str(tmp_path / "missing.exe"),), str(tmp_path), child_env(), 5)
    runner = Runner(tmp_path / "startfail", lambda t, i: spec, capability_gate=lambda t, s: True)
    with pytest.raises(RunnerError):
        runner.start(task(), {})
    assert runner.cleanup_confirmed("owned-task")
    with pytest.raises(RunnerError) as error:
        runner.start(task(), {})
    assert error.value.code == "REQUEST_CONFLICT"
    runner.close()


def test_output_observer_exception_does_not_block_pipe_drain(tmp_path):
    def broken_observer(task_id, stream, text):
        raise ValueError("controlled observer failure")
    runner = runner_for(tmp_path, "import sys;sys.stdout.write('x'*500000);sys.stderr.write('y'*500000)", on_output=broken_observer)
    try:
        runner.start(task(), {})
        result = runner.wait("owned-task", 10)
        assert result["cleanup_confirmed"] and result["logs_error"]
        assert result["outcome"] == "failed"
    finally:
        runner.close()


def test_concurrent_child_environment_is_private_and_parent_is_unchanged(tmp_path):
    before = os.environ.get("T10_FIXTURE_CONTEXT")
    def resolve(t, i):
        return LaunchSpec((sys.executable, "-B", "-c", "import os;print(os.environ['T10_FIXTURE_CONTEXT'])"),
                          str(tmp_path), child_env(T10_FIXTURE_CONTEXT=t["task_id"]), 5)
    runner = Runner(tmp_path / "env", resolve, capability_gate=lambda t, s: True)
    try:
        for name in ("plan-context", "api-context"):
            runner.start(task(name), {})
        for name in ("plan-context", "api-context"):
            result = runner.wait(name, 10)
            assert result["outcome"] == "succeeded" and result["stdout_tail"].strip() == name
        assert os.environ.get("T10_FIXTURE_CONTEXT") == before
    finally:
        runner.close()


@pytest.mark.parametrize("stage", ["after_create", "after_identity_commit", "after_resume"])
def test_hard_launcher_crash_at_each_ownership_window_cleans_without_replay(tmp_path, stage):
    marker, identity_file = tmp_path / "crash-tree.json", tmp_path / "native-identity.json"
    program = '''
import os,sys,json,time
from pathlib import Path
from local_agents.runner import Runner,LaunchSpec
p=Path(sys.argv[1]); wanted=sys.argv[2]
env={k:os.environ[k] for k in ('SYSTEMROOT','WINDIR','TEMP','TMP','PATH') if k in os.environ}
def hook(stage, identity):
    if stage==wanted:
        (p/'native-identity.json').write_text(json.dumps(identity))
        if stage=='after_resume':
            deadline=time.monotonic()+5
            while not (p/'crash-tree.json').exists() and time.monotonic()<deadline: time.sleep(.01)
        os._exit(73)
spec=LaunchSpec((sys.executable,'-B','-c',sys.argv[3]),str(p),env,10)
r=Runner(p/'owned-state',lambda t,i:spec,capability_gate=lambda t,s:True,fault_hook=hook)
r.start({'task_id':'crash-task','owner_id':'fixture-owner'}, {})
'''
    environment = child_env(PYTHONPATH=os.environ.get("PYTHONPATH", ""))
    child = subprocess.run([sys.executable, "-B", "-c", program, str(tmp_path), stage, tree_program(marker)],
                           env=environment, capture_output=True, text=True, timeout=15)
    assert child.returncode == 73, child.stdout + child.stderr
    recorded = wait_file(identity_file)
    assert_dead(recorded["pid"])
    if marker.exists():
        for pid in wait_file(marker).values():
            assert_dead(pid)
    reopened = Runner(tmp_path / "owned-state", lambda t, i: None)
    try:
        assert reopened.recover()["crash-task"] is True
        assert reopened.cleanup_confirmed("crash-task")
        with pytest.raises(RunnerError) as error:
            reopened.start(task("crash-task"), {})
        assert error.value.code == "REQUEST_CONFLICT"
    finally:
        reopened.close()


def test_recovery_never_kills_a_pid_with_different_creation_identity(tmp_path):
    runner = runner_for(tmp_path, "print('done')")
    runner.start(task(), {})
    runner.wait("owned-task", 10)
    runner.close()
    unrelated = subprocess.Popen([sys.executable, "-B", "-c", "import time;time.sleep(60)"], env=child_env())
    identity = process_identity(unrelated.pid)
    try:
        with sqlite3.connect(tmp_path / "owned-state/ownership.sqlite") as connection:
            connection.execute("UPDATE runs SET phase='owned',clean=0,outcome=NULL,creator_pid=999999999,creator_time=1,pid=?,creation_time=?",
                               (unrelated.pid, identity["creation_time"] + 1))
        reopened = Runner(tmp_path / "owned-state", lambda t, i: None)
        assert reopened.recover()["owned-task"] is False
        assert not reopened.cleanup_confirmed("owned-task")
        assert unrelated.poll() is None and process_identity(unrelated.pid) == identity
        reopened.close()
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=5)


def test_resume_window_exception_proves_cleanup_instead_of_sticking_forever(tmp_path):
    marker = tmp_path / "resume-window-tree.json"
    observed = {}
    def failure(stage, identity):
        if stage == "after_resume":
            observed.update(wait_file(marker))
            raise ValueError("controlled failure after owned resume")
    runner = runner_for(tmp_path, tree_program(marker), fault_hook=failure)
    try:
        with pytest.raises(RunnerError):
            runner.start(task(), {})
        assert runner.cleanup_confirmed("owned-task")
        for pid in observed.values():
            assert_dead(pid)
    finally:
        runner.close()


def test_cleanup_boolean_in_corrupt_active_journal_cannot_release_live_tree(tmp_path):
    marker = tmp_path / "active-tree.json"
    runner = runner_for(tmp_path, tree_program(marker))
    try:
        runner.start(task(), {})
        wait_file(marker)
        with sqlite3.connect(runner.database_path) as connection:
            connection.execute("UPDATE runs SET phase='owned',clean=1,outcome='succeeded'")
        reopened = Runner(tmp_path / "owned-state", lambda t, i: None)
        with pytest.raises(RunnerError) as error:
            reopened.cleanup_confirmed("owned-task")
        assert error.value.code == "STORAGE_UNAVAILABLE"
        # Restore owned fixture state so only our real runner cleans its tree.
        with sqlite3.connect(runner.database_path) as connection:
            connection.execute("UPDATE runs SET phase='owned',clean=0,outcome=NULL")
        reopened.close()
    finally:
        runner.close()


def test_native_reconciliation_refuses_forged_finished_proof_and_live_creator(tmp_path):
    marker = tmp_path / "owned-live.json"
    runner = runner_for(tmp_path, tree_program(marker))
    try:
        runner.start(task(), {})
        pids = wait_file(marker)
        observer = Runner(tmp_path / "owned-state", lambda t, i: None)
        assert observer.recover()["owned-task"] is False
        with sqlite3.connect(runner.database_path) as connection:
            connection.execute("UPDATE runs SET phase='finished',clean=1,outcome='succeeded'")
        assert not observer.cleanup_confirmed("owned-task")
        assert observer.result("owned-task")["outcome"] is None
        assert observer.recover()["owned-task"] is False
        assert process_identity(pids["parent"])["alive"] and process_identity(pids["child"])["alive"]
        with sqlite3.connect(runner.database_path) as connection:
            connection.execute("UPDATE runs SET phase='owned',clean=0,outcome=NULL")
        observer.close()
    finally:
        runner.close()


def test_unknown_journal_layout_preserves_history_and_rejects_launch(tmp_path):
    import hashlib
    runner = runner_for(tmp_path, "print('history')")
    runner.start(task(), {})
    runner.wait("owned-task", 10)
    runner.close()
    with sqlite3.connect(runner.database_path) as connection:
        connection.execute("ALTER TABLE runs ADD COLUMN unknown_future_field TEXT")
    before = hashlib.sha256(runner.database_path.read_bytes()).hexdigest()
    with pytest.raises(RunnerError) as error:
        Runner(tmp_path / "owned-state", lambda t, i: None)
    assert error.value.code == "SCHEMA_UNSUPPORTED"
    assert hashlib.sha256(runner.database_path.read_bytes()).hexdigest() == before


def test_journal_acceptance_failure_has_zero_external_process_effects(tmp_path):
    called = []
    runner = runner_for(tmp_path, "print('must not launch')", fault_hook=lambda stage, identity: called.append(stage))
    with sqlite3.connect(runner.database_path) as connection:
        connection.execute("CREATE TRIGGER deny_owned_record BEFORE INSERT ON runs BEGIN SELECT RAISE(ABORT,'fixture journal rejection'); END")
    try:
        with pytest.raises(RunnerError) as error:
            runner.start(task(), {})
        assert error.value.code == "STORAGE_UNAVAILABLE" and called == []
        assert runner.live == {}
    finally:
        runner.close()


def test_real_scheduler_keeps_project_lock_until_native_job_proof(monkeypatch, tmp_path):
    sys.path.insert(0, str(Path(__file__).parents[1] / "unit"))
    from test_agent_scheduler import selection
    from local_agents.store import Store
    from local_agents.scheduler import Scheduler
    marker = tmp_path / "scheduled-tree.json"
    runner = runner_for(tmp_path, tree_program(marker))
    store = Store(tmp_path / "tasks.sqlite", tmp_path / "reports")
    scheduler = Scheduler(store, runner, enabled=True)
    runner.on_exit = lambda task_id, outcome: scheduler.finish("owner", task_id, outcome)
    def accept(request_id):
        return scheduler.submit(owner_id="owner", operation="project_context",
            request={"project_id": "same-project", "request_id": request_id}, selection=selection())
    first, second = accept("one"), accept("two")
    task_id = first.value["task_id"]
    try:
        scheduler.dispatch()
        pids = wait_file(marker)
        job = runner.live[task_id].job
        actual_count = job.active_count
        def unknown_proof():
            raise RunnerError("CLEANUP_FAILED")
        monkeypatch.setattr(job, "active_count", unknown_proof)
        scheduler.cancel("owner", task_id)
        assert not scheduler.finish("owner", task_id, "succeeded")
        uncertain = store.get_task("owner", task_id)
        assert uncertain["status"] == "cancelling" and uncertain["error"]["code"] == "CLEANUP_FAILED"
        assert scheduler.dispatch() == []
        assert store.get_task("owner", second.value["task_id"])["status"] == "queued"
        for pid in pids.values():
            assert_dead(pid)
        monkeypatch.setattr(job, "active_count", actual_count)
        assert runner.wait(task_id, 10)["cleanup_confirmed"]
        deadline = time.monotonic() + 5
        while store.get_task("owner", task_id)["status"] != "cancelled":
            assert time.monotonic() < deadline
            time.sleep(.02)
        assert scheduler.dispatch() == [second.value["task_id"]]
        scheduler.cancel("owner", second.value["task_id"])
        runner.wait(second.value["task_id"], 10)
        assert not accept("one").created
    finally:
        runner.close()


def test_truncation_mark_is_only_on_the_stream_that_was_truncated(tmp_path):
    runner = runner_for(tmp_path, "import sys;sys.stdout.write('x'*100000);sys.stderr.write('short stderr')")
    try:
        runner.start(task(), {})
        result = runner.wait("owned-task", 10)
        assert result["stdout_tail"].startswith("[TRUNCATED]")
        assert result["stderr_tail"] == "short stderr"
    finally:
        runner.close()


def test_natural_parent_exit_does_not_leave_its_owned_child_running(tmp_path):
    marker = tmp_path / "normal-parent-exit.json"
    program = tree_program(marker)
    # Remove only the parent's final sleep; the child's sleep remains genuine.
    program = program.rsplit("time.sleep(60)", 1)[0] + "pass"
    runner = runner_for(tmp_path, program)
    try:
        runner.start(task(), {})
        pids = wait_file(marker)
        result = runner.wait("owned-task", 10)
        assert result["outcome"] == "succeeded" and result["cleanup_confirmed"]
        for pid in pids.values():
            assert_dead(pid)
    finally:
        runner.close()


def test_owned_journal_cannot_claim_success_before_cleanup(tmp_path):
    marker = tmp_path / "pending-proof.json"
    runner = runner_for(tmp_path, tree_program(marker))
    try:
        runner.start(task(), {})
        wait_file(marker)
        with sqlite3.connect(runner.database_path) as connection:
            connection.execute("UPDATE runs SET phase='owned',outcome='succeeded',clean=0")
        with pytest.raises(RunnerError) as error:
            runner.result("owned-task")
        assert error.value.code == "STORAGE_UNAVAILABLE"
        with sqlite3.connect(runner.database_path) as connection:
            connection.execute("UPDATE runs SET phase='owned',outcome=NULL,clean=0")
    finally:
        runner.close()


def test_second_pipe_failure_closes_every_already_allocated_fd(monkeypatch, tmp_path):
    import local_agents.runner as module
    marker = tmp_path / 'must-not-spawn'
    runner = runner_for(tmp_path, 'from pathlib import Path; Path(' + repr(str(marker)) + ').touch()')
    real_open, real_pipe = module.os.open, module.os.pipe
    allocated, calls = [], []

    def track_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        allocated.append(fd)
        return fd

    def second_pipe_fails():
        calls.append(True)
        if len(calls) == 2:
            raise OSError('owned synthetic pipe allocation failure')
        pair = real_pipe()
        allocated.extend(pair)
        return pair

    with monkeypatch.context() as patch:
        patch.setattr(module.os, 'open', track_open)
        patch.setattr(module.os, 'pipe', second_pipe_fails)
        try:
            with pytest.raises(RunnerError, match='EXECUTION_FAILED'):
                runner.start(task(), {})
            assert len(allocated) == 3
            still_open=[]
            for fd in allocated:
                try:
                    os.fstat(fd)
                    still_open.append(fd)
                except OSError:
                    pass
            assert not marker.exists()
            assert runner.identity('owned-task')['pid'] is None
            assert runner.cleanup_confirmed('owned-task')
            assert still_open == [], 'partial allocation leaked owned descriptors'
        finally:
            # RED must not leave leaked descriptors in the test worker.
            for fd in allocated:
                try:
                    os.close(fd)
                except OSError:
                    pass
            runner.close()


@pytest.mark.parametrize('cancel', [False, True])
def test_blocked_observer_fails_boundedly_without_holding_native_cleanup(tmp_path, cancel):
    from threading import Event
    entered, release = Event(), Event()
    observations = {}

    def blocking_observer(task_id, stream, text):
        entered.set()
        release.wait(10)

    runner = runner_for(tmp_path, "import time; print('owned fixture',flush=True); time.sleep(.05)",
                        timeout=.4 if not cancel else 10, on_output=blocking_observer)
    try:
        runner.start(task(), {})
        assert entered.wait(5)
        if cancel:
            runner.request_stop('owned-task')
        deadline = time.monotonic() + 5
        while runner.identity('owned-task')['job_active_processes']:
            assert time.monotonic() < deadline
            time.sleep(.02)
        identity=runner.identity('owned-task')
        assert_dead(identity['pid'])
        result=runner.wait('owned-task', 3)
        assert result['cleanup_confirmed'] and result['logs_error']
        assert result['outcome'] == ('cancelled' if cancel else 'failed')
        assert not release.is_set()
        observations.update(job_active_processes=0, parent_alive=False, cleanup_confirmed=True,
                            logs_error=True, outcome=result['outcome'], observer_still_blocked=True)
        print('BLOCKED_OBSERVER_PROOF=' + json.dumps(observations, sort_keys=True))
    finally:
        release.set()
        runner.close()


def test_observer_queue_overflow_never_becomes_success_and_stops_delivery(tmp_path):
    from threading import Event
    entered, release = Event(), Event()
    observed=[]
    def blocking_observer(task_id, stream, text):
        observed.append(text)
        entered.set()
        release.wait(10)
    runner=runner_for(tmp_path, "import sys,time; print('first',flush=True); time.sleep(.1); sys.stdout.write('x'*4000000)",
                      on_output=blocking_observer)
    try:
        runner.start(task(), {})
        assert entered.wait(5)
        result=runner.wait('owned-task', 4)
        assert result['cleanup_confirmed'] and result['logs_error'] and result['outcome']=='failed'
        assert len(observed)==1
        release.set()
        time.sleep(.2)
        assert len(observed)==1  # Failed/completed delivery is closed, not late success.
    finally:
        release.set(); runner.close()


def test_second_fdopen_failure_closes_transferred_and_untransferred_streams(monkeypatch, tmp_path):
    import local_agents.runner as module
    runner=runner_for(tmp_path, "raise SystemExit('must remain suspended')")
    real_open,real_pipe,real_fdopen=module.os.open,module.os.pipe,module.os.fdopen
    allocated=[]; calls=[]
    def opened(*args,**kwargs):
        fd=real_open(*args,**kwargs); allocated.append(fd); return fd
    def piped():
        pair=real_pipe(); allocated.extend(pair); return pair
    def fdopened(*args,**kwargs):
        calls.append(True)
        if len(calls)==2:
            raise OSError('owned synthetic stream transfer failure')
        return real_fdopen(*args,**kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(module.os,'open',opened); patch.setattr(module.os,'pipe',piped)
        patch.setattr(module.os,'fdopen',fdopened)
        try:
            with pytest.raises(RunnerError,match='EXECUTION_FAILED'):
                runner.start(task(),{})
            assert len(allocated)==5
            still_open=[]
            for fd in allocated:
                try:
                    os.fstat(fd); still_open.append(fd)
                except OSError:
                    pass
            assert runner.cleanup_confirmed('owned-task')
            assert still_open==[], 'stream ownership transfer interrupted descriptor cleanup'
        finally:
            for fd in allocated:
                try: os.close(fd)
                except OSError: pass
            runner.close()


def test_timeout_preserves_reason_while_blocked_observer_does_not_hold_tree(tmp_path):
    from threading import Event
    entered,release=Event(),Event()
    calls=[]
    def blocked(task_id,stream,text):
        calls.append(text); entered.set(); release.wait(10)
    runner=runner_for(tmp_path,"import time; print('first',flush=True); time.sleep(60)",
                      timeout=.3,on_output=blocked)
    try:
        runner.start(task(),{})
        assert entered.wait(5)
        result=runner.wait('owned-task',3)
        assert result['cleanup_confirmed'] and result['logs_error']
        assert result['outcome']=='timed_out'
        assert not release.is_set()
        assert_dead(runner.identity('owned-task')['pid'])
    finally:
        release.set();runner.close()


def test_observer_completes_before_deadline_and_close_rejects_later_launch(tmp_path):
    observed=[]
    def slow_but_bounded(task_id,stream,text):
        time.sleep(.03); observed.append((stream,text))
    runner=runner_for(tmp_path,"print('first'); print('second')",on_output=slow_but_bounded)
    runner.start(task(),{})
    result=runner.wait('owned-task',3)
    assert result['outcome']=='succeeded' and not result['logs_error']
    assert ''.join(text for stream,text in observed if stream=='stdout')=='first\r\nsecond\r\n'
    runner.close()
    with pytest.raises(RunnerError,match='CAPABILITY_UNAVAILABLE'):
        runner.start(task('after-close'),{})


def test_timeout_stops_queued_deliveries_when_inflight_observer_returns(tmp_path):
    from threading import Event
    entered,release=Event(),Event(); calls=[]
    def blocked(task_id,stream,text):
        calls.append(text);entered.set();release.wait(10)
    runner=runner_for(tmp_path,"import sys,time;sys.stdout.write('x'*16000);sys.stdout.flush();time.sleep(60)",
                      timeout=.3,on_output=blocked)
    try:
        runner.start(task(),{});assert entered.wait(5)
        assert_dead(runner.identity('owned-task')['pid'])
        release.set()
        result=runner.wait('owned-task',3)
        assert result['outcome']=='timed_out' and result['cleanup_confirmed']
        assert len(calls)==1, 'timeout accepted but queued observer notifications still delivered'
        assert result['logs_error']
    finally:
        release.set();runner.close()
