"""Focused public-monitor/verification seams; provider E2E has separate real evidence."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from config import Config, ServerConfig, TunnelConfig, config_to_dict, dump_toml
import runtime as module
from runtime import Runtime, RuntimeError


def active_runtime(tmp_path, mode="cloudflare", public_url=""):
    config = Config(projects=(), server=ServerConfig(port=12345),
                    tunnel=TunnelConfig(mode=mode, public_url=public_url))
    path = tmp_path / "config.toml"
    path.write_text(dump_toml(config_to_dict(config)), encoding="utf-8")
    instance = Runtime(path, tmp_path / "state", startup=None, probe_interval=60)
    # Input to this focused sampler is an already-applied generation, no process is started.
    instance._config = config
    return instance, config


@pytest.mark.parametrize("stale_url", [
    "https://old-account.ngrok-free.dev/mcp", "https://unrelated.example/mcp",
    "https://previous-session.trycloudflare.com/mcp",
])
def test_cloudflare_sampler_never_probes_canonical_public_url_before_owned_discovery(tmp_path, monkeypatch, stale_url):
    instance, _ = active_runtime(tmp_path, public_url=stale_url)
    calls = []
    def observed(url, **_kwargs):
        calls.append(url)
        return {"latency_ms": 12}
    monkeypatch.setattr(module, "probe_mcp", observed)
    result = instance.probe()
    assert calls == ["http://127.0.0.1:12345/mcp"]
    assert result["public"]["state"] == "unconfigured"
    assert result["public"]["url"] == ""


def test_cloudflare_sampler_uses_this_owned_log_url_and_ngrok_keeps_configured_url(tmp_path, monkeypatch):
    instance, _ = active_runtime(tmp_path, public_url="https://stale.example/mcp")
    actual = "https://observed-session.trycloudflare.com/mcp"
    (instance.state_dir / "tunnel.log").write_text(f"Your quick Tunnel: {actual.removesuffix('/mcp')}\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(module, "probe_mcp", lambda url, **kwargs: calls.append(url) or {"latency_ms": 1})
    assert instance.probe()["public"]["url"] == actual
    assert calls[-1] == actual and "https://stale.example/mcp" not in calls
    other = tmp_path / "ngrok"
    other.mkdir()
    ngrok, _ = active_runtime(other, mode="ngrok", public_url="https://account.ngrok-free.dev/mcp")
    assert ngrok.probe()["public"]["url"] == "https://account.ngrok-free.dev/mcp"


@pytest.mark.parametrize("mode,provider", [("cloudflare", "Cloudflare"), ("ngrok", "ngrok")])
def test_public_timeout_advice_matches_selected_provider(tmp_path, monkeypatch, mode, provider):
    instance, config = active_runtime(tmp_path, mode=mode)
    monkeypatch.setattr(instance, "probe", lambda: {})
    with pytest.raises(RuntimeError) as failure:
        instance.verify_public(config, timeout=0)
    message = str(failure.value)
    assert provider in message
    if mode == "cloudflare":
        assert "ngrok" not in message and "填写账号" not in message
        assert "网络" in message or "外网" in message


@pytest.mark.parametrize("mode,provider", [("cloudflare", "Cloudflare"), ("ngrok", "ngrok")])
def test_public_project_mismatch_advice_matches_selected_provider(tmp_path, monkeypatch, mode, provider):
    instance, config = active_runtime(tmp_path, mode=mode)
    instance._tunnel = SimpleNamespace(pid=0, poll=lambda: None)
    instance._discovered_url = ("https://current-session.trycloudflare.com/mcp" if mode == "cloudflare"
                                else "https://account.ngrok-free.dev/mcp")
    instance._tunnel_connections.add("0")
    monkeypatch.setattr(module, "probe_mcp", lambda *args, **kwargs: {"projects": [{"id": "wrong", "name": "Other MCP"}], "latency_ms": 1})
    with pytest.raises(RuntimeError) as failure:
        instance.verify_public(config, timeout=1)
    message = str(failure.value)
    assert provider in message
    if mode == "cloudflare":
        assert "ngrok" not in message and "重新" in message
