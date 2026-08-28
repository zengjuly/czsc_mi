"""test_score_offline — W2.1 离线金标：fixture K线 → core pipeline → 分数（零 IO）。

不 import adapters；不读网/读库。fixture 为金标日 2026-08-27 的 qfq 行情
（含 1d/1w/1M，由原机 ths_official 拉取后固化，见 fixtures/README）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mystery.core.models import Bar, BarSeries, MarketContext
from mystery.core.pipeline import run_mystery
from mystery.core.scorer import combine

FIX = Path(__file__).parent / "fixtures"
_STOCKS = ["sh600519", "sz000001", "sh600150"]


def _load_bars(sym: str) -> dict:
    data = json.loads((FIX / f"bars_{sym}_daily.json").read_text(encoding="utf-8"))
    out = {}
    for freq, blob in data.items():
        if freq == "context":
            continue
        bars = [Bar(dt=b["dt"], open=b["open"], high=b["high"], low=b["low"],
                    close=b["close"], volume=b["volume"], amount=b["amount"],
                    turnover=b["turnover"], pct_chg=b["pct_chg"])
                for b in blob["bars"]]
        out[freq] = BarSeries(symbol=blob["symbol"], freq=blob["freq"],
                              adjust=blob["adjust"], bars=bars, source=blob["source"])
    return out


def _load_ctx(sym: str) -> MarketContext:
    """fixture 固化的大盘/行业上下文（与金标同源同参）。"""
    data = json.loads((FIX / f"bars_{sym}_daily.json").read_text(encoding="utf-8"))
    c = data.get("context") or {}
    idx_blob = c.get("index_bars") or {}
    index_bars = None
    if idx_blob.get("bars"):
        index_bars = BarSeries(
            symbol=idx_blob["symbol"], freq=idx_blob["freq"],
            adjust=idx_blob["adjust"], source=idx_blob["source"],
            bars=[Bar(dt=b["dt"], open=b["open"], high=b["high"], low=b["low"],
                      close=b["close"], volume=b["volume"], amount=b["amount"],
                      turnover=b["turnover"], pct_chg=b["pct_chg"])
                  for b in idx_blob["bars"]])
    return MarketContext(index_bars=index_bars,
                         industry_name=c.get("industry_name", "未知"),
                         industry_score=c.get("industry_score"),
                         industry_up=c.get("industry_up"))


def _score(sym: str) -> float:
    series = _load_bars(sym)
    bd = run_mystery(series["1d"], weekly=series.get("1w"),
                     monthly=series.get("1M"), ctx=_load_ctx(sym),
                     include_detail=False)
    score, _, _ = combine(bd, None, chan_enabled=False)
    return float(score)


def _gold(sym: str) -> dict:
    return json.loads((FIX / f"gold_{sym}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("sym", _STOCKS)
def test_offline_score_matches_gold(sym):
    """离线分与金标分差 ≤ 1（chan 关闭 = Mystery 原分）。"""
    score = _score(sym)
    gold = _gold(sym)
    assert abs(score - float(gold.get("综合评分") or 0)) <= 1, \
        f"{sym}: mine={score} gold={gold.get('综合评分')}"


@pytest.mark.parametrize("sym", ["sh600519", "sh600150"])
def test_veto_stocks_score_zero(sym):
    """年线否决票（茅台/船舶）离线分必须为 0，不允许被修成高分。"""
    assert _score(sym) == 0.0


def test_fixtures_have_weekly():
    """fixture 必须带 1w（周线锚定参与评分，缺了不可复现）。"""
    for sym in _STOCKS:
        series = _load_bars(sym)
        assert series["1w"].bars, f"{sym} fixture 缺周线"
        assert series["1w"].freq == "1w"


def test_offline_no_adapters_import():
    """本测试文件禁止 import adapters（纯 core 路径）。"""
    import subprocess
    import sys

    code = ("import sys; "
            "sys.modules['mystery.adapters']=None; "
            "import mystery.core.pipeline, mystery.core.scorer, "
            "mystery.core.models")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
