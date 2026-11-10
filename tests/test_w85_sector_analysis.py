"""W85：行业板块关注跟踪 + 板块分析 + 报告/网页接入测试。"""
import os
import sys

import pytest

sys.path.insert(0, "/home/ai/ai_runner/stock/czsc_mi")

from mystery.services import sector_analysis as sa
from mystery.services import runtime_settings as rts
from mystery.apps.reports import excel_report as er
from mystery.apps.reports import html_report as hr


class _FakeDB:
    """内存伪 DB：提供 get_sector_kline / get_sector_meta / _connect。"""

    def __init__(self, sectors=None, rel=None, kline_rows=None, names=None):
        self._sectors = sectors or []
        self._rel = rel or []
        self._kline_rows = kline_rows or []
        self._names = names or []

    def get_sector_meta(self, active_only=True):
        return [(c, n, "行业/概念") for c, n in self._sectors]

    def get_sector_kline(self, sector_code):
        # 与真实 SectorRepoMixin 一致：返回中文列 DataFrame。
        import pandas as pd
        cols = ["sector_code", "sector_name", "日期", "开盘价", "最高价",
                "最低价", "收盘价", "成交量", "成交额"]
        df = pd.DataFrame(self._kline_rows, columns=cols)
        return df[["日期", "开盘价", "最高价", "最低价", "收盘价",
                   "成交量", "成交额"]]

    def _connect(self):
        import sqlite3
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE sector_kline (sector_code TEXT, sector_name TEXT, "
                    "trade_date TEXT, open REAL, high REAL, low REAL, close REAL, "
                    "volume REAL, amount REAL)")
        con.execute("CREATE TABLE stock_sector_rel (stock_code TEXT, sector_code TEXT, "
                    "is_primary INTEGER)")
        con.execute("CREATE TABLE stock_kline_data (code TEXT, date TEXT, period TEXT, "
                    "close REAL)")
        con.execute("CREATE TABLE stock_industry_info (code TEXT, code_name TEXT)")
        con.executemany("INSERT INTO sector_kline VALUES (?,?,?,?,?,?,?,?,?)",
                        self._kline_rows)
        con.executemany("INSERT INTO stock_sector_rel VALUES (?,?,1)", self._rel)
        con.executemany("INSERT INTO stock_industry_info VALUES (?,?)", self._names)
        return con


def test_normalize_sector_code():
    assert sa.normalize_sector_code("881101.TI") == "ths_881101"
    assert sa.normalize_sector_code("ths_881101") == "ths_881101"
    assert sa.normalize_sector_code("885863") == "ths_885863"


def test_sector_config_defaults_and_roundtrip(monkeypatch, tmp_path):
    """W85 配置：默认 auto + 稀疏覆盖层 + 一键还原（不影响其他覆盖层）。

    线上 ``data/sector_override.json``（网页面板保存的关注来源）会把默认值
    覆盖成 all/显式列表；这里用临时目录隔离，保证断言的是代码默认值，
    而不是部署环境的运行时配置。
    """
    monkeypatch.setenv("MYSTERY_RUNTIME_OVERRIDE_DIR", str(tmp_path))
    import importlib
    import mystery.services.runtime_settings as m
    importlib.reload(m)
    try:
        opts = m.sector_options_normalized()
        assert opts["enabled"] is True
        assert opts["focus"] == "auto"
        assert opts["top_n"] == 8
        assert opts["corr_window"] == 60
        assert opts["auto_max"] == 8
        # 归一：显式 code 列表 / all
        assert m.save_sector_override is not None
    finally:
        monkeypatch.delenv("MYSTERY_RUNTIME_OVERRIDE_DIR", raising=False)
        importlib.reload(m)  # 复位模块级路径，避免影响后续测试


def test_sector_options_normalized_explicit_codes(monkeypatch, tmp_path):
    """覆盖层写显式 code 列表 → normalized 返回 list；还原后回 auto。"""
    monkeypatch.setenv("MYSTERY_RUNTIME_OVERRIDE_DIR", str(tmp_path))
    import importlib
    import mystery.services.runtime_settings as m
    importlib.reload(m)
    m.save_sector_override({
        "MYSTERY_SECTOR_ENABLED": "1",
        "MYSTERY_SECTOR_FOCUS": ["ths_881101", "ths_881145"],
        "MYSTERY_SECTOR_TOP_N": 5,
    })
    o = m.sector_options_normalized()
    assert o["focus"] == ["ths_881101", "ths_881145"]
    assert o["top_n"] == 5
    assert m.sector_override_exists() is True
    m.clear_sector_override()
    assert m.sector_options_normalized()["focus"] == "auto"
    importlib.reload(m)  # 复位模块级路径，避免影响后续测试


def test_price_view_pure():
    """纯价格研判：单调上行 → 站上年线 + 多头排列。"""
    closes = [1000.0 + i for i in range(300)]
    v = sa._price_view(closes)
    assert v["above_year"] is True
    assert v["ma_aligned"] is True
    assert v["chg_20d"] is not None
    assert v["close"] == closes[-1]


def test_price_view_insufficient():
    v = sa._price_view([1.0, 2.0, 3.0])
    assert v["above_year"] is None
    assert v["ma_aligned"] is None


def test_analyze_sector_no_data():
    db = _FakeDB(kline_rows=[])
    d = sa.analyze_sector(db, "ths_881101", "测试板块")
    assert d["has_data"] is False
    assert d["accumulation"] is None
    assert d["related"] == []


def test_analyze_sector_with_synthetic_kline():
    """合成 300 根板块指数 → 有价格研判 / 强度分 / 形态 / 吸筹结论。"""
    import datetime as dt
    rows = []
    base = dt.date(2025, 1, 1)
    for i in range(300):
        c = 1000.0 + i * 0.5
        rows.append(("ths_881101", "测试板块", (base + dt.timedelta(days=i)).isoformat(),
                     c - 2, c + 2, c - 3, c, 1_000_000, 3_000_000_000.0))
    db = _FakeDB(sectors=[("ths_881101", "测试板块")], kline_rows=rows)
    d = sa.analyze_sector(db, "ths_881101", "", top_n=3)
    assert d["has_data"] is True
    assert d["name"] == "测试板块"      # 名称自动从 kline 补全
    assert d["close"] is not None
    assert d["strength_score"] is not None
    assert d["candle_daily"]
    assert d["accumulation"] and d["accumulation"]["has_data"] is True
    assert d["accumulation_level"] != "—"


def test_weekly_bars_aggregation():
    """日 K 重采样成周 K：根数减少且 OHLC 合理。"""
    import datetime as dt
    import pandas as pd
    rows = []
    base = dt.date(2025, 1, 1)
    for i in range(60):
        rows.append({"日期": (base + dt.timedelta(days=i)).isoformat(),
                      "开盘价": 100.0 + i, "最高价": 102.0 + i,
                      "最低价": 98.0 + i, "收盘价": 101.0 + i,
                      "成交量": 1000, "成交额": 100000})
    w = sa._weekly_bars(pd.DataFrame(rows))
    assert 0 < len(w) < 60
    assert all(b.high >= b.low for b in w)


def test_sector_summary_line():
    assert sa.sector_summary_line([]) == "[行业板块] 暂无数据"
    d = {"name": "电力", "has_data": True, "strength_score": 16.1,
         "strength_up": True, "chg_20d": -5.4, "accumulation_level": "疑似吸筹"}
    line = sa.sector_summary_line([d])
    assert "电力" in line and "疑似吸筹" in line and "-5.4%" in line


def test_report_signature_accepts_focus_sectors():
    """报告层签名兼容 W85 新参数（不需要真实写盘）。"""
    import inspect
    assert "focus_sectors" in inspect.signature(er.write_excel).parameters
    assert "focus_sectors" in inspect.signature(hr.write_html).parameters
    assert "major_indexes" in inspect.signature(er.write_excel).parameters


def test_excel_summary_has_accumulation_cols():
    keys = [k for k, _ in er.SUMMARY_COLS]
    assert "accumulation" in keys
    assert "accumulation_stage" in keys
    # W76 列序不被 W86 破坏：名称仍在前、代码在后
    assert keys[0] == "name" and keys[1] == "symbol"


def test_html_sector_block_contains_related():
    """HTML 板块区块包含板块名 + 强相关个股明细。"""
    sec = {
        "name": "电力", "code": "ths_881145", "has_data": True,
        "close": 1815.0, "chg_20d": -5.4, "above_year": False,
        "year_dist_pct": -9.0, "ma_aligned": False,
        "strength_score": 16.1, "strength_up": True,
        "accumulation": {"level": "medium", "evidence": ["低位：回撤23%"],
                         "conclusion": "疑似吸筹（观察区间）"},
        "accumulation_level": "疑似吸筹",
        "related": [{"name": "长青集团", "symbol": "002616.SZ", "corr": 0.481,
                     "analyzed": True, "score": 60.0, "advice": "观察",
                     "accumulation_level": "观察"}],
    }
    html = hr._sector_block([sec])
    assert "行业板块研判" in html
    assert "电力" in html and "长青集团" in html
    assert "疑似吸筹" in html and "0.481" in html
    assert "主力吸筹(W86)" in html


def test_html_index_block_has_accumulation_col():
    idx = [{"name": "上证指数", "code": "000001.SH", "close": 3000.0,
            "chg_20d": -3.6, "above_year": False, "year_dist_pct": -2.0,
            "ma_aligned": False, "candle_daily": "—", "candle_weekly": "—",
            "accumulation": {"level": "medium"}, "accumulation_level": "疑似吸筹"}]
    html = hr._index_block(idx)
    assert "主力吸筹(W86)" in html
    assert "疑似吸筹" in html
