"""Real Windows launcher ownership at the Job assignment and resume seams."""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes

import pytest

from config import Config, config_to_dict, dump_toml
from runtime import Runtime, RuntimeError, _WindowsJob


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows launcher and Job ownership")


def fixture_runtime(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(dump_toml(config_to_dict(Config(projects=()))), encoding="utf-8")
    return Runtime(config_path, tmp_path / "state", probe_interval=60)


def worker_program(marker):
    return ("import json,os,time; from pathlib import Path; Path(" + repr(str(marker))
            + ").write_text(json.dumps({'pid':os.getpid(),'parent':os.getppid()})); time.sleep(60)")


def test_windows_worker_cannot_escape_before_delayed_job_assignment(tmp_path, monkeypatch):
    """A scheduler pause before assignment cannot strand the real venv worker."""
    runtime = fixture_runtime(tmp_path)
    marker = tmp_path / "worker.json"
    original_assign = _WindowsJob.assign

    def delayed_assign(job, proc):
        time.sleep(0.4)
        original_assign(job, proc)

    monkeypatch.setattr(_WindowsJob, "assign", delayed_assign)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    worker_handle = None
    try:
        runtime._server = runtime._spawn("server", [sys.executable, "-c", worker_program(marker)])
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists(), "real worker did not execute"
        observed = json.loads(marker.read_text())
        worker_pid = observed["pid"]
        assert observed["parent"] == runtime._server.pid
        # Keep this exact process handle for cleanup even on the pre-fix RED.
        worker_handle = kernel.OpenProcess(0x100001, False, worker_pid)
        assert worker_handle
        assert runtime._jobs["server"].contains(worker_pid), (
            f"real worker {worker_pid} escaped launcher {runtime._server.pid}'s Job during delayed assignment")
        runtime.stop()
        assert kernel.WaitForSingleObject(worker_handle, 3000) == 0, "owner shutdown left its real worker running"
    finally:
        runtime.stop()
        if worker_handle:
            # Exact handle to the worker this fixture created; no PID/name based lookup.
            kernel.TerminateProcess(worker_handle, 1)
            kernel.WaitForSingleObject(worker_handle, 3000)
            kernel.CloseHandle(worker_handle)


@pytest.mark.parametrize("stage", ["assign", "resume"])
def test_failed_windows_ownership_start_never_executes_or_leaves_process(tmp_path, monkeypatch, stage):
    runtime = fixture_runtime(tmp_path)
    marker = tmp_path / "must-not-run.json"
    created = []
    real_popen = subprocess.Popen

    def capture_process(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        created.append(proc)
        return proc

    def fail_stage(job, proc):
        raise RuntimeError("isolated ownership failure")

    monkeypatch.setattr(subprocess, "Popen", capture_process)
    monkeypatch.setattr(_WindowsJob, stage, fail_stage)
    try:
        with pytest.raises(RuntimeError, match="isolated ownership failure"):
            runtime._spawn("server", [sys.executable, "-c", worker_program(marker)])
        assert len(created) == 1 and created[0].poll() is not None
        assert not marker.exists(), "worker code ran before ownership was established"
    finally:
        runtime.stop()
        for proc in created:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


def test_failed_popen_explicitly_closes_unregistered_windows_job(tmp_path, monkeypatch):
    runtime = fixture_runtime(tmp_path)
    jobs = []
    original_init = _WindowsJob.__init__

    def track_job(job):
        original_init(job)
        jobs.append(job)

    def fail_popen(*args, **kwargs):
        raise OSError("isolated Popen failure")

    monkeypatch.setattr(_WindowsJob, "__init__", track_job)
    monkeypatch.setattr(subprocess, "Popen", fail_popen)
    try:
        with pytest.raises(RuntimeError, match="无法启动 server"):
            runtime._spawn("server", [sys.executable, "-c", "pass"])
        # Keep a reference so passing cannot depend on garbage collection.
        assert len(jobs) == 1 and jobs[0]._handle is None
        assert not runtime._logs
    finally:
        runtime.stop()
        for job in jobs:
            job.close()


@pytest.mark.parametrize("stage", ["assign", "resume"])
@pytest.mark.parametrize("cleanup_failure", ["terminate", "wait"])
def test_failed_start_closes_job_log_and_pipe_even_if_popen_cleanup_fails(tmp_path, monkeypatch, cleanup_failure, stage):
    runtime = fixture_runtime(tmp_path)
    marker = tmp_path / "must-not-execute.json"
    created = []
    jobs = []
    fault_calls = []
    real_popen = subprocess.Popen

    def capture_process(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        created.append(proc)
        def fault(*args, **kwargs):
            fault_calls.append(cleanup_failure)
            raise OSError("isolated cleanup failure")
        monkeypatch.setattr(proc, cleanup_failure, fault)
        return proc

    def fail_resume(job, proc):
        jobs.append(job)
        raise RuntimeError("isolated resume failure")

    monkeypatch.setattr(subprocess, "Popen", capture_process)
    monkeypatch.setattr(_WindowsJob, stage, fail_resume)
    try:
        with pytest.raises(RuntimeError, match="isolated resume failure"):
            runtime._spawn("tunnel", [sys.executable, "-c", worker_program(marker)], redact="isolated_test_secret")
        assert fault_calls == [cleanup_failure]
        assert jobs[0]._handle is None, "failed suspended start left its Job handle open"
        assert created[0].stdout.closed, "failed start left its redacted log pipe open"
        assert not runtime._logs
        deadline = time.monotonic() + 3
        while created[0].poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert created[0].poll() is not None, "failed start left its suspended process alive"
        assert not marker.exists()
    finally:
        runtime.stop()
        for job in jobs:
            job.close()
        for proc in created:
            # Original class methods, independent of the injected instance faults.
            if proc.poll() is None:
                real_popen.terminate(proc)
                real_popen.wait(proc, timeout=5)
            if proc.stdout:
                proc.stdout.close()
