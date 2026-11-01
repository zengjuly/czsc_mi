"""G4 报告可视化测试：mystery.patterns 三形态在 HTML/Excel 报告层正确渲染。

（Web 层为 streamlit 运行时，靠 import 冒烟 + 结构与另两层一致保证。）
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mystery.apps.reports import html_report as H
from mystery.apps.reports import excel_report as E


def _pat():
    return {
        "强势反包阳": {"hit": True, "basic": True, "detail": {
            "昨阴": "10.00→8.00", "今阳": "7.50→10.50", "吞没最高价": True,
            "放量确认": True, "回撤%": 18.2, "站上MA5": False, "近期低位": True,
            "强度": "强势"}},
        "缺口": {"hit": True, "gap_up": True, "gap_type": "突破",
                 "gap_magnitude_pct": 3.1, "unfilled_days": 4, "detail": {
                     "缺口位": 9.8, "类型": "突破", "跳空幅度%": 3.1,
                     "横盘前兆": True, "前20日涨幅%": 6.0, "站上MA20": True,
                     "三日不补": True, "未补天数": 4}},
        "低位地量": {"hit": True, "低位": True, "量能压缩": True, "detail": {
            "回撤%": 25.0, "近20日地量": True, "量/60日均量": 0.32,
            "MA20<MA60(长期缩量)": True}},
    }


def _d():
    return {
        "symbol": "sh600000", "name": "测试银行", "trade_date": "2026-11-02",
        "price": 10.5, "score": 62.0, "advice": "持有", "true_resonance": False,
        "turnover_20": 1.5,
        "mystery": {
            "vap_atr": {"突破信号": True, "自适应周期": {"avg_turnover": 1.5}},
            "platform": {"突破信号": False, "平台状态": "-"},
            "main_wave": {"主升浪状态": "主升", "判定依据": ["MA5>MA10"]},
            "resonance": {"详情": ["个股趋势↑"]},
            "patterns": _pat(),
        },
        "financial": {"PE": 5.0, "PB": 0.7, "roe": 12.0, "report_date": "2026-09-30"},
        "sector": {"行业名称": "银行", "行业趋势分": 60},
        "chan": {},
    }


def test_html_report_pattern_tags():
    card = H._stock_card(_d())
    assert "强势反包阳" in card
    assert "缺口·突破" in card
    assert "低位地量" in card
    assert "向上跳空缺口[突破]" in card


def test_excel_report_pattern_section():
    flat = E._detail_flat(_d())
    keys = list(flat.keys())
    assert any("强势反包阳(信号)" in k for k in keys)
    assert any("缺口类型" in k for k in keys)
    assert any("低位地量(信号)" in k for k in keys)
    assert flat.get("缺口类型") == "突破"
    assert flat.get("强势反包阳(信号)") in ("✅", "是")
    assert flat.get("低位地量(信号)") in ("✅", "是")


def test_html_gap_exhaustion_warn_class():
    d = _d()
    d["mystery"]["patterns"]["缺口"]["gap_type"] = "衰竭"
    card = H._stock_card(d)
    assert "缺口·衰竭" in card
    assert "signal-tag warn" in card
