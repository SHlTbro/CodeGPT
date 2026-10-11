"""Verified, reversible per-user Windows login startup for the existing Setup owner.

Only our canonical-path-derived HKCU Run value is managed. The command is built
from fixed flags and trusted local project/config paths, never browser input.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

try:
    import winreg
except ImportError:
    winreg = None

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVAL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
_MAX_COMMAND_LENGTH = 260
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


class StartupError(RuntimeError):
    """An actionable startup failure; never contains registry data or launch details."""


@dataclass(frozen=True)
class StartupSnapshot:
    """Private compensation state; never serialize into a Setup API response."""
    entry_name: str
    present: bool
    value: object = field(default=None, repr=False)
    value_type: int | None = None


class StartupManager:
    def __init__(self, project_dir, config_path, setup_port, *, registry=None):
        self.project_dir = Path(project_dir).resolve()
        self.config_path = Path(config_path).resolve()
        if type(setup_port) is not int or not 0 <= setup_port <= 65535:
            raise StartupError("配置页面端口必须是 1–65535 的整数。")
        # Port zero is provisional for test/OS-assigned listeners. No registered
        # command may use it; Setup replaces this manager after the actual bind.
        self.setup_port = setup_port
        self._registry = registry if registry is not None else winreg
        identity = str(self.config_path).casefold().encode("utf-8")
        self.entry_name = "Code-GPT-" + hashlib.sha256(identity).hexdigest()[:16]
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(self.entry_name, threading.RLock())

    def _access(self, write=False):
        reg = self._registry
        return (reg.KEY_WRITE if write else reg.KEY_READ) | getattr(reg, "KEY_WOW64_64KEY", 0)

    def _command(self):
        if not 1 <= self.setup_port <= 65535:
            raise StartupError("配置页面尚未取得实际监听端口，请重新打开 Setup 后保存自动启动设置。")
        if self.config_path.suffix.lower() != ".toml":
            raise StartupError("自动启动只支持 canonical TOML，请先使用 config.toml。")
        script = self.project_dir / "setup.ps1"
        if not script.is_file() or not self.config_path.is_file():
            raise StartupError("找不到项目启动脚本或 TOML。请确认 CodeGPT 项目与配置文件存在，再重新保存。")
        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        if not powershell.is_file():
            raise StartupError("未找到 Windows PowerShell，请修复 Windows PowerShell 安装后重试。")
        for path in (script, self.config_path, powershell):
            if any(ord(c) < 32 or c == '"' for c in str(path)):
                raise StartupError("自动启动路径不能包含引号或控制字符。请使用正常的本地项目路径。")
        try:
            relative = os.path.relpath(self.config_path, self.project_dir)
            config_argument = relative if len(relative) < len(str(self.config_path)) else str(self.config_path)
        except ValueError:
            config_argument = str(self.config_path)
        argv = [str(powershell), "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-WindowStyle", "Hidden", "-File", str(script),
                "-NoBrowser", "-SetupPort", str(self.setup_port), "-Config", config_argument]
        command = subprocess.list2cmdline(argv)
        if len(command) > _MAX_COMMAND_LENGTH:
            raise StartupError("Windows 登录启动命令超过 260 字符。请缩短 CodeGPT 项目和配置文件的目录路径，再开启自动启动。")
        return command

    def snapshot(self):
        """Read only our exact Run value, including its native type for rollback."""
        if self._registry is None:
            return StartupSnapshot(self.entry_name, False)
        reg = self._registry
        try:
            with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY, 0, self._access()) as key:
                value, value_type = reg.QueryValueEx(key, self.entry_name)
            return StartupSnapshot(self.entry_name, True, value, value_type)
        except FileNotFoundError:
            return StartupSnapshot(self.entry_name, False)
        except OSError:
            raise StartupError("无法读取 Windows 登录启动设置。请检查当前用户注册表权限，再重试。") from None

    def _approval(self):
        """Observe Task Manager settings without clearing or changing them."""
        reg = self._registry
        try:
            with reg.OpenKey(reg.HKEY_CURRENT_USER, APPROVAL_KEY, 0, self._access()) as key:
                value, value_type = reg.QueryValueEx(key, self.entry_name)
        except FileNotFoundError:
            return "allowed"
        except OSError:
            raise StartupError("无法验证 Windows 启动应用状态。请检查任务管理器中的启动应用和当前用户权限。") from None
        if value_type != reg.REG_BINARY or not isinstance(value, bytes) or len(value) < 4:
            return "unknown"
        state = int.from_bytes(value[:4], "little")
        return "allowed" if state == 2 else ("disabled" if state == 3 else "unknown")

    def status(self, desired_enabled):
        if type(desired_enabled) is not bool:
            raise StartupError("自动启动开关必须是 true 或 false。")
        if self._registry is None:
            return {"supported": False, "enabled": False, "registered": False,
                    "verified": not desired_enabled, "message": "自动启动目前仅支持 Windows。"}
        try:
            current = self.snapshot()
            if not current.present:
                return {"supported": True, "enabled": False, "registered": False,
                        "verified": not desired_enabled,
                        "message": "尚未登记 Windows 登录启动。" if desired_enabled else "已关闭 Windows 登录启动。"}
            matching = current.value_type == self._registry.REG_SZ and current.value == self._command()
            approval = self._approval() if matching else "unknown"
            effective = matching and approval == "allowed"
            if not desired_enabled:
                message = "Windows 启动项仍存在，请重新保存关闭设置。"
            elif not matching:
                message = "Windows 启动项与当前项目配置不一致，请重新保存自动启动设置。"
            elif approval == "disabled":
                message = "Windows 已禁用此启动项。请在任务管理器 → 启动应用中启用本项目的 CodeGPT 项（可能显示为 Windows PowerShell），再重试。"
            elif approval == "unknown":
                message = "Windows 启动应用状态无法确认。请在任务管理器 → 启动应用中检查此 CodeGPT 项后重试。"
            else:
                message = "已登记 Windows 登录后自动启动；后台复用 Setup，不打开浏览器。"
            return {"supported": True, "enabled": bool(effective), "registered": True,
                    "verified": bool(desired_enabled and effective), "message": message}
        except StartupError as error:
            return {"supported": True, "enabled": False, "registered": None,
                    "verified": False, "message": str(error)}
        except OSError:
            return {"supported": True, "enabled": False, "registered": None, "verified": False,
                    "message": "无法验证 Windows 登录启动设置。请检查当前用户权限后重试。"}

    def _write(self, snapshot):
        reg = self._registry
        if snapshot.present:
            with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, RUN_KEY, 0, self._access(write=True)) as key:
                reg.SetValueEx(key, self.entry_name, 0, snapshot.value_type, snapshot.value)
        else:
            try:
                with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY, 0, self._access(write=True)) as key:
                    reg.DeleteValue(key, self.entry_name)
            except FileNotFoundError:
                pass

    def restore(self, previous):
        """Precisely restore only our value after a later configuration failure."""
        if not isinstance(previous, StartupSnapshot) or previous.entry_name != self.entry_name:
            raise StartupError("自动启动恢复状态不属于当前项目，操作已取消。")
        if self._registry is None:
            if previous.present:
                raise StartupError("此系统无法恢复 Windows 登录启动项。")
            return {"verified": True}
        with self._lock:
            try:
                if self.snapshot() != previous:
                    self._write(previous)
                if self.snapshot() != previous:
                    raise StartupError("Windows 启动项恢复后验证失败。请检查注册表权限后重新保存设置。")
            except OSError:
                raise StartupError("无法恢复原 Windows 登录启动项。请检查当前用户注册表权限后重新保存设置。") from None
            return {"verified": True}

    def apply(self, enabled):
        """Set and verify the real OS setting; compensate any failed registration."""
        if type(enabled) is not bool:
            raise StartupError("自动启动开关必须是 true 或 false。")
        if self._registry is None:
            if enabled:
                raise StartupError("自动启动目前仅支持 Windows，请保持关闭并手动运行 setup.bat。")
            return self.status(False)
        with self._lock:
            if enabled:
                command = self._command()
                approval = self._approval()
                if approval != "allowed":
                    if not self.snapshot().present:
                        state = "Windows 保留了此项目以前的禁用状态" if approval == "disabled" else "Windows 保留了此项目无法确认的启动状态"
                        raise StartupError(state + "，但启动项已移除。请先双击 setup.bat 手动启动；在 Windows 恢复此项目的启动权限前，登录自动启动暂时无法开启。")
                    message = ("Windows 已禁用此启动项" if approval == "disabled" else "Windows 启动应用状态无法确认")
                    raise StartupError(message + "。请在任务管理器 → 启动应用中检查并启用此 CodeGPT 项，再重试。")
                desired = StartupSnapshot(self.entry_name, True, command, self._registry.REG_SZ)
            else:
                desired = StartupSnapshot(self.entry_name, False)
            previous = self.snapshot()
            try:
                # Run reconciliation is read-only when the desired entry already
                # matches. Startup must never continually rewrite itself at login.
                if previous != desired:
                    self._write(desired)
                result = self.status(enabled)
                if result["verified"] is not True:
                    raise StartupError(result["message"])
            except (OSError, StartupError):
                try:
                    self.restore(previous)
                except StartupError:
                    raise StartupError("自动启动未生效，原启动项恢复也未完成。请检查当前用户注册表权限后重新保存。") from None
                raise StartupError("Windows 自动启动设置未生效，已恢复原启动项。请检查当前用户权限或任务管理器中的启动应用后重试。") from None
            return result

