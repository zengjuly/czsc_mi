"""W71 — 三振（true_resonance）实现审计问题修复的回归测试。

对应《三振实现审计报告》改进建议 1/2/3/4/6：
- P1-1：核心信号正例零覆盖 → 补 85 边界正例、四维全过、大盘高位否决、
  资金 8/0、缺数据降级、集成路径（真能触发且有区分度）。
- P1-2：大盘高位仍判真三振，与「大盘非高位」文案矛盾 → is_true 加门闩。
- P1-3：signal 与 resonance 双份共振数据分裂 + 重复计算 → 只算一次并同源。
- P2-1：三级共振口径 OR 过宽 + 归因写死 → 严格腿 + 动态归因。
- P3：行业腿 closes/amounts 各自 dropna 错位 → 先对齐再 dropna。

全部离线：合成 OHLC + 纯 dict 入参，不 import czsc、不读写生产库。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from mystery.core import resonance as res
from mystery.core.mystery_rules import MysteryLogic
from mystery.core.models import Bar, BarSeries, MarketContext
from mystery.core.pipeline import run_mystery
from mystery.core.scorer import combine


# ---------------------------------------------------------------- 构造工具

def _daily(n: int = 300, up: bool = True, seed: int = 7) -> pd.DataFrame:
    """合成日K：稳定上行(默认)/下行，含全部指标列 + 末根放量。"""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2025-01-01", periods=n)
    drift = 0.0016 if up else -0.0016
    close = 10 * np.cumprod(1 + rng.normal(drift, 0.010, n))
    open_ = close * (1 + rng.normal(0, 0.003, n))
    high = np.maximum(open_, close) * (1 + abs(rng.normal(0, 0.004, n)))
    low = np.minimum(open_, close) * (1 - abs(rng.normal(0, 0.004, n)))
    vol = rng.uniform(5e6, 9e6, n)
    vol[-1] = vol[-6:-1].mean() * 2.0          # 末根量比≈2 → 资金 +12
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


def _index(n: int = 160, up: bool = True) -> pd.DataFrame:
    """合成指数。

    up=True 用「高位→回落筑底→反转上行但未创新高」的 V 型：
    收盘 > MA20/MA60（趋势向上）+ 近120日位置=中位（非高位），
    恰好同时满足「大盘向上」「大盘非高位」两道门闩。线性上升会被判
    「高位」而被 W71 的高位硬条件拦下，故正例必须显式构造中位。
    up=False 单调下行 → 趋势向下 + 低位（负例）。
    """
    dates = pd.bdate_range("2024-01-01", periods=n)
    if up:
        first = np.linspace(3550, 3500, 40)
        down = np.linspace(3500, 3000, 60)
        up_leg = np.linspace(3005, 3400, n - 100)
        close = np.concatenate([first, down, up_leg])
    else:
        close = np.linspace(3600, 2700, n)
    df = pd.DataFrame({"日期": dates, "收盘价": close})
    df["MA20"] = df["收盘价"].rolling(20, min_periods=1).mean()
    df["MA60"] = df["收盘价"].rolling(60, min_periods=1).mean()
    return df


def _series(df: pd.DataFrame, freq: str = "1d") -> BarSeries:
    """日K/指数通用：缺 OHLCV 列时按收盘价兜底（指数只有 收盘价/MA）。"""
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
    return BarSeries(symbol="600519.SH", freq=freq, adjust="qfq",
                     bars=bars, source="test")


def _ctx(index_up: bool = True, industry: bool = True) -> MarketContext:
    return MarketContext(index_bars=_series(_index(up=index_up), "1d"),
                         industry_up=industry)


# ---------------------------------------------------------------- P1-1 正例

def _four_ok(capital_score=20.0, capital_active=True, position="中位"):
    return ({"基础过滤": True, "均线多头": True},
            {"趋势方向": "向上", "position": position},
            {"整体趋势": "向上", "detail": "强势5/弱势1"},
            {"active": capital_active, "score": capital_score, "detail": "量比2.0"})


def test_true_strike_positive_full_mark():
    """四维全过 + 资金 20 + 大盘中位 → 100 分 → 真三振 True（正例零覆盖补齐）。"""
    r = res.calculate_resonance_score(*_four_ok())
    assert r["score"] == 100.0
    assert r["is_true_three_strike"] is True
    assert r["level"] == "真三振（三级）"
    assert r["reject_reasons"] == []
    assert all(r["gates"].values())


def test_true_strike_85_boundary():
    """85 分边界：资金 5 → 85 恰好触发；84.9 不触发。"""
    r = res.calculate_resonance_score(*_four_ok(capital_score=5.0))
    assert r["score"] == 85.0 and r["is_true_three_strike"] is True
    r2 = res.calculate_resonance_score(*_four_ok(capital_score=4.9))
    assert round(r2["score"], 1) == 84.9 and r2["is_true_three_strike"] is False
    assert "共振评分≥85" in r2["reject_reasons"]


def test_true_strike_capital_8_and_0():
    """资金 8（量比1.6档）→88 触发；资金 0 →80 不触发（隐含门槛回归）。"""
    assert res.calculate_resonance_score(*_four_ok(8.0))["is_true_three_strike"] is True
    r0 = res.calculate_resonance_score(*_four_ok(0.0, capital_active=False))
    assert r0["score"] == 80.0 and r0["is_true_three_strike"] is False
    assert "共振评分≥85" in r0["reject_reasons"] and "资金活跃" in r0["reject_reasons"]


# ---------------------------------------------------------------- P1-2 高位

def test_high_position_blocks_true_strike():
    """P1-2：大盘高位 + 资金满分 → score 85，但不得判真三振（契约一致）。"""
    r = res.calculate_resonance_score(*_four_ok(position="高位"))
    assert r["score"] == 85.0                     # -15 软惩罚仍在
    assert r["is_true_three_strike"] is False    # 硬条件否决
    assert "大盘非高位" in r["reject_reasons"]
    assert r["gates"]["大盘非高位"] is False


def test_high_position_score_penalty_kept():
    """高位仍扣 15 分，级别按分数走（不因否决而伪造成 0 分）。"""
    base = res.calculate_resonance_score(*_four_ok(position="中位"))
    high = res.calculate_resonance_score(*_four_ok(position="高位"))
    assert base["score"] - high["score"] == 15.0


# ---------------------------------------------------------------- 缺数据降级

@pytest.mark.parametrize("which", ["market_down", "market_unknown", "industry_unknown",
                                   "industry_down", "stock_fail"])
def test_missing_or_negative_leg_never_true(which):
    """unknown / 反向腿一律不判 True，且不会被别的维度补偿。"""
    ind, mkt, industry, cap = _four_ok()
    if which == "market_down":
        mkt = {"趋势方向": "向下", "position": "中位"}
    elif which == "market_unknown":
        mkt = {"趋势方向": "未知", "position": "未知"}
    elif which == "industry_unknown":
        industry = {"整体趋势": "未知"}
    elif which == "industry_down":
        industry = {"整体趋势": "向下"}
    elif which == "stock_fail":
        ind = {"基础过滤": False, "均线多头": True}
    r = res.calculate_resonance_score(ind, mkt, industry, cap)
    assert r["is_true_three_strike"] is False
    assert r["reject_reasons"], "必须给出真实未过门闩"


def test_capital_none_no_crash():
    r = res.calculate_resonance_score(*_four_ok()[:3], capital_result=None)
    assert r["score"] == 80.0 and r["is_true_three_strike"] is False


def test_empty_df_degrades():
    """空/极短板块 K 线 → 中性 12.5，不抛异常。"""
    assert res.calculate_industry_score_from_sector(pd.DataFrame()) == 12.5
    assert res.calculate_industry_score_from_sector(None) == 12.5


# ---------------------------------------------------------------- P3 行业腿对齐

def test_industry_aligned_dropna_no_drift():
    """P3：收盘价含 NaN 时，成交额必须按同一行丢弃，行业分不发生错位漂移。"""
    n = 60
    close = np.linspace(100, 130, n)
    amount = np.full(n, 1e9)
    clean = pd.DataFrame({"收盘价": close, "成交额": amount})
    dirty = clean.copy()
    dirty.loc[n - 3, "收盘价"] = np.nan     # 制造单列 NaN
    s_clean = res.calculate_industry_score_from_sector(clean)
    s_dirty = res.calculate_industry_score_from_sector(dirty)
    # 对齐后 dirty 只少一行，bias/量能仍取同一批交易日 → 分数应稳定
    assert abs(s_clean - s_dirty) < 0.6, (s_clean, s_dirty)


def test_industry_amount_nan_aligned():
    """成交额含 NaN 时同样按行对齐，不产生序列错位。"""
    n = 60
    df = pd.DataFrame({"收盘价": np.linspace(100, 130, n),
                       "成交额": np.full(n, 1e9)})
    df.loc[10, "成交额"] = np.nan
    s = res.calculate_industry_score_from_sector(df)
    assert 0 <= s <= 25


def test_industry_no_amount_column():
    """无成交额列时按 0 处理，不崩溃（旧兼容）。"""
    df = pd.DataFrame({"收盘价": np.linspace(100, 130, 60)})
    assert 0 <= res.calculate_industry_score_from_sector(df) <= 25


# ---------------------------------------------------------------- P2-1 三级共振

def test_three_level_uses_strict_stock_leg(monkeypatch):
    """P2-1：基础过滤未过、仅收盘>MA20 命中宽松 OR 时，三级共振不得成立。"""
    logic = MysteryLogic()
    df = _daily()
    monkeypatch.setattr(logic, "basic_filter", lambda d: (False, ["mock"]))
    # 让个股趋势走 OR 宽松口径（均线排列==1）
    r = logic.three_resonance_analysis(df, market_data={"上证指数": _index()},
                                       industry_trend=True)
    assert r["个股趋势"] is True               # 展示口径保持宽松
    assert r["个股严格腿"] is False            # 严格腿未过
    assert r["三级共振"] is False


def test_three_level_true_with_strict_leg():
    """基础过滤通过 + 三腿同向 → 三级共振 True（严格口径正例）。"""
    logic = MysteryLogic()
    r = logic.three_resonance_analysis(_daily(), market_data={"上证指数": _index()},
                                       industry_trend=True)
    assert r["个股严格腿"] is True
    assert r["三级共振"] is True


def test_attribution_not_hardcoded():
    """P2-1：三级共振成立但未达真三振时，归因应是真实未过门闩，不再是写死的资金/位置。"""
    logic = MysteryLogic()
    # 资金不活跃（成交量平稳，无放量）→ 三级共振仍可成立，真三振不成立
    df = _daily()
    df["成交量"] = np.full(len(df), 1e7)
    df["成交额"] = df["成交量"] * df["收盘价"]
    r = logic.three_resonance_analysis(df, market_data={"上证指数": _index()},
                                       industry_trend=True)
    tail = " ".join(r["详情"][-3:])
    if r["三级共振"] and not r["真三振"]:
        assert "资金/位置条件不足" not in tail
        assert "未达真三振" in tail


# ---------------------------------------------------------------- 集成路径能触发

def test_integration_true_strike_can_trigger():
    """P1-1 核心：强上涨个股 + 大盘向上 + 行业向上 + 放量 → 集成链路真三振能触发。"""
    logic = MysteryLogic()
    r = logic.three_resonance_analysis(_daily(), market_data={"上证指数": _index()},
                                       industry_trend=True)
    assert r["真三振"] is True, r["详情"]
    assert r["共振评分"] >= 85


def test_integration_has_discrimination():
    """同样个股，大盘向下 / 行业走弱时真三振不成立（信号有区分度）。"""
    logic = MysteryLogic()
    ok = logic.three_resonance_analysis(_daily(), market_data={"上证指数": _index()},
                                        industry_trend=True)
    bad_mkt = logic.three_resonance_analysis(
        _daily(), market_data={"上证指数": _index(up=False)}, industry_trend=True)
    bad_ind = logic.three_resonance_analysis(
        _daily(), market_data={"上证指数": _index()}, industry_trend=False)
    assert ok["真三振"] is True
    assert bad_mkt["真三振"] is False and bad_ind["真三振"] is False
    # 两种负例各断一条 25 分腿（大盘 / 行业），分数恰好相等；
    # 区分度体现在真三振布尔与「正例显著高于负例」，不强求两个负例互有高低。
    assert ok["共振评分"] > bad_mkt["共振评分"]
    assert ok["共振评分"] > bad_ind["共振评分"]
    assert bad_mkt["共振评分"] <= 75 and bad_ind["共振评分"] <= 75


# ---------------------------------------------------------------- P1-3 同源/去重

def test_comprehensive_reuses_passed_resonance(monkeypatch):
    """P1-3：传入 resonance 时不再重复调用 three_resonance_analysis。"""
    logic = MysteryLogic()
    calls = []
    real = logic.three_resonance_analysis

    def spy(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(logic, "three_resonance_analysis", spy)
    pre = {"共振评分": 55.0, "共振级别": "一级共振", "真三振": False,
           "共振建议": "观望", "资金活跃": False, "最强板块": [], "详情": ["x"]}
    out = logic.comprehensive_signal_analysis(
        _daily(), weekly_data=None, market_data=None, resonance=pre)
    assert calls == [], "传入 resonance 后不应重复计算"
    assert out["共振评分"] == 55.0 and out["共振级别"] == "一级共振"


def test_early_exit_signal_matches_resonance(monkeypatch):
    """P1-3：年线否决早退时 signal 共振字段与传入 resonance 同源（不再硬编码 0）。"""
    logic = MysteryLogic()
    monkeypatch.setattr(logic, "basic_filter", lambda d: (False, ["mock veto"]))
    pre = {"共振评分": 37.0, "共振级别": "一级共振", "真三振": False,
           "共振建议": "观望为主", "资金活跃": True, "最强板块": ["银行"],
           "详情": ["d1"]}
    out = logic.comprehensive_signal_analysis(_daily(), resonance=pre)
    assert out["综合评分"] == 0.0               # 年线否决不被复活
    assert out["真三振"] is False
    assert out["共振评分"] == 37.0              # 与 resonance 同源
    assert out["共振级别"] == "一级共振"
    assert out["资金活跃"] is True and out["最强板块"] == ["银行"]


def test_early_exit_true_strike_never_revived(monkeypatch):
    """即使外部传入真三振 True，年线否决早退也不得复活（一票否决铁律）。"""
    logic = MysteryLogic()
    monkeypatch.setattr(logic, "basic_filter", lambda d: (False, ["mock"]))
    pre = {"共振评分": 100.0, "共振级别": "真三振（三级）", "真三振": True,
           "共振建议": "关注", "资金活跃": True, "最强板块": [], "详情": []}
    out = logic.comprehensive_signal_analysis(_daily(), resonance=pre)
    assert out["真三振"] is False and out["综合评分"] == 0.0


def test_run_mystery_computes_resonance_once(monkeypatch):
    """P1-3：run_mystery 里 three_resonance_analysis 只调用一次，且 signal/resonance 同源。"""
    import mystery.core.mystery_rules as mr
    calls = []
    real = mr.MysteryLogic.three_resonance_analysis

    def spy(self, *a, **k):
        calls.append(1)
        return real(self, *a, **k)

    monkeypatch.setattr(mr.MysteryLogic, "three_resonance_analysis", spy)
    bd = run_mystery(_series(_daily()), None, None, _ctx(), include_detail=False)
    assert len(calls) == 1, f"应只算一次，实际 {len(calls)} 次"
    assert bd.signal["共振评分"] == bd.resonance["共振评分"]
    assert bd.signal["共振级别"] == bd.resonance["共振级别"]


def test_run_mystery_true_resonance_end_to_end():
    """端到端：run_mystery → combine → true_resonance 能被真实上下文触发。"""
    bd = run_mystery(_series(_daily()), None, None, _ctx(), include_detail=False)
    _, _, true_res = combine(bd, None, mix_enabled=False)
    assert true_res is True
    assert bd.resonance["真三振"] is True


# ---------------------------------------------------------------- 综合评分不变

def test_scorer_score_formula_unchanged():
    """修复不改变综合评分公式：score = 共振分*0.6 + 主升浪40*0.4。"""
    logic = MysteryLogic()
    df = _daily()
    r = logic.three_resonance_analysis(df, market_data={"上证指数": _index()},
                                       industry_trend=True)
    out = logic.comprehensive_signal_analysis(
        df, weekly_data=None, market_data={"上证指数": _index()},
        industry_trend=True, resonance=r)
    mw = logic.main_bull_wave_signal(df, None)
    expect = round(r["共振评分"] * 0.6
                   + (40 if mw["主升浪信号"] else 0) * 0.4, 1)
    assert out["综合评分"] == expect


def test_scorer_veto_not_revived_in_mix(monkeypatch):
    """混合分模式下年线否决仍强 0，不因共振字段同源被拉正。"""
    from mystery.core.models import MysteryBreakdown
    bd = MysteryBreakdown(signal={"综合评分": 0.0, "共振评分": 70.0,
                                  "年线滤网": False, "真三振": False,
                                  "操作建议": "观望（未通过年线滤网）"})
    score, _, tr = combine(bd, {"1d": None}, mix_enabled=True)
    assert score == 0.0 and tr is False
