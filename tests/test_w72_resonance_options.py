"""W72 — 三振共振口径参数化（审计建议 5/7）的回归测试。

对应《三振实现审计报告》建议 5（大盘腿严格口径 + 指数滞后）与建议 7
（85 阈值/资金门槛重评 + 三级共振可过滤信号）的落地，全部走
「默认零变化 + 显式开关」：

- core 零 IO：口径由 service 层从 env 注入，core 只吃 options；
- 默认（无 options / 空 options / 无 env）逐字段与 W71/0.10.30 一致，
  不改综合评分 / 三类信号 / AnalysisResult / RULE_VER；
- 严格大盘腿（年线 MA250 + 多头顺次排列）、85 阈值、资金门槛、
  指数滞后降级均为可选，缺数据/unknown 一律不放行；
- 三级共振只读暴露为可过滤信号，不改 classify 既有返回契约。

全部离线：合成 OHLC + 纯 dict 入参 + monkeypatch env，不读写生产库。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from mystery.core import resonance as res
from mystery.core.mystery_rules import MysteryLogic
from mystery.core.models import Bar, BarSeries, MarketContext
from mystery.core.pipeline import run_mystery
from mystery.core.scan_signals import classify, filter_by_signal
from mystery.core.scorer import combine
from mystery.services import analyze as svc


# ---------------------------------------------------------------- 构造工具

def _daily(n: int = 300, up: bool = True, seed: int = 7,
           end: str = "2026-11-06") -> pd.DataFrame:
    """合成日K：稳定上行(默认)，含全部指标列 + 末根放量。"""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(end=end, periods=n)
    drift = 0.0016 if up else -0.0016
    close = 10 * np.cumprod(1 + rng.normal(drift, 0.010, n))
    open_ = close * (1 + rng.normal(0, 0.003, n))
    high = np.maximum(open_, close) * (1 + abs(rng.normal(0, 0.004, n)))
    low = np.minimum(open_, close) * (1 - abs(rng.normal(0, 0.004, n)))
    vol = rng.uniform(5e6, 9e6, n)
    vol[-1] = vol[-6:-1].mean() * 2.0
    df = pd.DataFrame({
        "日期": dates.strftime("%Y-%m-%d"), "开盘价": open_, "最高价": high,
        "最低价": low, "收盘价": close, "成交量": vol,
        "成交额": vol * close, "换手率": rng.uniform(3, 6, n),
    })
    for w in (5, 10, 20, 60, 250):
        df[f"MA{w}"] = df["收盘价"].rolling(w, min_periods=1).mean()
    df["均线排列"] = 1 if up else -1
    df["量比"] = df["成交量"] / df["成交量"].rolling(5, min_periods=1).mean()
    return df


def _index(n: int = 160, up: bool = True, end: str = "2026-11-06") -> pd.DataFrame:
    """W71 V 型指数：收盘>MA20/MA60（向上）+ 近120日中位（非高位）。"""
    dates = pd.bdate_range(end=end, periods=n)
    if up:
        first = np.linspace(3550, 3500, 40)
        down = np.linspace(3500, 3000, 60)
        up_leg = np.linspace(3005, 3400, n - 100)
        close = np.concatenate([first, down, up_leg])
    else:
        close = np.linspace(3600, 2700, n)
    df = pd.DataFrame({"日期": dates.strftime("%Y-%m-%d"), "收盘价": close})
    df["MA20"] = df["收盘价"].rolling(20, min_periods=1).mean()
    df["MA60"] = df["收盘价"].rolling(60, min_periods=1).mean()
    return df


def _index_yearline(n: int = 300, up: bool = True,
                    end: str = "2026-11-06") -> pd.DataFrame:
    """严格口径用：≥250 根，单调上行(站年线+多头顺次排列) / 破年线。"""
    dates = pd.bdate_range(end=end, periods=n)
    if up:
        close = np.linspace(3000, 3600, n)
    else:
        close = np.linspace(3600, 2900, n)   # 末段跌破 MA250
    return pd.DataFrame({"日期": dates.strftime("%Y-%m-%d"), "收盘价": close})


def _series(df: pd.DataFrame, symbol: str = "600519.SH",
            freq: str = "1d") -> BarSeries:
    bars = []
    for r in df.itertuples():
        c = float(r.收盘价)
        o = float(getattr(r, "开盘价", c))
        h = float(getattr(r, "最高价", max(o, c)))
        lo = float(getattr(r, "最低价", min(o, c)))
        v = float(getattr(r, "成交量", 0.0))
        a = float(getattr(r, "成交额", 0.0))
        t = float(getattr(r, "换手率", 0.0)) if hasattr(r, "换手率") else None
        bars.append(Bar(dt=str(r.日期), open=o, high=h, low=lo, close=c,
                        volume=v, amount=a, turnover=t))
    return BarSeries(symbol=symbol, freq=freq, adjust="qfq",
                     bars=bars, source="test")


def _ctx(index_df: pd.DataFrame, industry: bool = True) -> MarketContext:
    return MarketContext(index_bars=_series(index_df, "000001.SH"),
                         industry_up=industry)


def _four_ok(capital_score: float = 20.0, capital_active: bool = True,
             position: str = "中位"):
    """四维全过入参（个股/大盘/行业/资金），默认 100 分。"""
    return ({"基础过滤": True, "均线多头": True},
            {"趋势方向": "向上", "position": position},
            {"整体趋势": "向上", "detail": "强势5/弱势1"},
            {"active": capital_active, "score": capital_score, "detail": "量比2.0"})


# ---------------------------------------------------------------- 选项解析

def test_default_options_equal_w71_constants():
    """零变化基线：默认口径逐项等于 W71 硬编码值。"""
    o = res.resolve_resonance_options(None)
    assert o["market_mode"] == "ma"
    assert o["true_threshold"] == 85.0
    assert o["capital_min_score"] == 8.0
    assert o["capital_min_volume_ratio"] == 1.5
    assert o["index_max_lag_days"] is None
    assert res.resolve_resonance_options({}) == o


def test_resolve_rejects_invalid_values():
    """非法值保守回退，绝不因脏 env 放宽触发条件。"""
    o = res.resolve_resonance_options({
        "market_mode": "BOGUS", "true_threshold": -1,
        "capital_min_score": "abc", "index_max_lag_days": "off"})
    assert o["market_mode"] == "ma"
    assert o["true_threshold"] == 85.0
    assert o["capital_min_score"] == 8.0
    assert o["index_max_lag_days"] is None
    o2 = res.resolve_resonance_options({
        "market_mode": "yearline", "true_threshold": "80",
        "index_max_lag_days": "3"})
    assert o2["market_mode"] == "yearline"
    assert o2["true_threshold"] == 80.0
    assert o2["index_max_lag_days"] == 3
    assert res.resolve_resonance_options(
        {"index_max_lag_days": -5})["index_max_lag_days"] is None


# ---------------------------------------------------------------- 阈值参数化（建议7）

def test_default_85_boundary_and_gate_label_unchanged():
    """W71 契约：默认 85 分边界触发，门闩标签仍为「共振评分≥85」。"""
    r = res.calculate_resonance_score(*_four_ok(capital_score=5.0))
    assert r["score"] == 85.0 and r["is_true_three_strike"] is True
    assert r["true_threshold"] == 85.0
    r2 = res.calculate_resonance_score(*_four_ok(capital_score=4.9))
    assert r2["is_true_three_strike"] is False
    assert "共振评分≥85" in r2["reject_reasons"]
    assert "共振评分≥85" in r2["gates"]


def test_true_threshold_parameterized():
    """阈值降到 80：80 分由 False→True，且门闩标签动态化。"""
    base = res.calculate_resonance_score(*_four_ok(capital_score=0.0))
    assert base["score"] == 80.0 and base["is_true_three_strike"] is False
    low = res.calculate_resonance_score(*_four_ok(capital_score=0.0),
                                        true_threshold=80.0)
    assert low["is_true_three_strike"] is True
    assert low["true_threshold"] == 80.0
    assert "共振评分≥80" in low["gates"]
    high = res.calculate_resonance_score(*_four_ok(capital_score=5.0),
                                         true_threshold=90.0)
    assert high["is_true_three_strike"] is False
    assert "共振评分≥90" in high["reject_reasons"]


def test_default_vs_explicit_default_score_identical():
    """显式传默认阈值与不传，除新增诊断键外结果逐字段一致。"""
    a = res.calculate_resonance_score(*_four_ok())
    b = res.calculate_resonance_score(*_four_ok(), true_threshold=85.0)
    a.pop("true_threshold", None)
    b.pop("true_threshold", None)
    assert a == b


# ---------------------------------------------------------------- 资金门槛（建议7）

def _capital_df(volume_ratio: float = 1.6) -> pd.DataFrame:
    n = 8
    vol = np.array([1e6] * (n - 1) + [1e6 * volume_ratio])
    return pd.DataFrame({"成交量": vol, "成交额": np.full(n, 1e7),
                         "换手率": np.zeros(n)})


def test_capital_min_score_parameterized():
    """门槛可调：默认 8/1.5 → active；抬高到 9/2.0 → 不 active。"""
    df = _capital_df(1.6)
    base = res.analyze_capital_flow(df)
    assert base["score"] == 8 and base["active"] is True
    strict = res.analyze_capital_flow(df, min_score=9.0,
                                      min_volume_ratio=2.0)
    assert strict["score"] == 8 and strict["active"] is False


# ---------------------------------------------------------------- 严格大盘腿（建议5）

def test_yearline_insufficient_bars_is_unknown():
    """<250 根 → 趋势未知，绝不放行大盘腿。"""
    r = res.analyze_market_trend_yearline(_index_yearline(n=200))
    assert r["趋势方向"] == "未知"
    assert r["大盘口径"] == "年线MA250+多头顺次排列"
    assert any("数据不足" in x for x in r["详情"])


def test_yearline_bullish_aligned_is_up():
    """站上年线 + 多头顺次排列 → 向上。"""
    r = res.analyze_market_trend_yearline(_index_yearline(n=300, up=True))
    assert r["趋势方向"] == "向上"
    assert r["position"] == "高位"       # 单调上行近120日分位=高位，门闩口径延续
    assert any("站上年线=True" in x for x in r["详情"])


def test_yearline_below_yearline_is_down_not_up():
    """破年线 → 向下（严格口径比 MA20/MA60 更严）。"""
    r = res.analyze_market_trend_yearline(_index_yearline(n=300, up=False))
    assert r["趋势方向"] == "向下"
    assert r["position"] == "低位"


def test_yearline_returns_same_key_shape():
    """严格口径返回结构对齐默认函数，供 calculate_resonance_score 无缝消费。"""
    a = res.analyze_market_trend(_index_yearline(n=300, up=True))
    b = res.analyze_market_trend_yearline(_index_yearline(n=300, up=True))
    for k in ("趋势方向", "强度", "MA20状态", "MA60状态", "position",
              "近20日涨幅", "详情"):
        assert k in b, k
    assert isinstance(a["趋势方向"], str) and isinstance(b["趋势方向"], str)


# ---------------------------------------------------------------- 指数滞后（建议5）

def test_index_lag_days_computes_and_handles_missing():
    s = _daily(n=30)
    i = _index_yearline(n=30, end="2026-11-03")
    lag = res.index_lag_days(s, i)
    assert lag == 3
    assert res.index_lag_days(s, pd.DataFrame({"收盘价": [1, 2]})) is None
    assert res.index_lag_days(None, i) is None


def test_index_lag_no_downgrade_by_default():
    """默认 index_max_lag_days=None：滞后只诊断、不降级（零变化）。"""
    logic = MysteryLogic()
    d = _daily()
    idx = _index(end="2026-10-20")       # 指数明显滞后
    r = logic.three_resonance_analysis(
        d, market_data={"上证指数": idx}, industry_trend=True)
    assert r["大盘腿降级"] is False
    assert r["指数滞后天数"] is not None and r["指数滞后天数"] > 3
    assert r["大盘趋势"] is True         # 默认口径仍按指数自身判向上


def test_index_lag_downgrade_when_enabled():
    """显式开启且超阈值 → 大盘腿收紧为未知，真三振不成立。"""
    logic = MysteryLogic()
    d = _daily()
    idx = _index(end="2026-10-20")
    r = logic.three_resonance_analysis(
        d, market_data={"上证指数": idx}, industry_trend=True,
        options={"index_max_lag_days": 3})
    assert r["大盘腿降级"] is True
    assert r["大盘趋势"] is False
    assert r["真三振"] is False
    within = logic.three_resonance_analysis(
        d, market_data={"上证指数": _index(end="2026-11-05")},
        industry_trend=True, options={"index_max_lag_days": 3})
    assert within["大盘腿降级"] is False   # lag=1 不触发


# ---------------------------------------------------------------- 端到端口径注入

def test_three_resonance_default_vs_empty_options_identical():
    """不传 options 与传空 options：既有判定字段逐字段一致（零变化）。"""
    logic = MysteryLogic()
    d, idx = _daily(), _index()
    a = logic.three_resonance_analysis(
        d, market_data={"上证指数": idx}, industry_trend=True)
    b = logic.three_resonance_analysis(
        d, market_data={"上证指数": idx}, industry_trend=True, options={})
    assert a == b
    assert a["共振口径"]["大盘口径"] == "ma"


def test_three_resonance_yearline_option_applied():
    """注入 yearline：诊断口径生效，严格腿参与判定。"""
    logic = MysteryLogic()
    r = logic.three_resonance_analysis(
        _daily(), market_data={"上证指数": _index_yearline(n=300, up=True)},
        industry_trend=True, options={"market_mode": "yearline"})
    assert r["共振口径"]["大盘口径"] == "yearline"
    assert r["大盘趋势"] is True          # 严格口径判向上
    assert r["大盘位置"] == "高位"       # 高位 → W71 非高位门闩仍拦真三振


def test_run_mystery_options_injected_and_signal_resonance_same_source():
    """pipeline 注入 options：生效且 signal/resonance 共振分同源（W71 不回退）。"""
    ctx = _ctx(_index())
    daily = _series(_daily())
    bd = run_mystery(daily, None, None, ctx, include_detail=False,
                     resonance_options={"true_threshold": 80.0,
                                       "capital_min_score": 9.0})
    assert bd.signal.get("共振评分") == bd.resonance.get("共振评分")
    assert bd.resonance["共振口径"]["真三振阈值"] == 80.0
    assert bd.resonance["共振口径"]["资金门槛分"] == 9.0


def test_run_mystery_default_score_unchanged():
    """默认路径综合评分与显式空 options 一致，combine 结果不变。"""
    ctx = _ctx(_index())
    daily = _series(_daily())
    a = run_mystery(daily, None, None, ctx, include_detail=False)
    b = run_mystery(daily, None, None, ctx, include_detail=False,
                    resonance_options={})
    sa = combine(a)[0]
    sb = combine(b)[0]
    assert sa == sb
    assert a.resonance["共振评分"] == b.resonance["共振评分"]


# ---------------------------------------------------------------- env 注入链路（service 层）

def test_resonance_options_from_env(monkeypatch):
    """env → dict 搬运；未设置的项不出现，交由 core 回退默认。"""
    for k in ("MYSTERY_RESONANCE_MARKET_MODE", "MYSTERY_RESONANCE_TRUE_THRESHOLD",
              "MYSTERY_RESONANCE_CAPITAL_MIN", "MYSTERY_RESONANCE_CAPITAL_VR",
              "MYSTERY_RESONANCE_INDEX_MAX_LAG_DAYS"):
        monkeypatch.delenv(k, raising=False)
    assert svc.resonance_options_from_env() == {}
    monkeypatch.setenv("MYSTERY_RESONANCE_MARKET_MODE", "yearline")
    monkeypatch.setenv("MYSTERY_RESONANCE_TRUE_THRESHOLD", "80")
    o = svc.resonance_options_from_env()
    assert o == {"market_mode": "yearline", "true_threshold": "80"}
    assert res.resolve_resonance_options(o)["true_threshold"] == 80.0


def test_run_rules_passes_env_options_to_pipeline(monkeypatch):
    """W73：service 注入「生产默认 + 显式 env 覆盖」到 pipeline（core 零 IO）。

    W72 原断言注入稀疏 env dict；W73 起 run_rules 改注入
    resonance_options_effective()，故无 env 时也应带上生产默认两键。
    """
    import mystery.core.pipeline as pipe
    captured = {}

    def fake_run_mystery(daily, weekly, monthly, ctx, **kw):
        captured.update(kw)
        return pipe.MysteryBreakdown()

    monkeypatch.setattr(pipe, "run_mystery", fake_run_mystery)
    for k, _ in svc._RESONANCE_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MYSTERY_RESONANCE_MARKET_MODE", "ma")  # 显式覆盖大盘腿
    s = svc.AnalysisService.__new__(svc.AnalysisService)
    s.logic, s.patterns = MysteryLogic(), None
    s.run_rules(_series(_daily()), None, None, MarketContext(), False)
    opts = captured.get("resonance_options")
    assert opts["market_mode"] == "ma"                 # env 覆盖生效
    assert opts["true_threshold"] == 80.0             # 生产默认仍在
    assert svc.resonance_options_effective() == opts  # 单一事实源


def test_resonance_cache_tag_default_is_nonempty_and_isolated(monkeypatch):
    """W73：默认 tag 恒非空，且与 W71 口径（ma/85）键空间隔离。

    W72 原契约「默认 tag 为空 → 既有缓存零失效」在生产默认改变后会造成
    旧 ma/85 缓存被 yearline/80 命中，故 W73 改为恒非空 tag。
    """
    for k, _ in svc._RESONANCE_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    t = svc.resonance_cache_tag()
    assert t.startswith('|R[') and 'market_mode=yearline' in t
    assert 'true_threshold=80.0' in t
    # 显式回退到 W71 口径 → tag 不同，不会互相污染
    monkeypatch.setenv("MYSTERY_RESONANCE_MARKET_MODE", "ma")
    monkeypatch.setenv("MYSTERY_RESONANCE_TRUE_THRESHOLD", "85")
    t_w71 = svc.resonance_cache_tag()
    assert t_w71 != t and 'market_mode=ma' in t_w71
    assert 'true_threshold=85.0' in t_w71


def test_resonance_cache_tag_stable_and_distinct(monkeypatch):
    """口径入缓存键：不同口径不同 tag，等价写法归一到同一 tag。"""
    for k, _ in svc._RESONANCE_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MYSTERY_RESONANCE_MARKET_MODE", "yearline")
    monkeypatch.setenv("MYSTERY_RESONANCE_TRUE_THRESHOLD", "80")
    t1 = svc.resonance_cache_tag()
    assert t1.startswith('|R[') and 'market_mode=yearline' in t1
    assert 'true_threshold=80.0' in t1
    # '80' 与 '80.0' 归一化后同一 tag（避免无谓 miss）
    monkeypatch.setenv("MYSTERY_RESONANCE_TRUE_THRESHOLD", "80.0")
    assert svc.resonance_cache_tag() == t1
    # 换成 ma 口径 → tag 不同，缓存键空间隔离，不会互相污染
    monkeypatch.setenv("MYSTERY_RESONANCE_MARKET_MODE", "ma")
    t2 = svc.resonance_cache_tag()
    assert t2 != t1 and 'market_mode=ma' in t2
    # 非法值回退默认，仍与显式默认写法一致
    monkeypatch.setenv("MYSTERY_RESONANCE_MARKET_MODE", "BOGUS")
    assert svc.resonance_cache_tag() == t2


# ---------------------------------------------------------------- 三级共振可过滤（建议7）

def _scan_row(symbol, three=None):
    d = {"symbol": symbol, "mystery": {}}
    if three is not None:
        d["mystery"]["resonance"] = {"三级共振": three}
    return d


def test_filter_three_resonance_readonly_exposure():
    rows = [_scan_row("a", True), _scan_row("b", False),
            _scan_row("c"), _scan_row("d", None)]
    got = [r["symbol"] for r in filter_by_signal(rows, "three_resonance")]
    assert got == ["a"]                       # 缺字段/None/unknown 不当 True
    assert len(filter_by_signal(rows, "bogus")) == 4  # 未知名仍返回全量
    assert len(filter_by_signal(rows, "true_resonance")) == 0  # 既有信号不受影响


def test_filter_three_resonance_top_level_key_compat():
    rows = [{"symbol": "x", "three_resonance": True, "mystery": {}},
            {"symbol": "y", "mystery": {}}]
    assert [r["symbol"] for r in filter_by_signal(rows, "three_resonance")] == ["x"]


def test_classify_contract_unchanged():
    """建议7 走只读分支，不新增 classify 返回键（保护既有精确断言）。"""
    out = classify({"mystery": {"vap_atr": {}, "platform": {}}})
    assert "three_resonance" not in out
    assert out["true_resonance"] is False and out["labels"] == []


# ---------------------------------------------------------------- W71 门闩回归

def test_w71_high_market_position_still_vetoed():
    """W71 高位硬门闩在参数化后依然生效，不被开关绕过。"""
    r = res.calculate_resonance_score(*_four_ok(position="高位"))
    assert r["score"] == 85.0
    assert r["is_true_three_strike"] is False
    assert r["gates"]["大盘非高位"] is False
    assert "大盘非高位" in r["reject_reasons"]


def test_w71_gates_all_present_on_positive():
    r = res.calculate_resonance_score(*_four_ok())
    assert r["is_true_three_strike"] is True
    assert r["reject_reasons"] == []
    assert all(r["gates"].values())
    assert set(r["gates"]) == {
        "共振评分≥85", "资金活跃", "大盘向上", "行业向上",
        "个股严格腿", "大盘非高位"}
