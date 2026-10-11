"""Loopback-only setup control plane; MCP runs on a different listener."""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import sys
import secrets
import subprocess
import threading
import time
import tomllib
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from config import (ConfigError, ConfigConflictError, ConfigStore, ensure_config,
                    config_from_data, config_to_dict, dump_toml, load_config, _file_lock, _atomic_write, _fsync_directory, _parse_bytes)
from runtime import Runtime, RuntimeError as ManagedRuntimeError, valid_ngrok_token
from startup import StartupManager
from build_info import compatible_version, process_identity, read_build_manifest, utc_now
from local_agents.credentials import CredentialStore, CredentialError, protect_directory
from local_agents.model_catalog import ModelCatalog
from local_agents.model_options import ModelOptionError
from local_agents.contracts import validation_errors
from local_agents.store import Store, StoreError
from local_agents.scheduler import TERMINAL, ACTIVE

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
PUBLIC_KEYS = {"projects", "server", "tunnel", "startup", "agent"}
PROJECT_KEYS = {"id", "name", "path", "write_enabled", "write_roots", "agent"}
UI_CONTRACT_VERSION = "2.0.0"
UI_ASSET_VERSION = "agent-ui-2.0.0-t18.2"
class ApiError(Exception):
    def __init__(self, status, code, message, hint=""):
        self.status, self.code, self.message, self.hint = status, code, message, hint
        super().__init__(message)


class SetupApplication:
    def __init__(self, config_path, state_dir, setup_port=8169):
        self.config_path = Path(config_path).resolve()
        self.store = ConfigStore(self.config_path)
        self.startup = StartupManager(BASE_DIR, self.config_path, setup_port or 8169)
        self.runtime = Runtime(self.config_path, state_dir)
        self.runtime.startup = self.startup
        self.port = setup_port
        # This process identity is frozen before serving status requests. A release
        # archive carries the commit identity; source checkouts without a manifest
        # report an explicit unknown commit instead of observing a later Git HEAD.
        self.build_manifest = read_build_manifest(BASE_DIR)
        self.process_started_at = utc_now()
        self.version = compatible_version(self.build_manifest)
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.last_snapshot = None
        self.start_error = None
        self.agent_resources = None
        self.agent_lock = threading.RLock()
        self.agent_root = Path(state_dir).resolve() / 'agent'
        self.agent_tasks = None

    def _check_agent_storage_paths(self):
        paths = (self.agent_root, self.agent_root / 'store.sqlite', self.agent_root / 'results')
        if any(p.is_symlink() or p.is_junction() for path in paths for p in (path, *path.parents)):
            raise StoreError('STORAGE_UNAVAILABLE')

    def agent_storage(self):
        # Fixed private service directory; no client-selected storage path.
        with self.agent_lock:
            if self.agent_resources is None:
                self._check_agent_storage_paths()
                root = protect_directory(self.agent_root)
                if self.agent_tasks is None:
                    self.agent_tasks = Store(root / 'store.sqlite', root / 'results')
                tasks = self.agent_tasks
                def revoke(context, revision):
                    # No producer is exposed until Runner is wired and verified.
                    # Existing unfinished work cannot be claimed cancelled here.
                    return not any(t['execution_context']['credential_context_id'] == context
                        and t['execution_context']['credential_revision'] == revision
                        and t['status'] not in TERMINAL
                        for t in tasks.list_tasks())
                self.agent_resources = (CredentialStore(root / 'credentials', revoke=revoke),
                    ModelCatalog(root / 'models.json'), tasks)
            return self.agent_resources

    def agent_catalog(self, *, refresh=False, view=None):
        view = view if view is not None else self.config_view()
        prefs = view['data']['agent']
        codex = prefs['codex']
        credentials, catalog, _ = self.agent_storage()
        mode = codex['auth_mode']
        binding = credentials.binding()
        context = {'auth_mode': mode, 'billing_source': 'chatgpt_plan' if mode == 'chatgpt' else
            ('openai_api' if codex['api_provider'] == 'openai' else 'custom_api') if mode == 'api_key' else None,
            'provider': codex['api_provider'] if mode == 'api_key' else 'openai',
            'endpoint': codex['api_base_url'] if mode == 'api_key' else None,
            'credential_context_id': binding['credential_context_id'] if mode == 'api_key' else None,
            'credential_revision': binding['credential_revision'] if mode == 'api_key' else None,
            'config_revision': view['revision'], 'cli_version': None, 'cli_sha256': None, 'executable': None}
        operation = catalog.refresh if refresh else catalog.view
        return {'ok': True, 'catalog': operation(context,
            enabled=prefs['enabled'] and codex['enabled'],
            saved_model_id=prefs['profiles'][prefs['default_profile']]['model'] or None)}

    def agent_request(self, path, method, body):
        definition = {'/api/agent/models': 'setup_models_refresh_request',
            '/api/agent/probe': 'setup_agent_probe_request', '/api/agent/model-test': 'setup_model_test_request',
            '/api/agent/credential': 'setup_credential_put_request' if method == 'PUT' else 'setup_credential_delete_request'}[path]
        if validation_errors(definition, body):
            raise ApiError(422, 'INVALID_ARGUMENT', '请求字段不正确。', '刷新页面后重新提交。')
        # Serialize config revision and protected-store CAS against external TOML saves.
        with self.lock, _file_lock(self.store.lock_path):
            raw = self.config_path.read_bytes()
            revision = hashlib.sha256(raw).hexdigest()
            if body['revision'] != revision:
                raise ApiError(409, 'REQUEST_CONFLICT', '配置已改变，未执行。', '重新读取当前配置。')
            cfg = config_from_data(_parse_bytes(raw, self.config_path), self.config_path)
            if path == '/api/agent/credential':
                credentials, _, _ = self.agent_storage()
                if method == 'PUT':
                    if cfg.agent['codex']['auth_mode'] != 'api_key':
                        raise ApiError(422, 'AUTH_MODE_UNSELECTED', '尚未选择 API 额度来源。', '先选择并保存 API 配置。')
                    return credentials.put(body, endpoint=cfg.agent['codex']['api_base_url'],
                        provider=cfg.agent['codex']['api_provider'], config_revision=revision)
                return credentials.delete(body, config_revision=revision)
            if path == '/api/agent/models':
                # Keep the accepted preference/revision together after releasing
                # the TOML lock; a later edit cannot change this refresh's identity.
                view = {'revision': revision, 'data': config_to_dict(cfg)}
            elif path == '/api/agent/model-test':
                raise ApiError(503, 'CAPABILITY_UNAVAILABLE', '本机可信 Agent 执行入口尚未启用，未调用模型。',
                    '模型偏好可以保存；Core 项目工具和连接不受影响。公网请求不能启动 Codex。')
            elif path == '/api/agent/probe':
                if cfg.agent['enabled'] and cfg.agent['codex']['enabled']:
                    raise ApiError(503, 'CAPABILITY_UNAVAILABLE', '当前尚未接入可信 CLI 检测器，未启动 Codex。',
                        '可先保存设置；本机可信执行验证完成后再检测。Core 可独立使用。')
                state = self.status()['agent']
                probe = {k: state[k] for k in ('backend', 'availability', 'version', 'auth_state', 'enforcement',
                    'auth_mode', 'billing_source', 'credential_status', 'selected_profile', 'requested_model',
                    'requested_reasoning_effort', 'requested_service_tier', 'model_availability', 'selection_fingerprint')}
                probe.update(checked_at=datetime.now(timezone.utc).isoformat(), errors=[], model_request_performed=False)
                return {'ok': True, 'probe': probe, 'config_revision': revision}
        return self.agent_catalog(refresh=True, view=view)

    def set_port(self, port):
        self.port = port
        self.startup = StartupManager(BASE_DIR, self.config_path, port)
        self.runtime.startup = self.startup

    def config_view(self):
        try:
            snapshot = self.store.read()
            self.last_snapshot = snapshot
        except ConfigError as exc:
            if self.last_snapshot is None:
                raise
            snapshot = self.last_snapshot
            snapshot = dict(snapshot, warning=str(exc))
        data = config_to_dict(config_from_data(snapshot["data"], self.config_path))
        data.setdefault("projects", [])
        data.setdefault("server", {"host": "127.0.0.1", "port": 8168, "path": "/mcp"})
        data.setdefault("tunnel", {"mode": "off", "public_url": ""})
        return {"revision": snapshot["revision"], "data": {k: data[k] for k in PUBLIC_KEYS},
                "config_error": snapshot.get("warning") or getattr(self.store, "last_error", None),
                "canonical": str(self.config_path)}

    def save(self, body):
        if not isinstance(body, dict) or set(body) != {"revision", "data"}:
            raise ApiError(400, "INVALID_REQUEST", "保存请求格式不正确。", "刷新配置页面后重试。")
        data = body["data"]
        if not isinstance(data, dict) or set(data) - PUBLIC_KEYS:
            raise ApiError(400, "INVALID_REQUEST", "页面仅支持项目、连接和开机启动配置。", "高级配置请在本机编辑 TOML。")
        if not isinstance(body["revision"], str) or len(body["revision"]) != 64:
            raise ApiError(400, "INVALID_REVISION", "配置版本缺失。", "重新加载配置后保存。")
        projects = data.get("projects")
        if projects is not None and (not isinstance(projects, list) or any(
                not isinstance(p, dict) or set(p) - PROJECT_KEYS for p in projects)):
            raise ApiError(400, "INVALID_REQUEST", "项目字段不正确。", "仅填写项目标识、名称、目录和写权限。")
        if "server" in data and not isinstance(data["server"], dict):
            raise ApiError(422, "INVALID_REQUEST", "MCP 监听配置必须是对象。", "从页面高级设置中修改。")
        if data.get("server", {}).get("port") == self.port:
            raise ApiError(422, "PORT_CONFLICT", "MCP 端口不能与配置页面端口相同。", "为 MCP 选择其他空闲端口，例如 8168。")
        with self.lock:
            snapshot = self.store.read()
            merged = copy.deepcopy(snapshot["data"])
            merged.update(data)
            result = self.store.save(merged, body["revision"], self.runtime.apply)
            self.start_error = None
            view = self.config_view()
            applied = dict(result.get("applied") or {})
            # Runtime._verify requires owned loaded revision plus MCP initialize
            # and ping before returning this proof. No CLI/model probe is added.
            verified = applied.get("verified") is True and bool(applied.get("revision"))
            applied['checks'] = {'config_loaded': verified, 'local_health': verified,
                                 'auth_discovery': 'not_run', 'agent_capability': 'not_checked'}
            return dict(view, ok=True, applied=applied)

    @property
    def ngrok_token_path(self):
        # Fixed local secret; never accepts a browser-supplied filename or directory.
        return self.config_path.parent / "ngrok-token.txt"

    def ngrok_status(self):
        credential_saved = False
        try:
            credential_saved = (not self.ngrok_token_path.is_symlink()
                                and self.ngrok_token_path.is_file()
                                and self.ngrok_token_path.stat().st_size > 0)
        except OSError:
            pass
        executable = BASE_DIR / ("ngrok.exe" if os.name == "nt" else "ngrok")
        return {"credential_saved": credential_saved,
                "agent_available": executable.is_file() or bool(shutil.which("ngrok"))}

    def cloudflare_status(self):
        executable = BASE_DIR / ("cloudflared.exe" if os.name == "nt" else "cloudflared")
        return {"agent_available": executable.is_file() or bool(shutil.which("cloudflared"))}

    def setup_cloudflare(self, body):
        """Enable or retry one newly owned Quick Tunnel and verify its public MCP."""
        if not isinstance(body, dict) or set(body) != {"revision"}:
            raise ApiError(400, "INVALID_REQUEST", "Cloudflare 启用请求格式不正确。", "刷新页面后重试；此操作仅接受当前配置版本。")
        revision = body["revision"]
        if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{64}", revision):
            raise ApiError(400, "INVALID_REVISION", "配置版本缺失。", "重新读取配置后重试。")
        with self.lock:
            if self.ngrok_recovery_path.exists():
                raise ApiError(503, "CREDENTIAL_RECOVERY_REQUIRED", "上次凭据更新尚未完成恢复。", "检查配置目录的写权限，关闭启动窗口，再双击 setup.bat。请不要只重启 MCP 与隧道。")
            snapshot = self.store.read()
            if revision != snapshot["revision"]:
                raise ConfigConflictError("配置已被其他窗口或进程修改。请重新读取后重试。")
            if not self.cloudflare_status()["agent_available"]:
                raise ApiError(503, "CLOUDFLARE_NOT_INSTALLED", "本机没有找到 cloudflared 程序。", "从 https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/downloads/ 下载官方程序，放入项目目录或 PATH 后重试。")
            desired = copy.deepcopy(snapshot["data"])
            # Quick Tunnel addresses come only from this newly owned connector.
            # A previous ngrok/fixed URL must never become a Cloudflare fallback.
            desired["tunnel"] = {"mode": "cloudflare", "public_url": ""}
            config_from_data(desired, self.config_path)
            rollback_attempted = False
            rollback_verified = False
            failure_hint = "检查外网连接和防火墙是否允许 cloudflared 连接 Cloudflare，确认配置目录可写，然后点击重试。重建临时隧道后地址可能变化，请同步更新 ChatGPT 中的 MCP URL。"

            def restore(config):
                nonlocal rollback_attempted, rollback_verified
                rollback_attempted = True
                # Restore even for identical configs or a failed candidate write.
                # Local verification must not wait for public connectivity again.
                restored = self.runtime.apply(config, force_tunnel_restart=True)
                rollback_verified = ConfigStore._accepted(restored)
                return restored

            def apply(config):
                nonlocal failure_hint
                try:
                    applied = self.runtime.apply(config, force_tunnel_restart=True)
                    if not isinstance(applied, dict) or applied.get("verified") is not True:
                        raise RuntimeError("Local MCP 尚未通过协议验证。")
                    public = self.runtime.verify_public(config, timeout=30)
                    if not isinstance(public, dict) or public.get("public_verified") is not True:
                        raise RuntimeError("公网 MCP 尚未通过协议验证。")
                    address = public.get("public_url")
                    if not isinstance(address, str):
                        raise RuntimeError("尚未取得本次 Cloudflare 隧道的公网地址。")
                    parsed = urlsplit(address)
                    if (parsed.scheme != "https" or parsed.username is not None
                            or parsed.password is not None or parsed.port is not None
                            or not re.fullmatch(r"[a-z0-9-]+\.trycloudflare\.com", parsed.hostname or "")
                            or parsed.path != config.server.path or parsed.query or parsed.fragment):
                        raise RuntimeError("本次 Cloudflare 公网 MCP 地址尚未通过验证。")
                    return {"verified": True, "action": applied.get("action"),
                            "public_verified": True, "public_url": address,
                            "public_latency_ms": public.get("public_latency_ms")}
                except Exception as error:
                    if isinstance(error, ManagedRuntimeError):
                        # RuntimeError publishes predefined advice, never raw logs.
                        failure_hint = str(error)
                    raise RuntimeError("Cloudflare 启用未完成；检查外网连接和防火墙后重试。") from None
            try:
                # CAS and atomic TOML replacement also apply when bytes are unchanged.
                result = self.store.save(desired, revision, apply, rollback_callback=restore)
            except ConfigConflictError:
                raise
            except Exception:
                if rollback_attempted and not rollback_verified:
                    failure_hint += " 上一份配置的后端恢复尚未确认；请检查本机 MCP 端口与网络，关闭启动窗口，再双击 setup.bat。"
                raise ApiError(503, "CLOUDFLARE_SETUP_FAILED", "Cloudflare 未启用成功，已尝试恢复上一份有效配置。", failure_hint) from None
            self.start_error = None
            return dict(self.config_view(), ok=True, applied=result["applied"])

    @property
    def ngrok_recovery_path(self):
        # Fixed and permanently denied by MCP, even when .setup itself is a project.
        return self.config_path.parent / ".setup" / "ngrok-credential.pending"

    def _clear_ngrok_recovery(self):
        self.ngrok_recovery_path.unlink(missing_ok=True)
        _fsync_directory(self.ngrok_recovery_path.parent)

    def recover_ngrok_credentials(self, apply_callback=None):
        """Recover an interrupted transaction only after obtaining the owner lock.

        Until deleting the record the one-click operation is uncommitted, even
        if MCP verification completed immediately before an owner crash.
        """
        record_path = self.ngrok_recovery_path
        if not record_path.exists():
            return False
        if (record_path.is_symlink() or record_path.parent.is_symlink()
                or self.ngrok_token_path.is_symlink()):
            raise ConfigError("凭据恢复文件不能是链接。请检查本机 .setup 与 ngrok-token.txt 后重启。")
        with _file_lock(self.store.lock_path):
            try:
                record = json.loads(record_path.read_bytes())
                if (not isinstance(record, dict) or set(record) != {
                        "version", "previous_exists", "previous_token", "previous_config", "candidate_revision"}
                        or record["version"] != 1 or type(record["previous_exists"]) is not bool
                        or not isinstance(record["candidate_revision"], str)
                        or not re.fullmatch(r"[a-f0-9]{64}", record["candidate_revision"])):
                    raise ValueError()
                previous_config = base64.b64decode(record["previous_config"], validate=True)
                previous_token = base64.b64decode(record["previous_token"], validate=True)
                previous = config_from_data(tomllib.loads(previous_config.decode("utf-8")), self.config_path)
                current = self.config_path.read_bytes()
                good = self.store.last_good_path.read_bytes()
            except (OSError, ValueError, TypeError, KeyError, ConfigError):
                raise ConfigError("上次 ngrok 凭据更新未完成，恢复记录不可用。请检查本机配置目录的权限和 last-good 文件后重启；请勿删除原凭据备份。") from None
            matching = {hashlib.sha256(previous_config).hexdigest(), record["candidate_revision"]}
            if (hashlib.sha256(current).hexdigest() not in matching
                    or hashlib.sha256(good).hexdigest() not in matching):
                raise ConfigConflictError("上次 ngrok 更新未完成，但 TOML 已被其他程序修改。为避免覆盖新配置，自动恢复已停止；先备份当前 TOML，再恢复 config.last-good.toml 后重启。")
            try:
                if record["previous_exists"]:
                    _atomic_write(self.ngrok_token_path, previous_token)
                    if os.name != "nt":
                        self.ngrok_token_path.chmod(0o600)
                else:
                    self.ngrok_token_path.unlink(missing_ok=True)
                _atomic_write(self.config_path, previous_config)
                _atomic_write(self.store.last_good_path, previous_config)
                self.store.pending_path.unlink(missing_ok=True)
                _fsync_directory(self.config_path.parent)
                if apply_callback is not None:
                    applied = apply_callback(previous)
                    if not ConfigStore._accepted(applied):
                        raise RuntimeError("上一份配置尚未重新连接。")
                self._clear_ngrok_recovery()
            except Exception:
                raise ConfigError("旧配置与凭据恢复尚未完成。请检查配置目录的写权限、MCP 端口和网络，关闭启动窗口，再双击 setup.bat。") from None
            self.store.last_error = "上次 ngrok 更新未完成，已恢复最后一次有效配置和原凭据。"
            self.last_snapshot = None
            return True

    def setup_ngrok(self, body):
        """Write-only credentials + canonical configuration + real public MCP verification."""
        if (not isinstance(body, dict) or set(body) - {"revision", "token", "public_url"}
                or "revision" not in body):
            raise ApiError(400, "INVALID_REQUEST", "ngrok 配置请求格式不正确。", "刷新页面后重试。")
        revision = body["revision"]
        if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{64}", revision):
            raise ApiError(400, "INVALID_REVISION", "配置版本缺失。", "重新读取配置后重试。")
        submitted = body.get("token", "")
        if not isinstance(submitted, str) or any(ord(c) < 32 or ord(c) == 127 for c in submitted):
            raise ApiError(422, "INVALID_NGROK_TOKEN", "请粘贴 ngrok Authtoken。", "只复制完整 Token；不能包含换行、附加参数或其他命令。")
        submitted = submitted.strip()
        command = re.fullmatch(r"ngrok config add-authtoken ([A-Za-z0-9_-]+)", submitted)
        if command:
            submitted = command.group(1)
        if submitted and not valid_ngrok_token(submitted):
            raise ApiError(422, "INVALID_NGROK_TOKEN", "Authtoken 格式不正确。", "粘贴原始 Token，或不带引号的 ngrok config add-authtoken 命令；不要附加其他参数。")
        if "public_url" in body and not isinstance(body["public_url"], str):
            raise ApiError(422, "INVALID_PUBLIC_URL", "公网地址必须是 HTTPS 字符串。", "没有固定域名时留空，由 ngrok 自动分配。")
        with self.lock:
            if self.ngrok_recovery_path.exists():
                raise ApiError(503, "CREDENTIAL_RECOVERY_REQUIRED", "上次凭据更新尚未完成恢复。", "检查配置目录的写权限，关闭启动窗口，再双击 setup.bat。请不要只重启 MCP 与隧道。")
            snapshot = self.store.read()
            if revision != snapshot["revision"]:
                raise ConfigConflictError("配置已被其他窗口或进程修改。请重新读取后重试。")
            if self.ngrok_token_path.is_symlink() or self.ngrok_recovery_path.parent.is_symlink():
                raise ApiError(422, "UNSAFE_CREDENTIAL_FILE", "本地凭据文件或恢复目录不能是链接。", "移除本机 ngrok-token.txt 或 .setup 链接后重新配置。")
            status = self.ngrok_status()
            if not status["agent_available"]:
                raise ApiError(503, "NGROK_NOT_INSTALLED", "本机没有找到 ngrok 程序。", "从 https://ngrok.com/download 下载官方程序，放入项目目录或 PATH 后重试。")
            if not submitted and not status["credential_saved"]:
                raise ApiError(422, "NGROK_TOKEN_REQUIRED", "首次启用需要 ngrok Authtoken。", "从 ngrok 账户复制 Authtoken，粘贴到凭据框后重试。")
            desired = copy.deepcopy(snapshot["data"])
            old_tunnel = desired.get("tunnel", {})
            desired["tunnel"] = {**old_tunnel, "mode": "ngrok",
                                 "public_url": old_tunnel.get("public_url", "") if old_tunnel.get("mode") == "ngrok" else ""}
            if "public_url" in body:
                desired["tunnel"]["public_url"] = body["public_url"].strip()
            # Complete validation precedes any secret or canonical-file mutation.
            config_from_data(desired, self.config_path)
            candidate_revision = hashlib.sha256(dump_toml(desired).encode("utf-8")).hexdigest()
            original_secret = None
            original_exists = False
            secret_attempted = False
            candidate_attempted = False
            rollback_verified = False
            failure_hint = "确认 Authtoken 有效、账户可用和外网连通；恢复未完成时检查本机写权限，再受控重启。"

            def restore_secret():
                if not secret_attempted:
                    return
                if original_exists:
                    _atomic_write(self.ngrok_token_path, original_secret)
                    if os.name != "nt":
                        self.ngrok_token_path.chmod(0o600)
                else:
                    self.ngrok_token_path.unlink(missing_ok=True)

            def apply(config):
                nonlocal original_secret, original_exists, secret_attempted, candidate_attempted, rollback_verified, failure_hint
                if candidate_attempted:
                    # ConfigStore calls this again after restoring canonical last-good.
                    # Put the old credential back BEFORE reconnecting that backend.
                    try:
                        restore_secret()
                        restored = self.runtime.apply(config, force_tunnel_restart=True)
                        rollback_verified = ConfigStore._accepted(restored)
                        return restored
                    except Exception:
                        raise RuntimeError("上一份有效配置的重连未完成；请检查本机网络后受控重启。") from None
                candidate_attempted = True
                try:
                    if submitted:
                        original_exists = self.ngrok_token_path.is_file()
                        if original_exists:
                            original_secret = self.ngrok_token_path.read_bytes()
                        previous_config = self.store.last_good_path.read_bytes()
                        record = {"version": 1, "previous_exists": original_exists,
                                  "previous_token": base64.b64encode(original_secret or b"").decode("ascii"),
                                  "previous_config": base64.b64encode(previous_config).decode("ascii"),
                                  "candidate_revision": candidate_revision}
                        # Durable undo state precedes replacing the credential.
                        _atomic_write(self.ngrok_recovery_path, json.dumps(record).encode("ascii"))
                        if os.name != "nt":
                            self.ngrok_recovery_path.chmod(0o600)
                        secret_attempted = True
                        _atomic_write(self.ngrok_token_path, submitted.encode("ascii"))
                        if os.name != "nt":
                            self.ngrok_token_path.chmod(0o600)
                    # Every one-click attempt verifies a newly owned connector.
                    applied = self.runtime.apply(config, force_tunnel_restart=True)
                    public = self.runtime.verify_public(config, timeout=30)
                    if (not isinstance(applied, dict) or applied.get("verified") is not True
                            or not isinstance(public, dict) or public.get("public_verified") is not True
                            or not isinstance(public.get("public_url"), str)
                            or not public["public_url"].startswith("https://")):
                        raise RuntimeError("公网 MCP 尚未通过协议验证。")
                    return {"verified": True, "action": applied.get("action"),
                            "public_verified": True, "public_url": public["public_url"],
                            "public_latency_ms": public.get("public_latency_ms")}
                except Exception as error:
                    if isinstance(error, ManagedRuntimeError):
                        # This class contains only predefined advice and safe codes.
                        failure_hint = str(error)
                    try:
                        restore_secret()
                    except OSError:
                        raise RuntimeError("启用失败，旧凭据未能恢复；请检查本机凭据文件的写权限后重试。") from None
                    # Never include a library exception, raw agent log or submitted token.
                    raise RuntimeError("ngrok 启用未完成；请确认 Authtoken 有效、账户状态正常且能连接外网，然后重试。") from None
            try:
                # The callback writes the credential only after ConfigStore's locked
                # revision check, so stale requests never touch an existing credential.
                result = self.store.save(desired, revision, apply)
                if submitted:
                    try:
                        self._clear_ngrok_recovery()
                    except OSError:
                        # Undo state must never remain after an API claims success.
                        self.recover_ngrok_credentials(
                            lambda config: self.runtime.apply(config, force_tunnel_restart=True))
                        raise RuntimeError("本机凭据事务未能提交，请检查配置目录的写权限。")
            except ConfigConflictError:
                raise
            except Exception:
                if rollback_verified and self.ngrok_recovery_path.exists():
                    try:
                        self._clear_ngrok_recovery()
                    except OSError:
                        pass  # Durable recovery stays available for the next startup.
                raise ApiError(503, "NGROK_SETUP_FAILED", "ngrok 未启用成功，已尝试恢复上一份配置与凭据。",
                               failure_hint) from None
            self.start_error = None
            return dict(self.config_view(), ok=True, credential_saved=True, applied=result["applied"])

    def status(self):
        result = self.runtime.status()
        result["version"] = dict(self.version)
        result.setdefault("config", {})
        result["config"]["revision"] = (self.last_snapshot or {}).get("revision")
        mcp_identity = result.get("mcp", {})
        effective_revision = (mcp_identity.get("effective_config_version")
                              if mcp_identity.get("pid") is not None else None)
        result["setup"] = process_identity("setup", self.build_manifest, os.getpid(),
            self.process_started_at, effective_revision)
        if self.start_error:
            result["config"]["error"] = self.start_error
        # Status must not wait for a slow ConfigStore transaction. Atomic TOML
        # reads observe complete files; malformed external edits retain last-good.
        desired_enabled = bool((self.last_snapshot or {}).get("data", {}).get("startup", {}).get("enabled", False))
        cfg = None
        try:
            cfg = load_config(self.config_path)
            desired_enabled = cfg.startup.enabled
        except ConfigError:
            pass
        result["startup"] = self.startup.status(desired_enabled)
        cfg = cfg or config_from_data((self.last_snapshot or self.store.read())["data"], self.config_path)
        preference = cfg.agent
        codex = preference['codex']
        profile = preference['profiles'][preference['default_profile']]
        enabled = preference['enabled'] and codex['enabled']
        mode = codex['auth_mode']
        credential_error = None
        # Default Core polling must not create an Agent DB, DPAPI file or ACL.
        # An explicit API selection or opened optional settings loads it lazily.
        credential = {'configured': False, 'credential_revision': 'unselected', 'storage': 'none',
                      'bound_endpoint': None, 'status': 'unknown', 'updated_at': None}
        try:
            if mode == 'api_key' or self.agent_resources is not None:
                credentials, _, _ = self.agent_storage()
                credential = credentials.status(endpoint=codex['api_base_url'] if mode == 'api_key' else None)
        except (CredentialError, StoreError) as exc:
            # The optional protected key store must not hide legacy MCP health.
            # "unavailable" is no valid CAS token and cannot authorize a mutation.
            credential = {'configured': False, 'credential_revision': 'unavailable', 'storage': 'none',
                          'bound_endpoint': None, 'status': 'unavailable', 'updated_at': None}
            credential_error = {'code': 'CREDENTIAL_UNAVAILABLE', 'message': 'Agent 凭据存储不可用；原 MCP 状态仍可查看。',
                'recoverable': True, 'hint': '保留受保护凭据文件；修复其权限或存储后重试。模型执行已拒绝。',
                'task_id': None, 'retriable': True}
            if isinstance(exc, StoreError):
                credential_error = {'code': 'STORAGE_UNAVAILABLE', 'message': '已有 Agent 任务存储不可用；Core 不受影响。',
                    'recoverable': True, 'hint': '保留任务存储和备份，核对兼容版本；未启动或重放任务。',
                    'task_id': None, 'retriable': False}
        # Existing accepted work remains visible without loading credentials or
        # enabling a scheduler. A new Core installation creates no Agent store.
        try:
            with self.agent_lock:
                if self.agent_tasks is None and (self.agent_root / 'store.sqlite').is_file():
                    self._check_agent_storage_paths()
                    self.agent_tasks = Store(self.agent_root / 'store.sqlite', self.agent_root / 'results')
                current_tasks = self.agent_tasks.list_tasks() if self.agent_tasks is not None else []
        except StoreError:
            current_tasks = []
            credential_error = {'code': 'STORAGE_UNAVAILABLE', 'message': '已有 Agent 任务存储不可用；Core 不受影响。',
                'recoverable': True, 'hint': '保留任务存储和备份，核对兼容版本；未启动或重放任务。',
                'task_id': None, 'retriable': False}
        result['agent'] = {'backend': 'codex', 'enabled': enabled,
            'availability': 'capability_unavailable' if enabled else 'disabled',
            'version': None, 'auth_state': 'unknown',
            'enforcement': {'state': 'unavailable', 'mechanisms': [], 'tested_boundaries': []},
            'checked_at': None, 'active_tasks': sum(t['status'] in ACTIVE for t in current_tasks),
            'queued_tasks': sum(t['status'] == 'queued' for t in current_tasks), 'last_error': credential_error,
            'auth_mode': mode, 'billing_source': 'chatgpt_plan' if mode == 'chatgpt' else
                ('openai_api' if codex['api_provider'] == 'openai' else 'custom_api') if mode == 'api_key' else 'unselected',
            'credential_status': credential, 'selected_profile': preference['default_profile'],
            'requested_model': profile['model'] or None, 'requested_reasoning_effort': profile['reasoning_effort'] or None,
            'requested_service_tier': profile['service_tier'], 'model_availability': 'unverified' if profile['model'] else 'unselected',
            'selection_fingerprint': None, 'last_model_test': None}
        result['auth'] = {'mode': 'none', 'resource': None, 'issuer': None, 'discovery_state': 'unknown',
                          'chatgpt_connection': 'unknown', 'last_verified_at': None}
        sampled = result['history'][-1]['time'] if result.get('history') else None
        for name in ('local', 'public'):
            network = result.get(name)
            if not isinstance(network, dict):
                continue
            online = network['state'] == 'online'
            observed = online or network['state'] == 'offline'
            network.update(reachability='reachable' if online else 'unknown',
                mcp='ready' if online else 'not_checked', auth='not_required' if online else 'unknown',
                checked_at=datetime.fromtimestamp(sampled, timezone.utc).isoformat() if observed and sampled else None,
                http_status=None)
        return result


class SetupServer(ThreadingHTTPServer):
    daemon_threads = True
    # Windows SO_REUSEADDR permits a second listener to steal the same address.
    # Unix needs reuse for immediate restart after HTTP connections enter TIME_WAIT.
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(self, address, app):
        super().__init__(address, SetupHandler)
        self.app = app
        app.set_port(self.server_port)


class SetupHandler(BaseHTTPRequestHandler):
    server_version = "CodeGPTSetup"
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):
        # No bodies, auth headers or secret values in access logs.
        pass

    def _guard(self, mutate=False):
        try:
            local = ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            local = False
        hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if not local or self.headers.get("Host", "").lower() not in hosts:
            raise ApiError(403, "LOCAL_ONLY", "配置页面仅允许从本机直接访问。", "使用 http://127.0.0.1 的配置页面地址。")
        if any(k.lower().startswith("x-forwarded-") or k.lower() == "forwarded" for k in self.headers.keys()):
            raise ApiError(403, "PROXY_FORBIDDEN", "配置页面不接受代理或 Tunnel 请求。", "Tunnel 应仅指向 MCP 端口。")
        origin = self.headers.get("Origin")
        if origin and origin.lower() not in {"http://" + h for h in hosts}:
            raise ApiError(403, "ORIGIN_FORBIDDEN", "请求来源不是本机配置页面。", "从本机地址重新打开页面。")
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise ApiError(403, "ORIGIN_FORBIDDEN", "禁止跨网站访问配置接口。", "从本机地址重新打开页面。")
        if mutate and not secrets.compare_digest(self.headers.get("X-Setup-Token", ""), self.server.app.token):
            raise ApiError(403, "TOKEN_REQUIRED", "配置会话已过期或缺失。", "刷新页面后重试。")

    def _send(self, status, body, mime="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status, body):
        """Deliver rejection before bounded transport cleanup, without parsing input.

        Closing a Windows socket while a fragmented request body is still arriving
        can replace the HTTP error with a TCP reset. Half-close the response side,
        then discard the declared remainder while retaining strict time/byte bounds.
        """
        self.close_connection = True
        try:
            self._send(status, body)
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_WR)
        except OSError:
            return  # The client has already disconnected.
        lengths = self.headers.get_all("Content-Length", [])
        try:
            length = int(lengths[0]) if len(lengths) == 1 and not self.headers.get("Transfer-Encoding") else -1
        except ValueError:
            length = -1
        # JSON acceptance remains capped at 64 KiB. Transport cleanup can discard
        # at most 128 KiB, including a small oversized request rejected with 413.
        budget = 131072
        remaining = (budget if length < 0 else
                     min(budget, max(0, length - getattr(self, "_body_consumed", 0))))
        deadline = time.monotonic() + 1
        while remaining > 0:
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                break
            try:
                self.connection.settimeout(timeout)
                discarded = self.rfile.read1(min(8192, remaining))
            except OSError:
                break
            if not discarded:
                break
            remaining -= len(discarded)

    def _json_body(self):
        if self.headers.get_content_type() != "application/json":
            raise ApiError(415, "JSON_REQUIRED", "此接口只接受 JSON。", "从配置页面提交。")
        if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) > 1:
            raise ApiError(400, "INVALID_BODY_FRAMING", "请求体的传输格式不明确。", "此接口只接受一个明确长度的 JSON 请求；请从配置页面重新提交。")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if not 0 < length <= 65536:
            raise ApiError(413, "REQUEST_SIZE", "配置请求为空或过大（上限 64 KB）。", "减少项目数量或字段长度后重试。")
        try:
            raw = self.rfile.read(length)
            self._body_consumed = len(raw)
            if len(raw) != length:
                raise ApiError(400, "INCOMPLETE_BODY", "配置请求尚未完整接收。", "连接可能中断；请重新加载配置页面后再提交。")
            return json.loads(raw)
        except (ValueError, UnicodeError):
            raise ApiError(400, "INVALID_JSON", "配置请求无法解析。", "刷新页面后重试。")

    def _dispatch(self):
        self.connection.settimeout(15)
        mutate = self.command in {"POST", "PUT", "DELETE"}
        self._guard(mutate)
        path = urlsplit(self.path).path
        app = self.server.app
        if self.command == "GET":
            if path == "/api/session":
                return self._send(200, {"token": app.token})
            if path == "/api/info":
                return self._send(200, {"name": "local-codebase-mcp-setup", "product": "CodeGPT", "config": str(app.config_path),
                    'ui_contract_version': UI_CONTRACT_VERSION, 'ui_asset_version': UI_ASSET_VERSION})
            if path == "/api/config":
                return self._send(200, app.config_view())
            if path == "/api/ngrok":
                return self._send(200, app.ngrok_status())
            if path == "/api/cloudflare":
                return self._send(200, app.cloudflare_status())
            if path == "/api/status":
                return self._send(200, app.status())
            if path == '/api/agent/credential':
                view = app.config_view()
                codex = view['data']['agent']['codex']
                return self._send(200, {'ok': True, 'credential': app.agent_storage()[0].status(
                    endpoint=codex['api_base_url'] if codex['auth_mode'] == 'api_key' else None), 'config_revision': view['revision']})
            if path == '/api/agent/models':
                return self._send(200, app.agent_catalog())
            assets = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/index.html": ("index.html", "text/html; charset=utf-8"),
                      "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/style.css": ("style.css", "text/css; charset=utf-8")}
            if path in assets:
                name, mime = assets[path]
                return self._send(200, (WEB_DIR / name).read_bytes(), mime)
        if path in {'/api/agent/credential', '/api/agent/models', '/api/agent/probe', '/api/agent/model-test'}:
            allowed = {'/api/agent/credential': {'PUT', 'DELETE'}, '/api/agent/models': {'POST'},
                       '/api/agent/probe': {'POST'}, '/api/agent/model-test': {'POST'}}
            if self.command not in allowed[path]:
                raise ApiError(405, 'METHOD_NOT_ALLOWED', '此操作不支持该请求方法。')
            if (self.headers.get('X-UI-Contract-Version') != UI_CONTRACT_VERSION or
                    self.headers.get('X-UI-Asset-Version') != UI_ASSET_VERSION):
                raise ApiError(409, 'UI_VERSION_MISMATCH', '页面版本已过期，未执行。', '刷新页面后重试。')
            return self._send(200, app.agent_request(path, self.command, self._json_body()))
        if self.command == "PUT" and path == "/api/config":
            body = self._json_body()
            data = body.get('data', {}) if isinstance(body, dict) else {}
            # A legacy round-trip may echo new fields unchanged. Only changes
            # require the new asset contract; old clients can still save old fields.
            current = app.config_view()['data'] if isinstance(data, dict) else {}
            projects = data.get('projects', []) if isinstance(data, dict) else []
            existing = {p['id']: p.get('agent') for p in current.get('projects', [])}
            changes_agent = isinstance(data, dict) and 'agent' in data and data['agent'] != current.get('agent')
            changes_project_agent = isinstance(projects, list) and any(isinstance(p, dict) and 'agent' in p
                and p['agent'] != existing.get(p.get('id')) for p in projects)
            if changes_agent or changes_project_agent:
                if (self.headers.get('X-UI-Contract-Version') != UI_CONTRACT_VERSION or
                        self.headers.get('X-UI-Asset-Version') != UI_ASSET_VERSION):
                    raise ApiError(409, 'UI_VERSION_MISMATCH', '页面版本已过期，未保存。', '刷新页面后重新读取配置。')
            return self._send(200, app.save(body))
        if self.command == "POST":
            body = self._json_body()
            if path == "/api/ngrok":
                return self._send(200, app.setup_ngrok(body))
            if path == "/api/cloudflare":
                return self._send(200, app.setup_cloudflare(body))
            if path == "/api/probe":
                app.runtime.probe()
                return self._send(200, app.status())
            if path == "/api/restart":
                with app.lock:
                    result = app.runtime.restart()
                app.start_error = None
                return self._send(200, {"ok": True, "applied": result})
            if path == "/api/stop":
                self._send(200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
        raise ApiError(404, "NOT_FOUND", "未找到该页面或接口。", "从配置页面首页操作。")

    def _handle(self):
        self._body_consumed = 0
        try:
            self._dispatch()
        except ApiError as exc:
            self._send_error(exc.status, {"error": {"code": exc.code, "message": exc.message, "hint": exc.hint}})
        except ConfigError as exc:
            name = type(exc).__name__
            status = 409 if "Conflict" in name else (503 if "Apply" in name else 422)
            self._send_error(status, {"error": {"code": name, "message": str(exc),
                       "hint": "重新读取配置再提交；路径错误请确认本机目录存在。应用失败时已尝试恢复上一份有效配置。"}})
        except (CredentialError, ModelOptionError, StoreError) as exc:
            status = 409 if exc.code in {'REQUEST_CONFLICT', 'CREDENTIAL_CONFLICT'} else 422 if exc.code in {'INVALID_ARGUMENT', 'ENDPOINT_NOT_APPROVED'} else 503
            self._send_error(status, {'error': {'code': exc.code, 'message': '本地 Agent 操作未完成。',
                'hint': '配置冲突请重新读取；凭据清理失败请保留当前状态，待受管进程清理后重试。未调用模型。'}})
        except (OSError, RuntimeError, ValueError, TypeError) as exc:
            # Runtime errors are intentionally actionable; never return traceback or logs.
            message = str(exc) if isinstance(exc, RuntimeError) else "操作未完成。"
            self._send_error(503, {"error": {"code": "OPERATION_FAILED", "message": message,
                       "hint": "检查本机 MCP 端口、目录权限及隧道状态，再点击重新探测或受控重启。"}})

    do_GET = do_POST = do_PUT = do_DELETE = do_OPTIONS = _handle


def main():
    # Logs and launcher pipes must stay UTF-8 even under the Windows system ACP.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="CodeGPT 本机配置页面")
    parser.add_argument("--config", type=Path, default=BASE_DIR / "config.toml")
    parser.add_argument("--port", type=int, default=8169, help="Setup localhost 端口")
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--mcp-port", type=int, default=None)
    parser.add_argument("--tunnel", choices=["off", "cloudflare", "ngrok"], default=None)
    parser.add_argument("--open", action="store_true", help="打开系统浏览器")
    args = parser.parse_args()
    url = f"http://127.0.0.1:{args.port}"
    server = None
    instance_guard = None
    owner_acquired = False
    try:
        app = SetupApplication(args.config, args.state_dir or args.config.parent / ".setup", args.port)
        server = SetupServer(("127.0.0.1", args.port), app)
        instance_guard = _file_lock(args.config.with_name(args.config.name + ".setup-owner.lock"), timeout=0.5)
        try:
            instance_guard.__enter__()
            owner_acquired = True
        except ConfigError:
            server.server_close()
            app.runtime.stop()
            print("同一配置已经由另一个 Setup 管理。请使用原配置页面，或关闭原启动窗口后重试。", flush=True)
            raise SystemExit(2)
        # A duplicate owner must never recover another live owner's transaction.
        app.recover_ngrok_credentials()
        ensure_config(args.config)
        app.config_view()
    except OSError:
        if server is not None:
            server.server_close()
            app.runtime.stop()
        if owner_acquired:
            instance_guard.__exit__(None, None, None)
        # Repeated double-clicks reuse this exact config's existing Setup process.
        try:
            import urllib.request
            local_client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with local_client.open(url + "/api/info", timeout=2) as response:
                info = json.load(response)
            if info.get("name") == "local-codebase-mcp-setup" and info.get("config") == str(args.config.resolve()):
                if args.open:
                    webbrowser.open(url)
                print(f"配置页面已经运行：{url}", flush=True)
                return
        except Exception:
            pass
        print(f"无法启动配置页面：端口 {args.port} 被占用或目录不可写。关闭冲突程序或使用 --port 指定其他端口。", flush=True)
        raise SystemExit(2)
    except ConfigError as exc:
        if server is not None:
            server.server_close()
            app.runtime.stop()
        if owner_acquired:
            instance_guard.__exit__(None, None, None)
        print(f"配置无法加载：{exc}\n请修复 config.toml，或恢复 config.last-good.toml 后重试。", flush=True)
        raise SystemExit(2)
    try:
        if app.config_view()["data"]["server"].get("port") == server.server_port:
            app.start_error = "MCP 与 Setup 端口冲突。请在页面高级设置中为 MCP 选择其他端口。"
        else:
            try:
                app.runtime.start()
            except (RuntimeError, OSError, ConfigError) as exc:
                app.start_error = str(exc)
        if args.mcp_port is not None or args.tunnel is not None:
            try:
                view = app.config_view()
                data = view["data"]
                if args.mcp_port is not None:
                    data["server"]["port"] = args.mcp_port
                if args.tunnel is not None:
                    data["tunnel"]["mode"] = args.tunnel
                app.save({"revision": view["revision"], "data": data})
            except (ApiError, ConfigError, RuntimeError) as exc:
                app.start_error = str(exc)
        print(f"CodeGPT 本机配置页面：{url}\n用户配置：{args.config.resolve()}\n关闭此窗口将停止本次启动的 MCP 与 Tunnel。", flush=True)
        if args.open:
            webbrowser.open(url)
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        app.runtime.stop()
        server.server_close()
        instance_guard.__exit__(None, None, None)


if __name__ == "__main__":
    main()
