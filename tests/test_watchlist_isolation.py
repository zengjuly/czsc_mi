"""W65 防回归：测试不得写生产 data/watchlist.json / data/tdx_boards.json。

事故：`test_tdx_boards.py::patch_tdx` 只隔离了 `_DEFAULT_BOARDS_PATH`，漏了
`_DEFAULT_WATCHLIST`。每次 `pytest tests/` 都会 `save_watchlist([])` 清空生产
自选股，再写入 mock 的 3 只（600519/000001/600036），导致日报扫描范围从
zxg.blk 的 114 只缩到 3 只。

本测试静态检查：凡调用 watchlist 写 API 的测试文件，必须显式 monkeypatch
`_DEFAULT_WATCHLIST`。零依赖，CI 必跑。
"""
from __future__ import annotations

from pathlib import Path

# 会落盘写 watchlist 的 API（含内部写入函数）
WRITE_APIS = (
    "save_watchlist",
    "add_to_watchlist",
    "remove_from_watchlist",
    "_write_items",
    "sync_tdx_boards",
    "import_from_tdx",
)

TESTS_DIR = Path(__file__).resolve().parent


def test_tests_calling_watchlist_writes_isolate_path():
    offenders = []
    for f in sorted(TESTS_DIR.glob("test_*.py")):
        if f.name == Path(__file__).name:
            continue  # 跳过自身（WRITE_APIS 字面量会命中）
        src = f.read_text(encoding="utf-8")
        if not any(api in src for api in WRITE_APIS):
            continue
        if "_DEFAULT_WATCHLIST" not in src:
            offenders.append(f.name)
    assert not offenders, (
        "以下测试调用 watchlist 写 API 但未 monkeypatch `_DEFAULT_WATCHLIST`，"
        "会污染生产 data/watchlist.json（W65 事故）：%s" % offenders)
