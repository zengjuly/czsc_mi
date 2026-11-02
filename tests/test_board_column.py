"""test_board_column — 扫描结果/Excel 报告体现「所属自选板块」（W42.2）。

不依赖本机通达信：monkeypatch 板块快照。Excel 走 mystery.apps.reports.excel_report；
Web 扫描表走 mystery.apps.web.app._scan_table_data。
"""
from __future__ import annotations

import io

import openpyxl
import pytest

from mystery.apps.reports.excel_report import excel_bytes
from mystery.apps.web.app import _scan_table_data
from mystery.services import watchlist as wl


_FAKE_SNAP = {
    "synced_at": "2026-11-02", "source_dir": "/x",
    "boards": {
        "龙头股": {"file": "a.blk", "codes": ["sh600519", "sz000001"]},
        "自选股": {"file": "zxg.blk", "codes": ["sh600519"]},
    },
}


@pytest.fixture()
def patch_boards(monkeypatch):
    monkeypatch.setattr(wl, "load_tdx_boards", lambda: _FAKE_SNAP)


def _row(symbol, name, score):
    return {"symbol": symbol, "name": name, "score": score,
            "advice": "持有", "trade_date": "2026-08-30"}


def test_excel_board_column(patch_boards):
    rs = [_row("sh600519", "贵州茅台", 49.0),
          _row("sz000001", "平安银行", 70.0),
          _row("sh600150", "中国船舶", 20.0)]
    wb = openpyxl.load_workbook(io.BytesIO(excel_bytes(rs)))
    ws = wb["汇总报告"]
    headers = [c.value for c in ws[1]]
    assert "所属自选板块" in headers
    idx = headers.index("所属自选板块")
    by_sym = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        by_sym[row[0]] = row
    assert by_sym["sh600519"][idx] == "龙头股·自选股"
    assert by_sym["sz000001"][idx] == "龙头股"
    # 不属于任何板块 → 空白单元格（openpyxl 读回为空串/None，产品即「留空」）
    assert by_sym["sh600150"][idx] in ("", None)


def test_excel_board_column_no_snapshot(monkeypatch):
    """未同步 tdx 板块时，列仍存在于且为空白（不应出现 NaN/报错）。"""
    monkeypatch.setattr(wl, "load_tdx_boards", lambda: None)
    rs = [_row("sh600519", "贵州茅台", 49.0)]
    wb = openpyxl.load_workbook(io.BytesIO(excel_bytes(rs)))
    ws = wb["汇总报告"]
    headers = [c.value for c in ws[1]]
    idx = headers.index("所属自选板块")
    for row in ws.iter_rows(min_row=2, values_only=True):
        assert row[idx] in ("", None)


def test_scan_table_board_column(patch_boards):
    rows = [_row("sh600519", "贵州茅台", 49.0),
            _row("sh600150", "中国船舶", 20.0)]
    table = _scan_table_data(rows, nav_key="k")
    assert table[0]["所属板块"] == "龙头股·自选股"
    assert table[1]["所属板块"] == ""
