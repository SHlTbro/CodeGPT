"""task11 配置体验测试：热重载 / last-good / 删项目同步 / bind 不热切 / --check / 端口占用诊断。"""
from __future__ import annotations

import logging
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import PKG_DIR

import server as server_mod
import tools as tools_mod
from config import load_config
from tools_services import list_projects


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def write_cfg(path: Path, projects, server: str = "") -> Path:
    body = "projects:\n"
    for pid, p in projects:
        body += f"  - id: {pid}\n    name: {pid}\n    path: {p.as_posix()}\n"
    path.write_text(body + server, encoding="utf-8")
    return path


def make_dirs(tmp_path, *names):
    dirs = []
    for n in names:
        d = tmp_path / n
        d.mkdir(exist_ok=True)
        dirs.append(d)
    return dirs


def run_server_cli(*extra: str, cfg: Path):
    return subprocess.run(
        [sys.executable, str(PKG_DIR / "server.py"), *extra, "--config", str(cfg)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(PKG_DIR), timeout=60,
    )


# ---------------------------------------------------------------- 热重载
def test_reload_picks_up_new_project(tmp_path):
    a, b = make_dirs(tmp_path, "a", "b")
    cfg_path = write_cfg(tmp_path / "config.yaml", [("alpha", a)])
    cell = [load_config(cfg_path)]
    tools_mod._holder["config"] = cell[0]
    write_cfg(cfg_path, [("alpha", a), ("beta", b)])
    assert server_mod.reload_config(cell, cfg_path) is True
    assert [p["id"] for p in list_projects(cell[0])["projects"]] == ["alpha", "beta"]
    assert tools_mod._holder["config"] is cell[0]          # registry 引用已整体替换


def test_reload_removed_project_synced(tmp_path):
    a, b = make_dirs(tmp_path, "a", "b")
    cfg_path = write_cfg(tmp_path / "config.yaml", [("alpha", a), ("beta", b)])
    cell = [load_config(cfg_path)]
    write_cfg(cfg_path, [("alpha", a)])                    # 删项目：registry 同步减少
    assert server_mod.reload_config(cell, cfg_path) is True
    assert [p["id"] for p in list_projects(cell[0])["projects"]] == ["alpha"]


def test_bad_config_keeps_last_good(tmp_path, caplog):
    a = make_dirs(tmp_path, "a")[0]
    cfg_path = write_cfg(tmp_path / "config.yaml", [("alpha", a)])
    cell = [load_config(cfg_path)]
    tools_mod._holder["config"] = cell[0]
    old = cell[0]
    write_cfg(cfg_path, [("alpha", a), ("beta", tmp_path / "no-such-dir-xyz")])   # path 不存在
    with caplog.at_level(logging.WARNING, logger="local-codebase-mcp.reload"):
        assert server_mod.reload_config(cell, cfg_path) is False
    assert cell[0] is old                                  # last-good 保持服务
    assert any("重载失败" in r.getMessage() for r in caplog.records)
    assert tools_mod._holder["config"] is old


def test_bad_yaml_keeps_last_good(tmp_path, caplog):
    a = make_dirs(tmp_path, "a")[0]
    cfg_path = write_cfg(tmp_path / "config.yaml", [("alpha", a)])
    cell = [load_config(cfg_path)]
    old = cell[0]
    cfg_path.write_text("projects: [bad yaml\n", encoding="utf-8")   # 语法错
    with caplog.at_level(logging.WARNING, logger="local-codebase-mcp.reload"):
        assert server_mod.reload_config(cell, cfg_path) is False
    assert cell[0] is old
    assert any("重载失败" in r.getMessage() for r in caplog.records)


def test_server_section_change_warns_and_not_hotswap(tmp_path, caplog):
    a = make_dirs(tmp_path, "a")[0]
    port = _free_port()
    cfg_path = write_cfg(tmp_path / "config.yaml", [("alpha", a)],
                         server=f"server:\n  host: 127.0.0.1\n  port: {port}\n  path: /mcp\n")
    cell = [load_config(cfg_path)]
    old = cell[0]
    write_cfg(cfg_path, [("alpha", a)],
              server=f"server:\n  host: 127.0.0.1\n  port: {port + 1}\n  path: /mcp\n")
    with caplog.at_level(logging.WARNING, logger="local-codebase-mcp.reload"):
        assert server_mod.reload_config(cell, cfg_path) is False
    assert cell[0].server.port == port                     # 不热切 bind，保持原监听参数
    assert cell[0] is old
    assert any("需重启生效" in r.getMessage() for r in caplog.records)


def test_watcher_thread_applies_within_10s(tmp_path):
    a, b = make_dirs(tmp_path, "a", "b")
    cfg_path = write_cfg(tmp_path / "config.yaml", [("alpha", a)])
    cell = [load_config(cfg_path)]
    server_mod.start_reload_watcher(cell, cfg_path)
    write_cfg(cfg_path, [("alpha", a), ("beta", b)])
    # C-008 处置（task16 授权）：watcher 轮询间隔 1s，满载下 5s 断言临界 flaky——
    # 断言上限 5s→10s（侵入最小方案：不改事件同步、不动生产代码），留 10 倍余量。
    deadline = time.time() + 10
    while time.time() < deadline:
        ids = [p["id"] for p in list_projects(cell[0])["projects"]]
        if ids == ["alpha", "beta"]:
            return
        time.sleep(0.2)
    pytest.fail("轮询 10s 内未生效新 registry")


# ---------------------------------------------------------------- --check 与端口诊断
def test_validate_config_collects_problems(tmp_path):
    problems = server_mod.run_check.__globals__["validate_config"](
        {"projects": [{"id": "", "name": "n", "path": "D:/no-such-xyz-000"},
                      {"id": "dup", "name": "n", "path": str(tmp_path)},
                      {"id": "dup", "name": "n", "path": str(tmp_path)}],
         "server": {"host": "0.0.0.0", "port": 70000, "path": "mcp"}},
        tmp_path / "config.yaml",
    )
    text = "\n".join(problems)
    assert "id" in text and "重复" in text and "0.0.0.0" in text and "70000" in text and "/ 开头" in text


def test_check_ok_exit_zero(tmp_path):
    a = make_dirs(tmp_path, "a")[0]
    cfg = write_cfg(tmp_path / "config.yaml", [("alpha", a)],
                    server=f"server:\n  host: 127.0.0.1\n  port: {_free_port()}\n")
    r = run_server_cli("--check", cfg=cfg)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "全部通过" in r.stdout


def test_check_bad_config_exit_one(tmp_path):
    cfg = write_cfg(tmp_path / "config.yaml", [("ghost", tmp_path / "no-such-dir-xyz")])
    r = run_server_cli("--check", cfg=cfg)
    assert r.returncode == 1
    assert "ghost" in r.stdout and "no-such-dir-xyz" in r.stdout   # 报告指明具体项


def test_check_port_occupied_exit_one(tmp_path):
    a = make_dirs(tmp_path, "a")[0]
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    try:
        cfg = write_cfg(tmp_path / "config.yaml", [("alpha", a)],
                        server=f"server:\n  host: 127.0.0.1\n  port: {port}\n")
        r = run_server_cli("--check", cfg=cfg)
        assert r.returncode == 1
        assert str(port) in r.stdout and "已被占用" in r.stdout
    finally:
        blocker.close()


def test_normal_start_port_occupied_friendly_error(tmp_path):
    a = make_dirs(tmp_path, "a")[0]
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    try:
        cfg = write_cfg(tmp_path / "config.yaml", [("alpha", a)],
                        server=f"server:\n  host: 127.0.0.1\n  port: {port}\n")
        r = run_server_cli(cfg=cfg)                        # 正常启动（非 --check）
        out = r.stdout + r.stderr
        assert r.returncode != 0
        assert str(port) in out and "占用" in out and "建议" in out
        assert "Traceback" not in out                      # 禁止裸 traceback
    finally:
        blocker.close()
