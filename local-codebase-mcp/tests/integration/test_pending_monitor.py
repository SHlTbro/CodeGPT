"""A live UI transaction owns application until canonical commit or rollback."""
from __future__ import annotations

import copy
import socket
import threading

from config import ConfigStore, config_from_data, ensure_config, load_config
from runtime import Runtime, probe_mcp


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def test_monitor_cannot_apply_pending_ui_listener_change(tmp_path, monkeypatch):
    path = ensure_config(tmp_path / "config.toml")
    store = ConfigStore(path)
    snapshot = store.read()
    initial = copy.deepcopy(snapshot["data"])
    initial["server"]["port"] = free_port()
    store.save(initial, snapshot["revision"], lambda config: {"verified": True})
    runtime = Runtime(path, tmp_path / "state", probe_interval=0.2, startup_timeout=12)
    callback_entered = threading.Event()
    release_callback = threading.Event()
    unsolicited_apply = threading.Event()
    calls = []
    completed = {}
    worker = None
    try:
        assert runtime.start()["verified"] is True
        state = store.read()
        desired = copy.deepcopy(state["data"])
        desired["server"]["port"] = free_port()
        project = tmp_path / "pending-project"
        project.mkdir()
        desired["projects"] = [{"id": "pending-ui", "name": "Pending UI", "path": str(project)}]
        candidate = config_from_data(desired, path)
        real_apply = runtime.apply
        def observe_apply(config, **options):
            if config == candidate:
                record = {"thread": threading.current_thread().name,
                          "port": config.server.port, "pending": store.pending_path.exists()}
                calls.append(record)
                if record["thread"] != "pending-ui-save":
                    unsolicited_apply.set()
            return real_apply(config, **options)
        monkeypatch.setattr(runtime, "apply", observe_apply)
        def controlled_apply(config):
            # ConfigStore has atomically written the candidate, but the UI has
            # deliberately not yet authorized its public Runtime.apply call.
            assert store.pending_path.exists() and load_config(path) == config
            callback_entered.set()
            assert release_callback.wait(10), "test must release the controlled transaction"
            return runtime.apply(config)
        def save():
            try:
                completed["result"] = store.save(desired, state["revision"], controlled_apply)
            except BaseException as error:
                completed["error"] = error
        worker = threading.Thread(target=save, name="pending-ui-save")
        worker.start()
        assert callback_entered.wait(5), "actual atomic save did not enter its callback"
        # Healthy real probes keep the monitor active. The barrier makes it
        # deterministic that any observed apply here bypasses the UI callback.
        unsolicited_apply.wait(4)
        release_callback.set()
        worker.join(timeout=35)
        assert not worker.is_alive(), "controlled transaction did not finish"
        assert "error" not in completed, repr(completed.get("error"))
        assert completed["result"]["applied"]["verified"] is True
        assert probe_mcp(f"http://127.0.0.1:{candidate.server.port}/mcp", projects=True)["projects"] == [
            {"id": "pending-ui", "name": "Pending UI"}]
        assert load_config(path) == candidate and not store.pending_path.exists()
        assert calls == [{"thread": "pending-ui-save", "port": candidate.server.port, "pending": True}], calls
    finally:
        release_callback.set()
        if worker is not None:
            worker.join(timeout=35)
        runtime.stop()
