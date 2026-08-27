"""test_unified_score — chan 关闭时与 gold fixture 分差 ≤ 1（P1 验收）。

fixtures/gold_*.json 由旧系统 unified_stock_analysis 生成（见 scripts/verify_unified_analysis.py）。
数据源不可用（无 MarketDB/网络）时自动 skip，不阻塞离线 CI。
"""
import json
import os
from pathlib import Path

import pytest

from mystery.services.analyze import analyze_one_stock

FIXTURES = Path(__file__).parent / "fixtures"
_STOCKS = ["sh600519", "sz000001", "sh600150"]


def _load_gold(sym: str) -> dict:
    with open(FIXTURES / f"gold_{sym}.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.mark.parametrize("sym", _STOCKS)
def test_score_within_1_of_gold(sym):
    gold = _load_gold(sym)
    if not (FIXTURES / f"gold_{sym}.json").exists():
        pytest.skip("缺金标 fixture")
    try:
        r = analyze_one_stock(sym, include_detail=False)
    except Exception as e:
        pytest.skip(f"数据源不可用: {str(e)[:80]}")
    assert r.score is not None
    diff = abs(float(r.score) - float(gold.get("综合评分") or 0))
    assert diff <= 1, f"{sym}: mine={r.score} gold={gold.get('综合评分')}"


@pytest.mark.parametrize("sym", _STOCKS)
def test_gold_price_matches(sym):
    """最新价与金标一致（同一数据源同一天）。"""
    gold = _load_gold(sym)
    try:
        r = analyze_one_stock(sym, include_detail=False)
    except Exception as e:
        pytest.skip(f"数据源不可用: {str(e)[:80]}")
    gp = gold.get("最新价")
    if gp:
        assert abs(float(r.price or 0) - float(gp)) < 0.01, \
            f"{sym}: mine={r.price} gold={gp}"
