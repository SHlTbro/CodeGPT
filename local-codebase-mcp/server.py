"""local-codebase-mcp 最终装配（task07）：FastMCP + Streamable HTTP，监听 127.0.0.1:8000/mcp。

启动自检（config fail-fast、rg/git 检查）→ 注册 6 个只读工具 → 运行（task11 起支持
--config 热重载 / --check 校验 / 端口占用友好诊断）。
Python logging 落盘 server.log：每次工具调用记 metadata（时间戳/tool/project_id/
duration_ms/结果码），禁止记录文件内容或查询内容（corrections C5.9 精神）。
仅 Streamable HTTP 一种传输（D2 定案）；host 显式取自 config.yaml，禁止 0.0.0.0。
"""
from __future__ import annotations

import argparse
import logging
import shutil
import socket
import sys
import threading
import time
from pathlib import Path

from fastmcp import FastMCP

import tools
from config import load_config, validate_config

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "config.yaml"
LOG_PATH = BASE_DIR / "server.log"

_RELOAD_INTERVAL = 1.0  # 轮询周期 1s（GWT-1：≤5s 生效）

log = logging.getLogger("local-codebase-mcp.reload")


def setup_logging() -> logging.Logger:
    """工具调用 metadata 日志 → server.log（覆盖追加同文件，多进程共用一个 server 实例）。"""
    logger = logging.getLogger("local-codebase-mcp")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    logger.propagate = False
    if not log.handlers:
        rhandler = logging.FileHandler(LOG_PATH, encoding="utf-8")
        rhandler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(rhandler)
    log.propagate = False
    return logger


def self_check() -> None:
    """rg/git 自检：缺失给出明确修复指引（对应工具调用时才报 SEARCH_BACKEND_ERROR/GIT_ERROR）。"""
    if shutil.which("rg") is None:
        print("[warn] ripgrep 未在 PATH：search_code 将返回 SEARCH_BACKEND_ERROR。请安装 ripgrep 后重启 server。", file=sys.stderr)
    if shutil.which("git") is None:
        print("[warn] git 未在 PATH：git_info 将返回 GIT_ERROR。请安装 git 后重启 server。", file=sys.stderr)


def build_server(config) -> FastMCP:
    """建 FastMCP 实例并注册 6 个只读工具（config 已由 load_config_or_exit 校验）。"""
    mcp = FastMCP(
        name="local-codebase-mcp",
        instructions="本地代码库只读分析 MCP：先 list_projects 发现项目，再按 project_id 调用其余工具。",
    )
    tools.register_tools(mcp, config)
    return mcp


def _stamp(cfg_path: Path) -> tuple:
    """文件指纹（mtime+size），watch --config 实际传入文件，不猜另一份。"""
    st = cfg_path.stat()
    return st.st_mtime, st.st_size


def reload_config(cell: list, cfg_path: Path) -> bool:
    """重载一次：成功原子替换 cell[0]（整体引用替换，工具调用只见旧或新完整视图）；失败保持 last-good 并 WARN。"""
    try:
        new = load_config(cfg_path)
    except Exception as e:
        log.warning("配置重载失败，保持 last-good：原因=%s", e)
        return False
    old = cell[0]
    if new.server != old.server:
        # server 段（host/port/path）不热切 bind，仅提醒重启
        log.warning("监听参数变更需重启生效（当前保持 %s:%s%s）", old.server.host, old.server.port, old.server.path)
        return False
    old_ids = {p.id for p in old.registry}
    new_ids = {p.id for p in new.registry}
    added, removed = sorted(new_ids - old_ids), sorted(old_ids - new_ids)
    cell[0] = new  # 原子替换 registry 引用，防部分态
    tools._holder["config"] = new
    log.info("配置重载成功: 新增项目=%s 移除项目=%s", added or "无", removed or "无")
    return True


def start_reload_watcher(cell: list, cfg_path: Path) -> threading.Thread:
    """后台轮询线程：mtime+size 变化 → reload_config（标准库，无第三方依赖）。"""

    def _loop() -> None:
        last = _stamp(cfg_path)
        while True:
            time.sleep(_RELOAD_INTERVAL)
            try:
                if _stamp(cfg_path) != last:
                    last = _stamp(cfg_path)
                    reload_config(cell, cfg_path)
            except FileNotFoundError:
                continue  # 保存间隙文件暂缺，下轮再看
            except Exception:
                log.warning("重载失败，保持 last-good", exc_info=True)

    t = threading.Thread(target=_loop, name="config-reload", daemon=True)
    t.start()
    return t


def check_port_available(host: str, port: int) -> str | None:
    """端口占用探测（连接测试）；占用返回判定说明，空闲返回 None。"""
    try:
        with socket.create_connection((host, port), timeout=1):
            return f"端口 {port} 已被占用（{host} 上有进程正在监听）"
    except OSError:
        return None


def run_check(cfg_path: Path) -> int:
    """--check：不启动服务；校验 config + 探测端口占用，汇总报告，全过 0 任一失败 1。"""
    problems: list[str] = []
    data = None
    try:
        import yaml
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        problems.append(f"配置文件缺失: {cfg_path}")
    except Exception as e:
        problems.append(f"{cfg_path}: YAML 解析失败: {e}")
    if data is not None:
        problems += validate_config(data, cfg_path)
        try:
            from config import _parse_server
            srv = _parse_server(data, cfg_path)
            taken = check_port_available(srv.host, srv.port)
            if taken:
                problems.append(taken + "；建议：修改 config 中 server.port，或关闭占用进程")
        except Exception:
            pass  # 校验问题已列入
    if problems:
        print("配置检查未通过：")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("配置检查全部通过 ✓")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="local-codebase-mcp server")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="config.yaml 路径（默认本目录）")
    parser.add_argument("--check", action="store_true", help="仅校验配置与端口占用，不启动服务")
    args = parser.parse_args()

    if args.check:
        sys.exit(run_check(args.config))

    from config import load_config_or_exit
    config = load_config_or_exit(args.config)  # CONFIG_ERROR fail-fast：缺失/语法错/路径不存在/id 重复
    # 启动前端口预探测：占用时友好错误，禁止裸 uvicorn traceback（GWT-7）
    taken = check_port_available(config.server.host, config.server.port)
    if taken:
        print(f"[error] {taken}\n建议：修改 config 中 server.port，或关闭占用该端口的进程后重试。", file=sys.stderr)
        sys.exit(1)
    self_check()
    setup_logging()

    mcp = build_server(config)
    addr = f"http://{config.server.host}:{config.server.port}{config.server.path}"
    print(f"local-codebase-mcp 启动：{len(config.projects)} 个项目已登记，监听 {addr}", flush=True)
    cell = [config]  # 热重载可变引用单元：整体替换 cell[0]（原子防部分态）
    start_reload_watcher(cell, args.config)
    try:
        mcp.run(
            transport="streamable-http",
            host=config.server.host,   # 显式绑定（config 校验仅允许 loopback），禁止 0.0.0.0 默认
            port=config.server.port,
            path=config.server.path,
            show_banner=False,
        )
    except KeyboardInterrupt:
        print("\n停止", flush=True)


if __name__ == "__main__":
    main()
