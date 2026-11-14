"""W92：网页 / Excel 报告补齐「大盘指数」「行业板块」分析（展示层专项）。

背景（用户反馈）：
- 网页分析结果里大盘指数「分析内容都是空的」、没有板块的分析；
- Excel 报告里没有大盘指数和板块的分析。

根因不在扫描数据（生产库 job 111 的 8 行指数 ``index_analysis`` 完整），
而在展示层：指数被当成个股塞进 130+ 列大表、板块只写 JSONL 分片不进结果
表、Web 下载只生成单个「汇总报告」sheet、CLI 日报自选扫描无指数行。

本测试覆盖：
- ``index_payloads`` 把 ``_index_result`` 包装行解包成裸 dict；
- ``excel_bytes(..., major_indexes=, focus_sectors=)`` 追加两个工作表，
  内容与 CLI ``write_excel`` 同款；无参数时零回归（仅汇总报告）；
- Web 结果页：指数 / 个股行分离、指数与板块完整展示行；
- CLI 日报：results 无指数行时回退现算，传给报告的是非空裸 dict。
"""
from __future__ import annotations

import io
import os
import sys

import pytest

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ---------------------------------------------------------------------------
# 测试数据（纯 dict，不连生产库）
# ---------------------------------------------------------------------------
def _index_dict():
    return {
        "name": "上证指数", "code": "000001.SH", "date": "2026-11-13",
        "close": 3634.35, "chg_5d": -0.42, "chg_20d": -3.61,
        "above_year": False, "year_dist_pct": -7.9, "ma_aligned": False,
        "candle_daily": "多方炮(11-11·多)、看涨吞没(11-04·多)",
        "candle_weekly": "锤头线(10-20·多)",
        "accumulation": {"stage": "吸筹", "conclusion": "观察区间，非买点"},
        "accumulation_level": "疑似吸筹", "has_data": True,
    }


def _index_result_row(d):
    """模拟 W84 ``_index_result`` 落库的包装形态。"""
    return {"is_index": True, "symbol": d["code"], "name": d["name"],
            "score": None, "trade_date": d["date"],
            "index_analysis": d}


def _sector_dict():
    return {
        "name": "电力", "code": "ths_881101", "date": "2026-11-13",
        "close": 3120.5, "chg_5d": 1.2, "chg_20d": -5.5,
        "above_year": False, "year_dist_pct": -3.1, "ma_aligned": True,
        "strength_score": 16.2, "strength_up": True,
        "candle_daily": "多方炮", "candle_weekly": "看涨吞没",
        "accumulation": {"stage": "吸筹", "conclusion": "疑似吸筹，观察区间"},
        "accumulation_level": "疑似吸筹", "has_data": True,
        "related": [
            {"name": "华能国际", "symbol": "600011.SH", "corr": 0.81,
             "analyzed": True, "score": 55.0, "advice": "关注",
             "accumulation_level": "疑似吸筹"},
            {"name": "国电电力", "symbol": "600795.SH", "corr": 0.76,
             "analyzed": False, "score": None, "advice": "—",
             "accumulation_level": "—"},
        ],
    }


def _stock_row(symbol="600150.SH", name="中国船舶", score=12.0):
    return {"symbol": symbol, "name": name, "score": score,
            "advice": "观望", "price": 30.1, "trade_date": "2026-11-13",
            "mystery": {}}


# ---------------------------------------------------------------------------
# Excel 报告层
# ---------------------------------------------------------------------------
def _excel_mod():
    import mystery.apps.reports.excel_report as m
    return m


def test_index_payloads_unwraps_scan_result_rows():
    """W92：包装行解包成裸 dict；裸 dict 原样；无效行忽略。"""
    m = _excel_mod()
    d = _index_dict()
    out = m.index_payloads([_index_result_row(d)])
    assert len(out) == 1
    assert out[0]["close"] == 3634.35
    assert "多方炮" in out[0]["candle_daily"]
    assert out[0]["name"] == "上证指数" and out[0]["code"] == "000001.SH"
    # 裸 dict（analyze_major_indexes 直接产出）原样保留
    assert m.index_payloads([d])[0]["close"] == 3634.35
    # 非 dict / 无研判字段 → 忽略
    assert m.index_payloads([None, {}, {"name": "x"}]) == []
    assert m.index_payloads([]) == []


def test_excel_bytes_default_single_sheet_unchanged():
    """W92：不传指数/板块时仍是单个「汇总报告」（W90 契约零回归）。"""
    import openpyxl
    m = _excel_mod()
    data = m.excel_bytes([_stock_row(), _stock_row("600519.SH", "贵州茅台", 60.0)])
    wb = openpyxl.load_workbook(io.BytesIO(data))
    assert wb.sheetnames == ["汇总报告"]
    assert wb["汇总报告"].freeze_panes == "B2"


def test_excel_bytes_appends_index_and_sector_sheets():
    """W92：Web 下载 Excel 含 3 个 sheet，指数/板块内容非空、非占位。"""
    import openpyxl
    m = _excel_mod()
    data = m.excel_bytes(
        [_stock_row(), _stock_row("600519.SH", "贵州茅台", 60.0)],
        major_indexes=m.index_payloads([_index_result_row(_index_dict())]),
        focus_sectors=[_sector_dict()])
    wb = openpyxl.load_workbook(io.BytesIO(data))
    assert wb.sheetnames == ["汇总报告", "各大指数", "行业板块"]

    wi = wb["各大指数"]
    assert wi["A2"].value == "上证指数"
    assert wi["C2"].value == 3634.35           # 最新收盘有值，不是空
    assert "跌破年线" in str(wi["F2"].value)   # 年线位置
    assert "多方炮" in str(wi["H2"].value)     # 日K形态
    assert wi["J2"].value == "疑似吸筹"        # 主力吸筹
    assert wi.freeze_panes == "B2"             # 名称列冻结

    ws = wb["行业板块"]
    assert ws["A2"].value == "电力"
    assert ws["I2"].value == 16.2              # 板块强度分
    assert "多方炮" in str(ws["K2"].value)
    assert ws["N2"].value == 2                 # 强相关个股数
    # 强相关个股明细区（表头 + 2 行）
    flat = [str(c.value) for row in ws.iter_rows() for c in row
            if c.value is not None]
    assert "强相关个股" in flat
    assert "华能国际" in flat and "国电电力" in flat

    # 汇总报告里只有 2 只个股，指数行没有混进去（W92 分离）
    wsum = wb["汇总报告"]
    assert wsum.max_row == 3


def test_write_excel_and_excel_bytes_same_sheet_content(tmp_path):
    """W92：CLI 日报与 Web 下载的两个附加表内容完全一致（共用填充函数）。"""
    import openpyxl
    m = _excel_mod()
    results = [_stock_row()]
    idx = m.index_payloads([_index_result_row(_index_dict())])
    sectors = [_sector_dict()]
    path = str(tmp_path / "w92_daily.xlsx")
    m.write_excel(results, path, major_indexes=idx, focus_sectors=sectors)
    wb_file = openpyxl.load_workbook(path)
    wb_mem = openpyxl.load_workbook(io.BytesIO(
        m.excel_bytes(results, major_indexes=idx, focus_sectors=sectors)))
    for name in ("各大指数", "行业板块"):
        a = [[c.value for c in r] for r in wb_file[name].iter_rows()]
        b = [[c.value for c in r] for r in wb_mem[name].iter_rows()]
        assert a == b, f"{name} 表内容不一致"


# ---------------------------------------------------------------------------
# Web 展示层（需要 streamlit；CI 无 streamlit 时跳过）
# ---------------------------------------------------------------------------
def test_split_scan_rows_and_views():
    pytest.importorskip("streamlit")
    import mystery.apps.web.views.scan as sc

    rows = [_stock_row(), _index_result_row(_index_dict()),
            _stock_row("600519.SH", "贵州茅台", 60.0)]
    stock, index = sc.split_scan_rows(rows)
    assert len(stock) == 2 and len(index) == 1
    assert all(not r.get("is_index") for r in stock)
    assert index[0]["name"] == "上证指数"
    # 输入不被修改
    assert len(rows) == 3

    iv = sc.market_index_view(index)
    assert len(iv) == 1
    v = iv[0]
    assert v["指数"] == "上证指数"
    assert v["最新收盘"] == "3634.35"
    assert "跌破年线" in v["年线位置"]
    assert "多方炮" in v["日K形态"]
    assert v["主力吸筹"] == "疑似吸筹"
    assert "观察区间" in v["吸筹结论"]
    # 12 列全部有值（不再是一堆空列）
    assert all(str(x) not in ("", "None") for x in v.values())

    sv = sc.market_sector_view([_sector_dict()])
    assert len(sv) == 1
    assert sv[0]["板块"] == "电力"
    assert sv[0]["板块强度"] == "16.2（偏强）"
    assert sv[0]["强相关个股数"] == 2
    assert all(str(x) not in ("", "None") for x in sv[0].values())


def test_sector_cache_key_hashable_and_list_encoded():
    pytest.importorskip("streamlit")
    import mystery.apps.web.views.scan as sc
    k = sc._sector_cache_key({"enabled": True, "focus": ["ths_881101", "ths_881102"],
                              "top_n": 8, "corr_window": 60, "auto_max": 8})
    assert k[0] == "list:ths_881101,ths_881102"
    assert k[1] is True and k[2] == 8
    hash(k)  # 可哈希，供 st.cache_data 使用
    assert sc._sector_cache_key({"enabled": False, "focus": "auto"})[1] is False


def test_w90_column_config_contract_unchanged():
    """W92 不破坏 W90：名称仍为 LinkColumn(pinned) + display_text 正则。"""
    pytest.importorskip("streamlit")
    import mystery.apps.web.views.scan as sc
    cfg = sc._scan_table_column_config()
    assert "详情" not in cfg
    assert dict(cfg["名称"]).get("pinned") is True
    assert (dict(cfg["名称"]).get("type_config") or {}).get("type") == "link"


# ---------------------------------------------------------------------------
# CLI 日报：自选扫描无指数行时回退现算
# ---------------------------------------------------------------------------
def test_scan_report_falls_back_to_live_major_indexes(monkeypatch, tmp_path):
    """W92：results 无 is_index 行（cron 自选扫描）时仍传非空指数给报告。

    生产 11-10~11-13 日报只有「汇总报告 + 行业板块」、缺「各大指数」，
    根因就是 ``major_indexes = [r for r in results if r.get('is_index')]``
    在自选扫描下恒为空。
    """
    import argparse
    import mystery.apps.reports.excel_report as er
    import mystery.apps.reports.html_report as hr
    import mystery.services.index_analysis as ia
    import mystery.services.sector_analysis as sa
    from mystery.apps import cli

    cap = {}

    def fake_excel(results, path, **kw):
        cap["excel"] = kw
        return path

    def fake_html(results, path, **kw):
        cap["html"] = kw
        return path

    monkeypatch.setattr(er, "write_excel", fake_excel)
    monkeypatch.setattr(hr, "write_html", fake_html)
    monkeypatch.setattr(ia, "analyze_major_indexes",
                        lambda market: [_index_dict()])
    monkeypatch.setattr(sa, "analyze_focus_sectors", lambda db, **kw: [])
    monkeypatch.setattr(sa, "sector_summary_line", lambda s: "")
    monkeypatch.setattr(cli, "_turnover_qa_line", lambda *a, **k: "")
    import mystery.services.analyze as an
    monkeypatch.setattr(an, "market_env_summary", lambda market: {})
    monkeypatch.setattr(an, "market_env_line", lambda env: "")
    monkeypatch.setenv("MYSTERY_DB_PATH", str(tmp_path / "w92.db"))

    args = argparse.Namespace(cfg=None)
    # 自选扫描结果：只有个股行，没有 is_index
    results = [_stock_row(), _stock_row("600519.SH", "贵州茅台", 60.0)]
    cli._write_scan_report(results, args)

    assert cap.get("excel") is not None
    idx = cap["excel"]["major_indexes"]
    assert idx and idx[0]["name"] == "上证指数"
    assert idx[0]["close"] == 3634.35        # 裸 dict，不是包装行
    assert "index_analysis" not in idx[0]
    assert cap["html"]["major_indexes"] == idx


def test_scan_report_unwraps_index_result_rows(monkeypatch, tmp_path):
    """W92：全量扫描 results 含包装指数行时，解包后整表字段有值。"""
    import argparse
    import mystery.apps.reports.excel_report as er
    import mystery.apps.reports.html_report as hr
    import mystery.services.sector_analysis as sa
    from mystery.apps import cli

    cap = {}

    def fake_excel(results, path, **kw):
        cap["excel"] = kw
        cap["stock_results"] = results
        return path

    monkeypatch.setattr(er, "write_excel", fake_excel)
    monkeypatch.setattr(hr, "write_html",
                        lambda results, path, **kw: cap.update(html=kw) or path)
    monkeypatch.setattr(sa, "analyze_focus_sectors", lambda db, **kw: [])
    monkeypatch.setattr(sa, "sector_summary_line", lambda s: "")
    monkeypatch.setattr(cli, "_turnover_qa_line", lambda *a, **k: "")
    import mystery.services.analyze as an
    monkeypatch.setattr(an, "market_env_summary", lambda market: {})
    monkeypatch.setattr(an, "market_env_line", lambda env: "")
    monkeypatch.setenv("MYSTERY_DB_PATH", str(tmp_path / "w92b.db"))

    results = [_stock_row(), _index_result_row(_index_dict())]
    cli._write_scan_report(results, argparse.Namespace(cfg=None))
    idx = cap["excel"]["major_indexes"]
    assert idx and idx[0]["close"] == 3634.35
    assert "多方炮" in idx[0]["candle_daily"]
    assert "index_analysis" not in idx[0]
    # 个股表不含指数行（指数只进独立工作表）
    assert all(not r.get("is_index") for r in cap["stock_results"])
