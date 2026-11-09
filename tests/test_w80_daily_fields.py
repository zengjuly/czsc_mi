"""W80 回归：日报字段对齐（turnover_20 数值 + chip 相关标签，全部来自 to_dict()）。

覆盖：
1. result_fields 纯函数三态映射（chip_low / chip_low_unknown / chip_quiet /
   换手缺失、turnover_20 数值与来源标记）。
2. Excel 汇总页固定含 turnover_20/筹码低位/筹码标签/换手来源 列，且值来自
   payload 顶层字段（不重算）；「名称」仍在「代码」前。
3. HTML 卡片含 turnover_20 数值与 chip 标签，且「筹码低位」标签改用
   payload.chip_low（与 Excel/扫描同源）。
"""
from __future__ import annotations

import io

import openpyxl

from mystery.apps.reports import result_fields as rf
from mystery.apps.reports.excel_report import excel_bytes
from mystery.apps.reports.html_report import write_html


def _mk(symbol, name, chip_low=False, chip_unknown=False, chip_quiet=False,
        turnover_20=2.1036):
    return {
        "symbol": symbol, "name": name, "trade_date": "2026-11-08",
        "score": 70.0, "advice": "持有", "true_resonance": True, "price": 10.0,
        "turnover_20": turnover_20, "high_120": 12.0,
        "chip_low": chip_low, "chip_low_unknown": chip_unknown,
        "chip_quiet": chip_quiet,
        "mystery": {"vap_atr": {"POC": 9.0, "突破信号": True, "自适应周期": {}},
                    "platform": {"平台状态": "平台", "突破信号": False},
                    "main_wave": {"主升浪状态": "主升浪"},
                    "resonance": {"共振评分": 60},
                    "checklist8": {"满足数量": 6},
                    "signal": {"年线滤网": False}, "technical": {}},
        "financial": {"PE": 20.0, "PB": 2.5}, "sector": {"行业名称": "测试"},
        "chan": {}, "czsc_ver": "",
    }


# ---------------------------------------------------------------- 纯函数
def test_result_fields_chip_states():
    assert rf.turnover_20_value({"turnover_20": 2.1036}) == 2.1
    assert rf.turnover_20_value({}) is None
    assert rf.turnover_20_value({"turnover_20": ""}) is None
    assert rf.chip_low_text({"chip_low": True}) == "✅"
    assert rf.chip_low_text({"chip_low_unknown": True, "chip_low": False}) == "换手缺失"
    assert rf.chip_tag_text({"chip_low": True}) == "低位缩量"
    assert rf.chip_tag_text({"chip_quiet": True}) == "高位缩量"
    assert rf.chip_tag_text({"chip_low_unknown": True}) == "换手缺失"
    assert rf.turnover_source_text({"turnover_20": 0}) == "低换手"
    assert rf.turnover_source_text({}) == "缺失"


# ---------------------------------------------------------------- Excel
def test_excel_has_w80_columns_and_values_from_payload():
    rows = [
        _mk("sh600519", "贵州茅台", chip_low=True, turnover_20=1.5),
        _mk("sz000001", "平安银行", chip_unknown=True, turnover_20=None),
        _mk("sh600150", "中国船舶", chip_quiet=True, turnover_20=0.8),
    ]
    wb = openpyxl.load_workbook(io.BytesIO(excel_bytes(rows)))
    ws = wb["汇总报告"]
    headers = [c.value for c in ws[1]]
    for col in ("近20日均换手(%)", "筹码低位", "筹码标签", "换手来源"):
        assert col in headers, f"缺 W80 列 {col}"
    # 名称在代码前（W76 口径不回退）
    assert headers.index("名称") < headers.index("代码")
    name_idx = headers.index("名称")
    data = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        vals = dict(zip(headers, row))
        data[vals["名称"]] = vals
    m = data["贵州茅台"]
    assert abs(m["近20日均换手(%)"] - 1.5) < 1e-6   # 数值来自 payload
    assert m["筹码低位"] == "✅" and m["筹码标签"] == "低位缩量"
    p = data["平安银行"]
    assert p["近20日均换手(%)"] is None             # 缺失不伪装成 0
    assert p["筹码低位"] == "换手缺失" and p["换手来源"] == "缺失"
    s = data["中国船舶"]
    assert s["筹码标签"] == "高位缩量"


def test_excel_w80_columns_present_even_when_payload_missing_fields():
    """旧 job payload 无 turnover_20/chip 字段时列仍存在、值兜底（不报错）。"""
    rows = [{"symbol": "sh600519", "name": "贵州茅台", "score": 50.0,
             "trade_date": "2026-11-08", "mystery": {}, "sector": {},
             "financial": {}, "chan": {}}]
    wb = openpyxl.load_workbook(io.BytesIO(excel_bytes(rows)))
    headers = [c.value for c in wb["汇总报告"][1]]
    for col in ("近20日均换手(%)", "筹码低位", "筹码标签", "换手来源"):
        assert col in headers


# ---------------------------------------------------------------- HTML
def test_html_has_w80_fields_and_chip_from_payload(tmp_path):
    rows = [_mk("sh600519", "贵州茅台", chip_low=True, turnover_20=1.5)]
    path = str(tmp_path / "r.html")
    write_html(rows, path)
    html = open(path, encoding="utf-8").read()
    assert "近20日均换手(%)" in html
    assert "筹码低位" in html
    assert "筹码标签" in html
    assert "换手来源" in html
