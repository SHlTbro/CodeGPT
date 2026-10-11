"""Public Windows startup behavior at the standard winreg seam and real HKCU."""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import pytest

import startup
from config import ensure_config
from conftest import PKG_DIR


class _Key:
    def __init__(self, name):
        self.name = name
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass


class FakeWinreg:
    """Mirror only the documented winreg operations used for per-user values."""
    HKEY_CURRENT_USER = "HKCU"
    KEY_READ, KEY_WRITE, KEY_WOW64_64KEY = 1, 2, 4
    REG_SZ, REG_BINARY, REG_DWORD, REG_EXPAND_SZ = 1, 3, 4, 2
    def __init__(self):
        self.values = {startup.RUN_KEY: {"Other-App": ("unrelated launch value", self.REG_SZ)}}
        self.writes = []
        self.fail_write_once = False
        self.fail_delete_once = False
        self.fail_next_read = False
        self.fail_verification_once = False
        self.read_denied = False
    def OpenKey(self, root, path, reserved, access):
        assert root == self.HKEY_CURRENT_USER
        if self.read_denied and access & self.KEY_READ:
            raise PermissionError("private test error; must not leak")
        if path not in self.values:
            raise FileNotFoundError()
        return _Key(path)
    def CreateKeyEx(self, root, path, reserved, access):
        assert root == self.HKEY_CURRENT_USER
        self.values.setdefault(path, {})
        return _Key(path)
    def QueryValueEx(self, key, name):
        if self.fail_next_read:
            self.fail_next_read = False
            raise PermissionError("verification unavailable; must not leak")
        try:
            return copy.deepcopy(self.values[key.name][name])
        except KeyError:
            raise FileNotFoundError() from None
    def SetValueEx(self, key, name, reserved, kind, value):
        self.writes.append(("set", key.name, name))
        self.values[key.name][name] = (copy.deepcopy(value), kind)
        if self.fail_write_once:
            self.fail_write_once = False
            raise PermissionError("simulated partial registration")
        if self.fail_verification_once:
            self.fail_verification_once = False
            self.fail_next_read = True
    def DeleteValue(self, key, name):
        self.writes.append(("delete", key.name, name))
        if name not in self.values[key.name]:
            raise FileNotFoundError()
        del self.values[key.name][name]
        if self.fail_delete_once:
            self.fail_delete_once = False
            raise PermissionError("simulated partial removal")


@pytest.fixture
def manager():
    registry = FakeWinreg()
    # A clean checkout need not contain a user config.toml. Keep the Windows
    # command short using a dedicated canonical fixture, never production files.
    fixture_dir = PKG_DIR / ".setup-work" / "su"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    cfg = ensure_config(fixture_dir / "config.toml")
    return startup.StartupManager(PKG_DIR, cfg, 8169, registry=registry), registry


def assert_public_status(status):
    assert set(status) == {"supported", "enabled", "registered", "verified", "message"}
    assert not any(key in status for key in ("value", "command", "argv", "snapshot"))


def test_enable_registers_only_owned_entry_reads_back_and_is_idempotent(manager):
    owner, registry = manager
    unrelated = copy.deepcopy(registry.values[startup.RUN_KEY]["Other-App"])
    initial = owner.status(False)
    assert_public_status(initial)
    assert initial["verified"] is True and initial["registered"] is False
    enabled = owner.apply(True)
    assert_public_status(enabled)
    assert enabled["enabled"] is enabled["registered"] is enabled["verified"] is True
    captured = owner.snapshot()
    assert captured.present and captured.value_type == registry.REG_SZ
    assert owner.entry_name == "Code-GPT-" + hashlib.sha256(str(owner.config_path).casefold().encode("utf-8")).hexdigest()[:16]
    assert "-NoBrowser" in captured.value and "-WindowStyle Hidden" in captured.value
    assert "-File" in captured.value and "setup.ps1" in captured.value
    assert "-SetupPort 8169" in captured.value and "-Config .setup-work\\su\\config.toml" in captured.value
    assert "powershell.exe" in captured.value and "-Command" not in captured.value
    assert registry.values[startup.RUN_KEY]["Other-App"] == unrelated
    registry.writes.clear()
    assert owner.apply(True)["verified"] is True
    assert registry.writes == []
    assert "powershell" not in enabled["message"].lower()
    assert owner.apply(False)["verified"] is True
    assert owner.status(False)["registered"] is False
    registry.writes.clear()
    assert owner.apply(False)["verified"] is True and registry.writes == []
    assert registry.values[startup.RUN_KEY] == {"Other-App": unrelated}


def test_disable_removes_only_exact_owned_entry(manager):
    owner, registry = manager
    owner.apply(True)
    registry.values[startup.RUN_KEY]["Code-GPT-some-other-config"] = ("other launch", registry.REG_SZ)
    registry.writes.clear()
    result = owner.apply(False)
    assert result["enabled"] is False and result["registered"] is False and result["verified"] is True
    assert registry.values[startup.RUN_KEY]["Code-GPT-some-other-config"] == ("other launch", registry.REG_SZ)
    assert all(name == owner.entry_name for _, _, name in registry.writes)


@pytest.mark.parametrize("old", [None, ("native old text", FakeWinreg.REG_EXPAND_SZ), (42, FakeWinreg.REG_DWORD)])
@pytest.mark.parametrize("failure", ["write", "verify"])
def test_registration_failure_precisely_restores_original_native_value(manager, old, failure):
    owner, registry = manager
    if old is not None:
        registry.values[startup.RUN_KEY][owner.entry_name] = old
    before = copy.deepcopy(registry.values)
    if failure == "write":
        registry.fail_write_once = True
    else:
        registry.fail_verification_once = True
    with pytest.raises(startup.StartupError, match="已恢复"):
        owner.apply(True)
    assert registry.values == before
    assert all(name == owner.entry_name for _, _, name in registry.writes)


def test_failed_removal_restores_previous_entry(manager):
    owner, registry = manager
    owner.apply(True)
    previous = owner.snapshot()
    registry.fail_delete_once = True
    with pytest.raises(startup.StartupError, match="已恢复"):
        owner.apply(False)
    assert owner.snapshot() == previous
    assert owner.status(True)["verified"] is True


def test_restore_snapshot_compensates_a_later_runtime_failure(manager):
    owner, registry = manager
    previous = owner.snapshot()
    owner.apply(True)
    assert owner.restore(previous) == {"verified": True}
    assert owner.snapshot() == previous
    foreign = startup.StartupSnapshot("Code-GPT-foreign", False)
    with pytest.raises(startup.StartupError, match="不属于"):
        owner.restore(foreign)
    assert "Code-GPT-foreign" not in registry.values[startup.RUN_KEY]


@pytest.mark.parametrize("value", [None, "true", 1, {}, []])
def test_flag_type_is_strict_before_registry_mutation(manager, value):
    owner, registry = manager
    before = copy.deepcopy(registry.values)
    with pytest.raises(startup.StartupError):
        owner.apply(value)
    with pytest.raises(startup.StartupError):
        owner.status(value)
    assert registry.values == before and registry.writes == []


def test_registry_read_error_reports_action_and_no_raw_value(manager):
    owner, registry = manager
    registry.read_denied = True
    state = owner.status(True)
    assert state["verified"] is False and state["registered"] is None
    assert "权限" in state["message"] and "private test" not in state["message"]
    with pytest.raises(startup.StartupError):
        owner.apply(True)
    assert registry.writes == []


@pytest.mark.parametrize("approval,blocked", [(2, False), (3, True), (6, True), (0, True)])
def test_task_manager_approval_is_observed_and_never_changed(manager, approval, blocked):
    owner, registry = manager
    owner.apply(True)
    marker = approval.to_bytes(4, "little") + b"\x00" * 8
    registry.values[startup.APPROVAL_KEY] = {owner.entry_name: (marker, registry.REG_BINARY)}
    registry.writes.clear()
    state = owner.status(True)
    assert state["registered"] is True and state["verified"] is (not blocked)
    assert state["enabled"] is (not blocked)
    if blocked:
        with pytest.raises(startup.StartupError, match="启动应用"):
            owner.apply(True)
        assert registry.writes == []
    else:
        assert owner.apply(True)["verified"] is True and registry.writes == []
    assert registry.values[startup.APPROVAL_KEY][owner.entry_name] == (marker, registry.REG_BINARY)
    assert owner.apply(False)["verified"] is True
    assert registry.values[startup.APPROVAL_KEY][owner.entry_name] == (marker, registry.REG_BINARY)
    assert all(path == startup.RUN_KEY for _, path, _ in registry.writes)


@pytest.mark.parametrize("approval", [3, 6])
def test_removed_run_entry_with_retained_approval_has_honest_manual_start_advice(manager, approval):
    owner, registry = manager
    marker = approval.to_bytes(4, "little") + b"\x00" * 8
    registry.values[startup.APPROVAL_KEY] = {owner.entry_name: (marker, registry.REG_BINARY)}
    before = copy.deepcopy(registry.values)
    with pytest.raises(startup.StartupError) as failure:
        owner.apply(True)
    message = str(failure.value)
    assert "启动项已移除" in message and "setup.bat" in message
    assert "暂时无法开启" in message and "任务管理器" not in message
    assert registry.values == before and registry.writes == []
    assert owner.status(False)["verified"] is True


@pytest.mark.parametrize("marker", [b"", b"\x02", "02", 2])
def test_unknown_approval_formats_do_not_claim_enabled(manager, marker):
    owner, registry = manager
    owner.apply(True)
    registry.values[startup.APPROVAL_KEY] = {owner.entry_name: (marker, registry.REG_BINARY)}
    state = owner.status(True)
    assert state["registered"] is True and state["enabled"] is state["verified"] is False
    with pytest.raises(startup.StartupError):
        owner.apply(True)


def test_disabled_configuration_requires_no_owned_registry_value(manager):
    owner, registry = manager
    owner.apply(True)
    state = owner.status(False)
    assert state["registered"] is True and state["verified"] is False
    assert owner.apply(False)["verified"] is True


def test_provisional_zero_port_can_never_be_registered(manager):
    owner, registry = manager
    provisional = startup.StartupManager(owner.project_dir, owner.config_path, 0, registry=registry)
    assert provisional.apply(False)["verified"] is True
    with pytest.raises(startup.StartupError, match="实际监听端口"):
        provisional.apply(True)
    assert registry.writes == []


@pytest.mark.parametrize("port", [True, -1, 65536, "8169", None])
def test_invalid_setup_ports_are_rejected(manager, port):
    owner, registry = manager
    with pytest.raises(startup.StartupError):
        startup.StartupManager(owner.project_dir, owner.config_path, port, registry=registry)
    assert registry.writes == []


def test_non_windows_disables_cleanly_but_refuses_enable(monkeypatch):
    monkeypatch.setattr(startup, "winreg", None)
    owner = startup.StartupManager(PKG_DIR, PKG_DIR / "config.toml", 8169)
    assert owner.apply(False)["supported"] is False
    assert owner.apply(False)["verified"] is True
    assert owner.status(True)["verified"] is False
    with pytest.raises(startup.StartupError, match="仅支持 Windows"):
        owner.apply(True)


def test_too_long_run_command_does_not_register_and_can_still_disable(manager, monkeypatch):
    owner, registry = manager
    owner.apply(True)
    monkeypatch.setattr(startup, "_MAX_COMMAND_LENGTH", 10)
    with pytest.raises(startup.StartupError, match="260"):
        owner.apply(True)
    assert owner.status(True)["verified"] is False
    assert owner.apply(False)["verified"] is True
    assert owner.snapshot().present is False


@pytest.mark.skipif(startup.winreg is None, reason="Windows HKCU registration only")
def test_real_current_user_registration_verification_and_removal():
    """Actual reversible HKCU test; never launches a process or changes production TOML."""
    fixture_dir = PKG_DIR / ".setup-work" / "cs"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    cfg = ensure_config(fixture_dir / "config.toml")
    owner = startup.StartupManager(PKG_DIR, cfg, 8169)
    original = owner.snapshot()
    assert original.present is False, "isolated fixture unexpectedly has an existing startup entry"
    try:
        result = owner.apply(True)
        assert result["enabled"] is result["registered"] is result["verified"] is True
        captured = owner.snapshot()
        assert captured.present and captured.value_type == startup.winreg.REG_SZ
        assert len(captured.value) <= 260
        assert owner.apply(False)["verified"] is True
        assert owner.snapshot().present is False
    finally:
        owner.restore(original)
    assert owner.status(False)["verified"] is True and owner.snapshot().present is False

