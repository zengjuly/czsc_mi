"""W86：主力建仓 / 吸筹研判核心纯函数 + 报告字段推导测试。"""
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, "/home/ai/ai_runner/stock/czsc_mi")

from mystery.core.models import Bar
from mystery.core import accumulation as acc
from mystery.apps.reports import result_fields as rf


def _bars(closes, *, vols=None, base_price=None):
    """合成日 K：close 序列 → List[Bar]（high/low 按 close ±1%）。"""
    out = []
    base = datetime(2025, 1, 1)
    vols = vols or [1_000_000] * len(closes)
    for i, c in enumerate(closes):
        out.append(Bar(
            dt=(base + timedelta(days=i)).isoformat(),
            open=c * 0.995, high=c * 1.01, low=c * 0.99,
            close=float(c), volume=float(vols[i]),
        ))
    return out


def test_low_retrace_computed_correctly():
    """回撤比例 = 1 - price/近120高，落在 0~1，不允许负值。"""
    closes = [100.0] * 60 + [90.0] * 60 + [80.0] * 60
    a = acc.analyze_accumulation(_bars(closes))
    assert a["has_data"] is True
    assert a["retrace_pct"] is not None
    assert 0 <= a["retrace_pct"] <= 100


def test_low_flat_shrink_is_accumulation():
    """低位 + 横盘 + 缩量沉淀 + 低波动 → 强吸筹 / 疑似吸筹（非 none）。

    近 120 根窗口内需同时含「高点」与「回落后的低位」，否则回撤门闩
    看不到高位（与 scan_signals.high_120 近 120 根口径一致）。
    """
    closes = [100.0] * 240 + [70.0] * 60   # 近 120 根内高位 100 → 低位 70
    # 近 25 根：前 20 根常量、后 5 根缩量 → vol_trend < 0.9
    vols = [1_000_000] * (len(closes) - 5) + [700_000] * 5
    a = acc.analyze_accumulation(_bars(closes, vols=vols))
    assert a["retrace_pct"] >= acc.LOW_RETRACE * 100
    assert a["vol_state"] in ("缩量沉淀", "极度缩量")
    assert a["amp20"] is not None and a["amp20"] < acc.AMP_LOW
    assert a["level"] in ("strong", "medium", "weak")
    assert a["level"] != "none"


def test_falling_below_year_line_downgrades():
    """跌破年线时，即便证据分够 high 也不判 strong（严格大盘腿护栏）。"""
    closes = [200.0] * 150 + [120.0] * 150
    vols = [1_000_000] * 150 + [1_120_000] * 150  # 温和放量
    a = acc.analyze_accumulation(_bars(closes, vols=vols))
    assert a["above_year"] is False
    assert a["level"] != "strong"
    assert any("跌破年线" in e for e in a["evidence"])


def test_low_volume_drop_is_not_distribution():
    """低位放量下跌 = 打压吸筹 / 低位诱空，不能误判为高位派发。"""
    # 高位后持续下跌到低位，且近端放量（模拟打压），但价格远低于区间高点
    closes = [200.0] * 60 + list(range(195, 135, -1)) + [135.0] * 130
    vols = [1_000_000] * 60 + [1_300_000] * 60 + [1_000_000] * 130
    a = acc.analyze_accumulation(_bars(closes, vols=vols))
    assert a["stage"] != "派发"
    # 低位放量下跌应给出打压吸筹提示或非吸筹，但绝不误判派发
    assert a["level"] != "none" or a["stage"] != "派发"


def test_high_stagnant_volume_is_distribution():
    """高位 + 放量滞涨 → 派发，排除吸筹。"""
    closes = [100.0] * 150 + [100.0] * 149 + [100.2]
    vols = [1_000_000] * 150 + [1_000_000] * 145 + [1_600_000] * 5
    a = acc.analyze_accumulation(_bars(closes, vols=vols))
    assert a["stage"] == "派发" or a["level"] == "none"


def test_flat_series_not_false_high_or_low():
    """横盘序列（hi≈lo≈price）不得被误判为高位/低位（区间相对位置判据）。"""
    closes = [100.0] * 300
    a = acc.analyze_accumulation(_bars(closes, vols=[1_600_000] * 300))
    assert a["stage"] != "派发"          # 不得误判高位出货
    assert a["retrace_pct"] is not None and a["retrace_pct"] < 1.0
    assert a["near_low"] is False


def test_insufficient_data_unknown():
    a = acc.analyze_accumulation(_bars([100.0] * 10))
    assert a["level"] == "unknown"
    assert a["has_data"] is False
    assert a["conclusion"]


def test_accumulation_from_result_chip_low():
    """个股：chip_low=True + 阶段吸筹 → 判吸筹，且 evidence 含筹码低位。"""
    d = {"name": "测试股", "symbol": "600000.SH",
         "chip_low": True, "chip_low_unknown": False,
         "turnover_20": 1.2, "price": 10.0, "high_120": 15.0,
         "mystery": {"stage": {"stage": "吸筹"},
                     "signal": {"年线滤网": True}}}
    a = acc.accumulation_from_result(d)
    assert a["level"] in ("strong", "medium", "weak")
    assert any("筹码低位" in e for e in a["evidence"])
    assert a["chip_low"] is True


def test_accumulation_from_result_unknown_turnover():
    """换手缺失三态：不把 unknown 当 chip_low，明确提示禁止判定。"""
    d = {"name": "缺换手", "chip_low": False,
         "chip_low_unknown": True, "turnover_20": None,
         "mystery": {"stage": {"stage": "未知"}, "signal": {}}}
    a = acc.accumulation_from_result(d)
    assert a["chip_low"] is False
    assert any("换手数据缺失" in e for e in a["evidence"])


def test_accumulation_from_result_distribution():
    d = {"name": "派发股", "chip_low": False,
         "mystery": {"stage": {"stage": "派发"}, "signal": {}}}
    a = acc.accumulation_from_result(d)
    assert a["level"] == "none"


def test_result_fields_accumulation_text():
    """报告字段层：旧 payload 无 accumulation 字段也能纯函数推导，不报错。"""
    d = {"name": "旧结果", "chip_low": True,
         "turnover_20": 1.1, "price": 8.0, "high_120": 12.0,
         "mystery": {"stage": {"stage": "吸筹"}, "signal": {}}}
    assert rf.accumulation_text(d) in (
        "强吸筹", "疑似吸筹", "观察", "非吸筹", "数据不足")
    assert rf.accumulation_stage_text(d) == "吸筹"
    assert rf.accumulation_conclusion_text(d) != "—"
    assert rf.accumulation_evidence_text(d) != "—"


def test_accumulation_label_and_summary():
    assert acc.accumulation_label(None) == "—"
    items = [{"name": "A", "accumulation": {"level": "strong",
                                            "level_cn": "强吸筹"}},
             {"name": "B", "accumulation": {"level": "none"}}]
    line = acc.accumulation_summary_line(items)
    assert "A(强吸筹)" in line and "B" not in line


def test_core_has_no_io_imports():
    """core 零 IO：不 import 数据客户端 / 配置 / 数据库模块。

    只检查实际 import 语句与模块级赋值，不误伤 docstring 里说明性的
    「不改 RULE_VER」文字（与 W83 同类守卫的教训一致）。
    """
    import pathlib
    src = pathlib.Path(
        "/home/ai/ai_runner/stock/czsc_mi/mystery/core/accumulation.py"
    ).read_text(encoding="utf-8")
    code_lines = [ln for ln in src.splitlines()
                  if not ln.lstrip().startswith(("#", '"', "'"))]
    code = "\n".join(code_lines)
    for bad in ("import sqlite3", "from mystery.adapters",
                "from mystery.store", "import requests",
                "RULE_VER =", "rule_ver ="):
        assert bad not in code
