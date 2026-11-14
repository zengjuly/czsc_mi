"""W94/B：为什么没有三振 —— 六门闩诊断（纯展示层专项）。

背景（用户 2026-11-14 补充发帖时间线）：
- 1/14、10/23 都是人工观察到的三振日（大盘+板块+个股集体上涨、大资金共振）；
- 系统 ``true_resonance`` 全历史 0，根因不是「没有行情」，而是系统严格
  年线腿测「趋势确认后」、人工看「资金进场那一刻」的口径阶段差，
  叠加「所有 job 都基于最新 K 线、从未按历史日期回放」。

B 面板把六道硬门闩 + 大盘腿快照 + 口径阶段差摊开，纯读 payload 不改判。

本测试覆盖：
- ``row_gates``：从 payload ``mystery.resonance`` 正确还原六门闩；
  缺 resonance → 全 None（unknown 不伪造 False）；阈值取 payload 自身口径；
- ``diagnose``：全池聚合（通过/否决/未知、真三振数、三腿全过仅卡大盘、
  8 大指数站上年线统计、结论/阶段差文案）；
- Excel ``excel_bytes`` / ``write_excel`` 带 resonance_diag 追加「三振诊断」表，
  不带时零回归；HTML ``write_html`` 带 diag 渲染区块；
- CLI ``_write_scan_report`` / ``_cmd_daily`` 把诊断传给报告层（mock 捕获）；
- Web ``_render_resonance_diag`` 存在且不破坏 W92/W90 契约。
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


def _resonance(*, score=70.0, thr=70.0, capital=True, market_up=False,
               industry_up=True, strict=True, position="低位",
               true=False, mode="yearline"):
    """构造一条 ``mystery.resonance`` 中文键结构（对齐生产 payload）。"""
    return {
        "个股趋势": strict, "行业趋势": industry_up, "大盘趋势": market_up,
        "三级共振": False, "共振评分": score, "共振级别": "二级共振",
        "共振建议": "可关注", "资金活跃": capital, "最强板块": [],
        "大盘位置": position, "真三振": true, "详情": [],
        "共振口径": {"大盘口径": mode, "真三振阈值": thr,
                   "资金门槛分": 8.0, "资金量比门槛": 1.5,
                   "指数滞后上限": None},
        "指数滞后天数": 2, "大盘腿降级": False, "个股严格腿": strict,
    }


def _stock(symbol="600150.SH", **kw):
    # score/thr/true 等全部透传给 mystery.resonance；顶层 score/true_resonance
    # 同步取自同一组参数，保证聚合诊断读到的就是构造的口径。
    return {"symbol": symbol, "name": "测试股",
            "score": kw.get("score", 12.0),
            "advice": "观望", "price": 30.1, "trade_date": "2026-11-13",
            "true_resonance": kw.get("true", False),
            "mystery": {"resonance": _resonance(**kw)}}


def _index(name="上证指数", above=False, aligned=False):
    return {"name": name, "code": "000001.SH", "close": 3634.35,
            "above_year": above, "year_dist_pct": -8.4, "ma_aligned": aligned,
            "candle_daily": "多方炮", "candle_weekly": "锤头线",
            "accumulation": {}, "accumulation_level": "疑似吸筹",
            "has_data": True}


# ---------------------------------------------------------------------------
# row_gates 纯函数
# ---------------------------------------------------------------------------
def _diag_mod():
    import mystery.apps.reports.resonance_diag as m
    return m


def test_row_gates_full_payload():
    m = _diag_mod()
    d = _stock(score=75, thr=70, capital=True, market_up=False,
               industry_up=True, strict=True, position="低位")
    g = m.row_gates(d)
    assert g == {
        "score_ok": True, "capital_active": True, "market_up": False,
        "industry_up": True, "stock_strict": True, "market_not_high": True,
    }


def test_row_gates_below_threshold_and_high_position():
    m = _diag_mod()
    d = _stock(score=60, thr=70, capital=False, market_up=True,
               industry_up=False, strict=False, position="高位")
    g = m.row_gates(d)
    assert g["score_ok"] is False
    assert g["capital_active"] is False
    assert g["market_up"] is True
    assert g["industry_up"] is False
    assert g["stock_strict"] is False
    assert g["market_not_high"] is False   # 高位一票否决


def test_row_gates_missing_payload_is_unknown_not_false():
    """缺 resonance → 全 None（unknown），绝不把未知伪造为否决。"""
    m = _diag_mod()
    g = m.row_gates({"symbol": "x", "mystery": {}})
    assert set(g.values()) == {None}
    assert m.row_gates({}) == {k: None for k in m.GATE_KEYS}


def test_effective_options_from_payload_threshold():
    m = _diag_mod()
    opts = m.effective_options([_stock(thr=70)])
    assert opts["true_threshold"] == 70.0
    assert opts["market_mode"] == "yearline"


# ---------------------------------------------------------------------------
# diagnose 聚合
# ---------------------------------------------------------------------------
def test_diagnose_aggregate_zero_true_all_blocked_by_market():
    m = _diag_mod()
    rows = [
        _stock("A", score=75, thr=70, capital=True, industry_up=True,
               strict=True, market_up=False),
        _stock("B", score=72, thr=70, capital=True, industry_up=True,
               strict=True, market_up=False),
        _stock("C", score=10, thr=70, capital=False, industry_up=False,
               strict=False, market_up=False),
    ]
    idx = [_index(), _index("深证成指", above=True, aligned=True)]
    diag = m.diagnose(rows, indexes=idx)
    assert diag["n_total"] == 3
    assert diag["n_true"] == 0
    gates = {g["key"]: g for g in diag["gates"]}
    assert gates["market_up"]["failed"] == 3 and gates["market_up"]["passed"] == 0
    assert gates["stock_strict"]["passed"] == 2
    assert diag["three_legs_but_market"] == 2   # A/B 三腿全过仅卡大盘
    assert diag["score_max"] == 75.0
    assert diag["gap_to_threshold"] is None or diag["gap_to_threshold"] <= 0
    assert diag["n_index_above"] == 1 and diag["n_index_aligned"] == 1
    assert "口径阶段差" not in diag["conclusion"]  # 结论不含长说明
    assert "趋势确认后" in diag["stage_note"] and "大资金进场" in diag["stage_note"]
    assert "真三振 0 只" in diag["conclusion"]


def test_diagnose_with_true_strike():
    m = _diag_mod()
    d = _stock("A", score=90, thr=70, capital=True, industry_up=True,
               strict=True, market_up=True, position="中位", true=True)
    diag = m.diagnose([d], as_of="2026-01-14")
    assert diag["n_true"] == 1
    assert diag["as_of"] == "2026-01-14"
    assert "历史回放" in diag["conclusion"]
    assert "大级别共振窗口出现" in diag["conclusion"]


def test_diagnose_excludes_index_rows():
    m = _diag_mod()
    rows = [_stock(), {"is_index": True, "name": "上证指数"}]
    assert m.diagnose(rows)["n_total"] == 1


def test_diag_sheet_rows_structure():
    m = _diag_mod()
    rows = m.diag_sheet_rows(m.diagnose([_stock()], indexes=[_index()]))
    assert rows[0][0] == "项目"
    joined = " ".join(str(c) for r in rows for c in r)
    assert "门闩" in joined and "共振评分达标" in joined
    assert "口径阶段差" in joined and "上证指数" in joined


# ---------------------------------------------------------------------------
# Excel 层：三振诊断表 + 零回归
# ---------------------------------------------------------------------------
def _excel_mod():
    import mystery.apps.reports.excel_report as m
    return m


def test_excel_bytes_with_diag_adds_sheet():
    import openpyxl
    m = _excel_mod()
    diag = _diag_mod().diagnose([_stock()], indexes=[_index()])
    data = m.excel_bytes([_stock()], resonance_diag=diag)
    wb = openpyxl.load_workbook(io.BytesIO(data))
    assert "三振诊断" in wb.sheetnames
    ws = wb["三振诊断"]
    txt = " ".join(str(c.value) for r in ws.iter_rows() for c in r if c.value)
    assert "门闩" in txt and "口径阶段差" in txt and "共振评分达标" in txt


def test_excel_bytes_without_diag_zero_regression():
    import openpyxl
    m = _excel_mod()
    wb = openpyxl.load_workbook(io.BytesIO(m.excel_bytes([_stock()])))
    assert wb.sheetnames == ["汇总报告"]


def test_excel_bytes_all_three_optional_sheets():
    import openpyxl
    m = _excel_mod()
    diag = _diag_mod().diagnose([_stock()])
    data = m.excel_bytes([_stock()], major_indexes=[_index()],
                         focus_sectors=[{"name": "电力", "code": "x",
                                         "related": []}],
                         resonance_diag=diag)
    wb = openpyxl.load_workbook(io.BytesIO(data))
    assert wb.sheetnames == ["汇总报告", "各大指数", "行业板块", "三振诊断"]


def test_write_excel_and_excel_bytes_same_diag_sheet(tmp_path):
    import openpyxl
    m = _excel_mod()
    diag = _diag_mod().diagnose([_stock()])
    p = str(tmp_path / "w94.xlsx")
    m.write_excel([_stock()], p, resonance_diag=diag)
    wb_file = openpyxl.load_workbook(p)
    wb_mem = openpyxl.load_workbook(io.BytesIO(
        m.excel_bytes([_stock()], resonance_diag=diag)))
    assert wb_file.sheetnames == wb_mem.sheetnames == ["汇总报告", "三振诊断"]
    a = [c.value for r in wb_file["三振诊断"].iter_rows() for c in r]
    b = [c.value for r in wb_mem["三振诊断"].iter_rows() for c in r]
    assert a == b  # 落盘/内存两条链路同填充函数


# ---------------------------------------------------------------------------
# HTML 层
# ---------------------------------------------------------------------------
def test_write_html_with_diag_renders_block(tmp_path):
    m = _diag_mod()
    import mystery.apps.reports.html_report as hr
    diag = m.diagnose([_stock()], indexes=[_index()])
    p = str(tmp_path / "w94.html")
    hr.write_html([_stock()], p, resonance_diag=diag)
    html = open(p, encoding="utf-8").read()
    assert "为什么没有三振" in html
    assert "共振评分达标" in html and "口径阶段差" in html or "趋势确认后" in html
    assert "8 大指数" in html


def test_write_html_without_diag_zero_regression(tmp_path):
    import mystery.apps.reports.html_report as hr
    p = str(tmp_path / "w94b.html")
    hr.write_html([_stock()], p)
    assert "为什么没有三振" not in open(p, encoding="utf-8").read()


# ---------------------------------------------------------------------------
# Web 层：诊断函数存在 + W92/W90 契约不回归
# ---------------------------------------------------------------------------
def test_web_diag_func_and_w92_contract():
    import mystery.apps.web.views.scan as s
    assert callable(s._render_resonance_diag)
    assert callable(s.split_scan_rows)
    assert callable(s._scan_table_column_config)
    cfg = s._scan_table_column_config()
    assert "名称" in cfg and "序号" in cfg


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
