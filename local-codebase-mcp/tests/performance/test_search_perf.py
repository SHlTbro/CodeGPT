"""task08 GWT-6：1 万小文件标本 search_code 典型词基准。

断言 <6s（基准 3s × 2 CI 容差），实测耗时 print 到输出供本地记录。
"""
from __future__ import annotations

import asyncio
import time

from security import truncate_search_line  # noqa: F401  (确保包路径已注入)


def test_search_code_10k_files_under_6s(bulk_repo, capsys):
    import tools_services

    t0 = time.perf_counter()
    r = asyncio.run(tools_services.search_code(bulk_repo.cfg, "bulk", "perfmarker"))
    elapsed = time.perf_counter() - t0
    with capsys.disabled():
        print(f"\n[perf] 10000 文件 search_code('perfmarker') 实测耗时: {elapsed:.3f}s（断言上限 6.0s）")
    assert r["matches"], "应有命中"
    assert r["truncated"] is True and r["next_hint"] == "缩小 query 或限定 path"
    assert elapsed < 6.0, f"万文件搜索 {elapsed:.3f}s 超过 6s 容差上限"


def test_search_code_10k_files_rare_term(bulk_repo, capsys):
    """对照：稀有词（单文件命中）也应远低于容差。"""
    import tools_services

    t0 = time.perf_counter()
    r = asyncio.run(tools_services.search_code(bulk_repo.cfg, "bulk", "perfmarker typical line 4217"))
    elapsed = time.perf_counter() - t0
    with capsys.disabled():
        print(f"\n[perf] 10000 文件 search_code 稀有词实测耗时: {elapsed:.3f}s")
    assert r["matches"] and elapsed < 6.0


def test_list_files_10k_files_depth2_under_3s(bulk_repo, capsys):
    """task10 GWT9：万文件项目 list_files depth=2 单次 rg --files 组装，<3s。"""
    import tools_services

    t0 = time.perf_counter()
    r = tools_services.list_files(bulk_repo.cfg, "bulk", ".", 2)
    elapsed = time.perf_counter() - t0
    with capsys.disabled():
        print(f"\n[perf] 10000 文件 list_files depth=2 实测耗时: {elapsed:.3f}s（断言上限 3.0s）")
    assert r["tree"]["files"] and not r["tree"]["dirs"]
    assert r["truncated"] is True and r["next_hint"] == "缩小 path 或减小 depth"   # 1 万 > 2000 上限
    assert elapsed < 3.0, f"万文件 depth=2 列表 {elapsed:.3f}s 超过 3s 红线"
