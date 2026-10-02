"""task01 config 单元测试：加载 / 缺失 / 语法错 / id 重复 / 路径不存在 / server 段校验。"""
from __future__ import annotations

import pytest

import config as cfg_mod
from config import ConfigError, load_config, load_config_or_exit


def write_cfg(tmp_path, content: str):
    p = tmp_path / "config.yaml"
    p.write_text(content, encoding="utf-8")
    return p


def valid_cfg(tmp_path) -> str:
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    return (
        "projects:\n"
        "  - id: alpha\n    name: Alpha\n    path: " + proj.as_posix() + "\n"
        "  - id: beta\n    name: Beta\n    path: " + (tmp_path / "proj").as_posix() + "\n"
    )


def test_valid_load_preserves_declaration_order(tmp_path):
    c = load_config(write_cfg(tmp_path, valid_cfg(tmp_path)))
    assert [p.id for p in c.registry] == ["alpha", "beta"]       # 声明序
    assert c.registry["alpha"].name == "Alpha"
    assert c.server.host == "127.0.0.1" and c.server.port == 8000 and c.server.path == "/mcp"


def test_defaults_match_contract_limits_and_security(contract):
    from config import _DEFAULT_LIMITS, _DEFAULT_SECURITY
    limits = contract["configSchema"]["shape"]["limits"]
    security = contract["configSchema"]["shape"]["security"]
    assert _DEFAULT_LIMITS == limits
    assert _DEFAULT_SECURITY == security


def test_missing_config_fails_fast_with_example(tmp_path):
    with pytest.raises(ConfigError) as ei:
        load_config(tmp_path / "missing.yaml")
    msg = str(ei.value)
    assert "CONFIG_ERROR" in msg and "缺失" in msg and "projects:" in msg   # 含示例指引


def test_yaml_syntax_error_reports_line(tmp_path):
    with pytest.raises(ConfigError) as ei:
        load_config(write_cfg(tmp_path, "projects:\n  - id: x\n   bad: [\n"))
    assert "行" in str(ei.value) and "CONFIG_ERROR" in str(ei.value)


def test_duplicate_project_id_rejected(tmp_path):
    content = (
        "projects:\n"
        f"  - id: dup\n    name: A\n    path: {tmp_path.as_posix()}\n"
        f"  - id: dup\n    name: B\n    path: {tmp_path.as_posix()}\n"
    )
    with pytest.raises(ConfigError) as ei:
        load_config(write_cfg(tmp_path, content))
    assert "dup" in str(ei.value) and "重复" in str(ei.value)


def test_nonexistent_path_rejected(tmp_path):
    content = "projects:\n  - id: ghost\n    name: G\n    path: D:/no/such/dir-xyz-000\n"
    with pytest.raises(ConfigError) as ei:
        load_config(write_cfg(tmp_path, content))
    assert "ghost" in str(ei.value) and "D:/no/such/dir-xyz-000" in str(ei.value)


def test_relative_path_rejected(tmp_path):
    with pytest.raises(ConfigError) as ei:
        load_config(write_cfg(tmp_path, "projects:\n  - id: rel\n    name: R\n    path: ./rel\n"))
    assert "绝对路径" in str(ei.value)


def test_non_loopback_host_rejected(tmp_path):
    content = valid_cfg(tmp_path) + "server:\n  host: 0.0.0.0\n  port: 8000\n"
    with pytest.raises(ConfigError) as ei:
        load_config(write_cfg(tmp_path, content))
    assert "loopback" in str(ei.value)


def test_port_out_of_range_rejected(tmp_path):
    content = valid_cfg(tmp_path) + "server:\n  port: 70000\n"
    with pytest.raises(ConfigError) as ei:
        load_config(write_cfg(tmp_path, content))
    assert "port" in str(ei.value)


def test_top_level_not_mapping_rejected(tmp_path):
    with pytest.raises(ConfigError) as ei:
        load_config(write_cfg(tmp_path, "- just\n- a\n- list\n"))
    assert "映射" in str(ei.value)


def test_unknown_limits_key_rejected(tmp_path):
    content = valid_cfg(tmp_path) + "limits:\n  bogus_key: 1\n"
    with pytest.raises(ConfigError) as ei:
        load_config(write_cfg(tmp_path, content))
    assert "bogus_key" in str(ei.value)


def test_load_config_or_exit_exits_nonzero(tmp_path, capsys):
    with pytest.raises(SystemExit) as ei:
        load_config_or_exit(tmp_path / "missing.yaml")
    assert ei.value.code != 0
    assert "CONFIG_ERROR" in capsys.readouterr().err
