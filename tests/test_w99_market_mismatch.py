"""W99 专项：Web「仅大盘不匹配」筛选口径与边界。

口径（用户拍板「两种都算」）：
    个股严格腿 ✓ 且 行业向上 ✓ 且 ( 大盘向上 ✗ 或 大盘非高位 ✗ )
纯展示过滤，不改判信号、不写库、不动 RULE_VER。
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _resonance(*, strict=True, industry_up=True, market_up=False,
               position="低位", gate="hard", score=70.0, thr=70.0):
    return {
        "个股趋势": strict, "行业趋势": industry_up, "大盘趋势": market_up,
        "共振评分": score, "资金活跃": True, "大盘位置": position,
        "真三振": False, "个股严格腿": strict,
        "共振口径": {"大盘口径": "ma", "真三振阈值": thr, "高位风控": gate},
    }


def _stock(symbol, **kw):
    return {"symbol": symbol, "name": symbol, "is_index": False,
            "true_resonance": False,
            "mystery": {"resonance": _resonance(**kw)}}


@pytest.fixture
def m():
    import mystery.apps.reports.resonance_diag as mod
    return mod


def test_case1_market_down_low_matches(m):
    """情形1：个股✓行业✓、大盘趋势向下（低位）→ 匹配。"""
    d = _stock("A", market_up=False, position="低位", strict=True,
               industry_up=True)
    assert m.only_market_mismatch([d]) == [d]


def test_case2_high_position_hard_matches(m):
    """情形2：三腿全过但大盘高位被 hard 一票否决 → 匹配。"""
    d = _stock("B", market_up=True, position="高位", gate="hard",
               strict=True, industry_up=True)
    assert m.only_market_mismatch([d]) == [d]


def test_high_position_soft_relaxed_not_matched(m):
    """高位风控放宽为 soft 时门闩恒 True，高位不再算不匹配 → 不匹配。"""
    d = _stock("C", market_up=True, position="高位", gate="soft",
               strict=True, industry_up=True)
    assert m.only_market_mismatch([d]) == []


def test_high_position_off_relaxed_not_matched(m):
    """off 同理：三腿全过且高位门闩移除 → 不匹配。"""
    d = _stock("D", market_up=True, position="高位", gate="off",
               strict=True, industry_up=True)
    assert m.only_market_mismatch([d]) == []


def test_stock_strict_false_not_matched(m):
    """个股严格腿未过 → 不匹配（严格腿口径，非宽松「个股趋势」）。"""
    d = _stock("E", market_up=False, strict=False, industry_up=True)
    assert m.only_market_mismatch([d]) == []


def test_industry_down_not_matched(m):
    """行业向下 → 不匹配。"""
    d = _stock("F", market_up=False, strict=True, industry_up=False)
    assert m.only_market_mismatch([d]) == []


def test_all_three_legs_ok_not_matched(m):
    """大盘向上 + 非高位 + 个股行业过 → 无大盘问题，不匹配。"""
    d = _stock("G", market_up=True, position="低位", gate="hard",
               strict=True, industry_up=True)
    assert m.only_market_mismatch([d]) == []


def test_missing_resonance_not_fabricated(m):
    """缺 resonance 字段 → row_gates 全 None，不把 unknown 伪造成否决。"""
    assert m.only_market_mismatch([{"symbol": "H", "mystery": {}}]) == []
    assert m.only_market_mismatch([{}]) == []


def test_index_rows_excluded(m):
    """指数行直接剔除。"""
    d = _stock("IDX", market_up=False, strict=True, industry_up=True)
    d["is_index"] = True
    assert m.only_market_mismatch([d]) == []


def test_mixed_pool_keeps_only_qualified(m):
    """混合池：只保留符合口径的个股，保持输入顺序。"""
    hit1 = _stock("H1", market_up=False, position="低位")
    hit2 = _stock("H2", market_up=True, position="高位", gate="hard")
    miss1 = _stock("M1", market_up=True, position="低位")       # 大盘没问题
    miss2 = _stock("M2", market_up=False, strict=False)          # 个股严格腿未过
    miss3 = _stock("M3", market_up=True, position="高位", gate="soft")  # 已放宽
    out = m.only_market_mismatch([miss1, hit1, miss2, hit2, miss3])
    assert [d["symbol"] for d in out] == ["H1", "H2"]


def test_web_radio_contains_new_option():
    """Web 扫描页信号过滤含「仅大盘不匹配」选项，且不改 DB signal_map 语义。

    直接读源码文本，避免在无 streamlit 的环境（如最小 CI）里 import 失败。
    """
    scan_py = os.path.join(ROOT, "mystery/apps/web/views/scan.py")
    with open(scan_py, encoding="utf-8") as f:
        src = f.read()
    assert '"仅大盘不匹配"' in src
    assert '"仅大盘不匹配": None' in src
    assert 'only_market_mismatch(rows)' in src
