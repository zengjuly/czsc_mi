"""W84：各大指数分析模块 + 报告接入测试。"""
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, "/home/ai/ai_runner/stock/czsc_mi")

from mystery.core.models import Bar, BarSeries
from mystery.services import index_analysis as ia


def _mk_series(n: int, start: float, step: float, prefix: str = "I") -> BarSeries:
    bars = []
    base = datetime(2025, 1, 1)
    for i in range(n):
        c = start + step * i
        bars.append(Bar(
            dt=(base + timedelta(days=i)).isoformat(),
            open=c - 1, high=c + 2, low=c - 2, close=c,
            volume=1_000_000, turnover=1.5,
        ))
    return BarSeries(symbol=f"{prefix}.X", freq="1d", bars=bars, source="db")


class _FakeMarket:
    """按 code+freq 返回预设序列，模拟 MarketDataClient。"""

    def __init__(self, daily: BarSeries):
        self._daily = daily

    def fetch_index(self, code, freq="1d"):
        if freq == "1w":
            return self._daily  # 测试无需真实重采样
        return self._daily


def test_major_indexes_definition():
    assert len(ia.MAJOR_INDEXES) == 8
    for name, code in ia.MAJOR_INDEXES:
        assert name and code
        assert code.endswith((".SH", ".SZ"))


def test_analyze_index_rising():
    # 300 根单调上行：站上年线 + MA 多头排列
    daily = _mk_series(300, 1000.0, 1.0, "UP")
    m = _FakeMarket(daily)
    d = ia.analyze_index(m, "测试指数", "000001.SH")
    assert d["has_data"] is True
    assert d["above_year"] is True
    assert d["ma_aligned"] is True
    assert d["close"] == 1000.0 + 299.0
    assert d["candle_daily"]  # 有日K形态串
    assert d["candle_weekly"]  # 有周K形态串


def test_analyze_index_falling():
    # 300 根单调下行：跌破年线
    daily = _mk_series(300, 2000.0, -1.0, "DN")
    m = _FakeMarket(daily)
    d = ia.analyze_index(m, "测试指数", "000001.SH")
    assert d["has_data"] is True
    assert d["above_year"] is False


def test_analyze_index_no_data():
    m = _FakeMarket(BarSeries(symbol="X.X", freq="1d", bars=[]))
    d = ia.analyze_index(m, "空指数", "000001.SH")
    assert d["has_data"] is False
    assert d["above_year"] is None
    assert d["ma_aligned"] is None
    assert d["close"] is None


def test_analyze_major_indexes_returns_list():
    daily = _mk_series(300, 1000.0, 1.0, "UP")
    m = _FakeMarket(daily)
    idxs = ia.analyze_major_indexes(m)
    assert isinstance(idxs, list)
    assert len(idxs) == len(ia.MAJOR_INDEXES)
    # 全部以伪造数据填充（has_data=True），缺数据也不抛
    assert all(isinstance(x, dict) for x in idxs)


def test_index_summary_line_format():
    daily = _mk_series(300, 1000.0, 1.0, "UP")
    m = _FakeMarket(daily)
    line = ia.index_summary_line(ia.analyze_major_indexes(m))
    assert line.startswith("[各大指数]")
    assert "站上年线" in line


def test_report_writers_include_index_section(tmp_path):
    from mystery.apps.reports.excel_report import write_excel
    from mystery.apps.reports.html_report import write_html

    daily = _mk_series(300, 1000.0, 1.0, "UP")
    m = _FakeMarket(daily)
    idxs = ia.analyze_major_indexes(m)
    results = [{"name": "测试股", "symbol": "600000.SH", "score": 70.0,
                "trade_date": "2026-11-09"}]

    xlsx = str(tmp_path / "r.xlsx")
    html = str(tmp_path / "r.html")
    write_excel(results, xlsx, major_indexes=idxs)
    write_html(results, html, major_indexes=idxs)

    assert os.path.exists(xlsx) and os.path.exists(html)
    # Excel 新增「各大指数」工作表
    from openpyxl import load_workbook
    wb = load_workbook(xlsx)
    assert "各大指数" in wb.sheetnames
    # HTML 含指数研判区块
    html_txt = open(html, encoding="utf-8").read()
    assert "各大指数研判" in html_txt


def test_report_writers_without_indexes_no_section(tmp_path):
    from mystery.apps.reports.excel_report import write_excel
    from mystery.apps.reports.html_report import write_html

    results = [{"name": "测试股", "symbol": "600000.SH", "score": 70.0}]
    xlsx = str(tmp_path / "r2.xlsx")
    html = str(tmp_path / "r2.html")
    write_excel(results, xlsx)          # 不传 major_indexes
    write_html(results, html)
    from openpyxl import load_workbook
    wb = load_workbook(xlsx)
    assert "各大指数" not in wb.sheetnames
