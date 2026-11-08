"""W73 — 生产默认口径「严格大盘腿 + 降阈值」回归测试。

承接 W70 审计建议 5/7 的最后一步：W72 完成参数化与金标对拍但保留待拍板，
W73 把生产默认从 W71 基线（ma / 85）切换为（yearline / 80）。

分层契约（本次改动的核心）：
- core.DEFAULT_RESONANCE_OPTIONS 保持 W71 库级/离线基线 ma/85 不变，
  保护 W71 27 项 + W72 26 项既有金标测试与离线回放的可比性；
- services.analyze.PRODUCTION_RESONANCE_OPTIONS 定义生产默认，
  resonance_options_effective() = 生产默认 + 显式 env 覆盖，
  Web / CLI / scan / 日报全链路经 run_rules 注入生效；
- resonance_cache_tag() 恒非空：旧 ma/85 缓存（无 |R[...] 后缀）
  不会被 yearline/80 命中，杜绝切换默认后的口径污染。

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
from mystery.core.scorer import combine
from mystery.services import analyze as svc


@pytest.fixture(autouse=True)
def _isolate_runtime_overrides(tmp_path, monkeypatch):
    """隔离生产网页覆盖层：本文件验证代码层生产默认（yearline/80）。

    生产上网页把 true_threshold 调成 70 属合法运行时配置，不应让单元测试
    误判为回归；覆盖层指向空临时路径 + 清空显式 env 后恢复默认口径。
    """
    monkeypatch.setattr(svc, "RESONANCE_OVERRIDE_PATH",
                        tmp_path / "resonance_override.json")
    for env_key, _ in svc._RESONANCE_ENV_KEYS:
        monkeypatch.delenv(env_key, raising=False)


# ---------------------------------------------------------------- 构造工具

def _daily(n: int = 300, seed: int = 7) -> pd.DataFrame:
    """合成日K：稳定上行，含全部指标列 + 末根放量。"""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(end="2026-11-06", periods=n)
    close = 10 * np.cumprod(1 + rng.normal(0.0016, 0.010, n))
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
    df["均线排列"] = 1
    df["量比"] = df["成交量"] / df["成交量"].rolling(5, min_periods=1).mean()
    return df


def _series(df: pd.DataFrame, symbol: str = "600519.SH") -> BarSeries:
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
    return BarSeries(symbol=symbol, freq="1d", adjust="qfq",
                     bars=bars, source="test")


def _index(n: int = 160, up: bool = True) -> pd.DataFrame:
    """V 型指数：ma 口径判「向上 + 中位」；<250 根时 yearline 判「未知」。"""
    dates = pd.bdate_range(end="2026-11-06", periods=n)
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


def _index_yearline(n: int = 300, up: bool = True) -> pd.DataFrame:
    """≥250 根严格口径数据：站年线 + 多头顺次排列 / 破年线。"""
    dates = pd.bdate_range(end="2026-11-06", periods=n)
    close = np.linspace(3000, 3600, n) if up else np.linspace(3600, 2900, n)
    return pd.DataFrame({"日期": dates.strftime("%Y-%m-%d"), "收盘价": close})


def _four_ok(capital_score: float = 20.0, position: str = "中位"):
    """四维全过入参（个股/大盘/行业/资金）。"""
    return ({"基础过滤": True, "均线多头": True},
            {"趋势方向": "向上", "position": position},
            {"整体趋势": "向上", "detail": "强势5/弱势1"},
            {"active": True, "score": capital_score, "detail": "量比2.0"})


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """每个用例都从「无显式 env」的干净生产默认开始。"""
    for k, _ in svc._RESONANCE_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)


# ---------------------------------------------------------------- 生产默认常量

def test_production_defaults_are_strict_market_and_80():
    """生产默认 = 严格大盘腿 yearline + 阈值 80，其余沿用 core 基线。"""
    d = svc.PRODUCTION_RESONANCE_OPTIONS
    assert d["market_mode"] == "yearline"
    assert d["true_threshold"] == 80.0
    resolved = res.resolve_resonance_options(d)
    assert resolved["capital_min_score"] == 8.0
    assert resolved["capital_min_volume_ratio"] == 1.5
    assert resolved["index_max_lag_days"] is None


def test_core_baseline_still_w71_ma_85():
    """core 库级基线不变：离线单测/金标回放仍以 W71 ma/85 为基准。"""
    o = res.resolve_resonance_options(None)
    assert o["market_mode"] == "ma" and o["true_threshold"] == 85.0
    assert res.DEFAULT_RESONANCE_OPTIONS["market_mode"] == "ma"


# ---------------------------------------------------------------- 生效口径

def test_effective_no_env_equals_production_default():
    """无 env → 实际生效口径即生产默认。"""
    assert svc.resonance_options_effective() == dict(
        svc.PRODUCTION_RESONANCE_OPTIONS)
    assert svc.resonance_options_from_env() == {}   # W72 稀疏搬运语义不变


def test_effective_env_overrides_each_key(monkeypatch):
    """env 逐项覆盖生产默认，未覆盖项保持生产默认。"""
    monkeypatch.setenv("MYSTERY_RESONANCE_MARKET_MODE", "ma")
    monkeypatch.setenv("MYSTERY_RESONANCE_CAPITAL_MIN", "9")
    o = svc.resonance_options_effective()
    assert o["market_mode"] == "ma"
    assert o["true_threshold"] == 80.0              # 未被覆盖
    assert o["capital_min_score"] == "9"
    norm = res.resolve_resonance_options(o)
    assert norm["capital_min_score"] == 9.0


def test_effective_can_fully_revert_to_w71(monkeypatch):
    """一键回退：env 显式设 ma/85 即完全等价于 W71 口径。"""
    monkeypatch.setenv("MYSTERY_RESONANCE_MARKET_MODE", "ma")
    monkeypatch.setenv("MYSTERY_RESONANCE_TRUE_THRESHOLD", "85")
    n = res.resolve_resonance_options(svc.resonance_options_effective())
    assert n == res.resolve_resonance_options(None)


def test_effective_invalid_env_falls_back_to_w71_baseline(monkeypatch):
    """脏 env 保守回退：非法 mode 回落到 core 基线 ma，不放宽任何门闩。"""
    monkeypatch.setenv("MYSTERY_RESONANCE_MARKET_MODE", "BOGUS")
    monkeypatch.setenv("MYSTERY_RESONANCE_TRUE_THRESHOLD", "-1")
    n = res.resolve_resonance_options(svc.resonance_options_effective())
    assert n["market_mode"] == "ma"
    assert n["true_threshold"] == 85.0


# ---------------------------------------------------------------- 缓存键隔离（W23）

def test_cache_tag_default_is_nonempty_and_stable():
    """默认 tag 恒非空且稳定：重复调用一致，避免无谓 miss。"""
    t = svc.resonance_cache_tag()
    assert t.startswith('|R[')
    assert 'market_mode=yearline' in t and 'true_threshold=80.0' in t
    assert svc.resonance_cache_tag() == t


def test_cache_tag_isolates_production_default_from_w71(monkeypatch):
    """生产默认与显式回退 W71 各有独立键空间，绝不互相命中。"""
    t_prod = svc.resonance_cache_tag()
    monkeypatch.setenv("MYSTERY_RESONANCE_MARKET_MODE", "ma")
    monkeypatch.setenv("MYSTERY_RESONANCE_TRUE_THRESHOLD", "85")
    t_w71 = svc.resonance_cache_tag()
    assert t_prod != t_w71
    assert 'market_mode=ma' in t_w71 and 'true_threshold=85.0' in t_w71


def test_cache_tag_differs_from_legacy_empty_tag():
    """关键防污染：新 tag 非空，旧 W72 缓存（无后缀）不会被新口径命中。"""
    assert svc.resonance_cache_tag() != ''


# ---------------------------------------------------------------- 端到端注入

def test_run_rules_injects_production_default(monkeypatch):
    """run_rules 无 env 时注入的正是生产默认（Web/CLI/scan 全链路生效）。"""
    import mystery.core.pipeline as pipe
    captured = {}

    def fake_run_mystery(daily, weekly, monthly, ctx, **kw):
        captured.update(kw)
        return pipe.MysteryBreakdown()

    monkeypatch.setattr(pipe, "run_mystery", fake_run_mystery)
    s = svc.AnalysisService.__new__(svc.AnalysisService)
    s.logic, s.patterns = MysteryLogic(), None
    s.run_rules(_series(_daily()), None, None, MarketContext(), False)
    assert captured["resonance_options"] == dict(svc.PRODUCTION_RESONANCE_OPTIONS)
    n = res.resolve_resonance_options(captured["resonance_options"])
    assert n["market_mode"] == "yearline" and n["true_threshold"] == 80.0


def test_production_options_activate_strict_market_leg():
    """口径真的生效：同一 160 根指数，ma 判向上，生产默认 yearline 判未知。

    <250 根时严格口径「数据不足 → 未知 → 绝不放行」，与 ma 口径形成
    明确区分，证明生产默认不是只改了标签而是真正切换了大盘腿实现。
    生产实测指数 916 根（≥250），不受此限制。
    """
    logic = MysteryLogic()
    d, idx = _daily(), _index(n=160)
    prod = svc.resonance_options_effective()
    r_prod = logic.three_resonance_analysis(
        d, market_data={"上证指数": idx}, industry_trend=True, options=prod)
    r_ma = logic.three_resonance_analysis(
        d, market_data={"上证指数": idx}, industry_trend=True,
        options={"market_mode": "ma"})
    assert r_prod["共振口径"]["大盘口径"] == "yearline"
    assert r_prod["共振口径"]["真三振阈值"] == 80.0
    assert r_prod["大盘趋势"] is False           # 严格口径数据不足 → 不放行
    assert r_ma["大盘趋势"] is True              # ma 口径判向上


def test_production_options_yearline_up_on_sufficient_bars():
    """≥250 根且站年线多头排列 → yearline 判向上（生产数据形态）。"""
    logic = MysteryLogic()
    r = logic.three_resonance_analysis(
        _daily(), market_data={"上证指数": _index_yearline(n=300, up=True)},
        industry_trend=True, options=svc.resonance_options_effective())
    assert r["共振口径"]["大盘口径"] == "yearline"
    assert r["大盘趋势"] is True
    assert r["大盘位置"] == "高位"               # 单调上行近120日分位=高位


def test_run_mystery_production_signal_resonance_same_source():
    """pipeline 注入生产默认后 signal/resonance 共振分仍同源（W71 不回退）。"""
    ctx = MarketContext(index_bars=_series(_index(n=160), "000001.SH"),
                        industry_up=True)
    bd = run_mystery(_series(_daily()), None, None, ctx, include_detail=False,
                     resonance_options=svc.resonance_options_effective())
    assert bd.signal.get("共振评分") == bd.resonance.get("共振评分")
    assert bd.resonance["共振口径"]["大盘口径"] == "yearline"
    assert isinstance(combine(bd)[0], (int, float))


# ---------------------------------------------------------------- 阈值 80 语义

def test_threshold_80_boundary():
    """降阈值后 80 分触发、79 分不触发，门闩标签动态渲染为 ≥80。"""
    r80 = res.calculate_resonance_score(*_four_ok(capital_score=0.0),
                                        true_threshold=80.0)
    assert r80["score"] == 80.0 and r80["is_true_three_strike"] is True
    assert "共振评分≥80" in r80["gates"]
    r79 = res.calculate_resonance_score(*_four_ok(capital_score=-1.0),
                                        true_threshold=80.0)
    assert r79["is_true_three_strike"] is False
    assert "共振评分≥80" in r79["reject_reasons"]


def test_lower_threshold_does_not_bypass_high_position_gate():
    """关键风控：降阈值只放宽评分一道，gates 为 all()，高位硬门闩仍否决。"""
    r = res.calculate_resonance_score(*_four_ok(capital_score=20.0,
                                                position="高位"),
                                      true_threshold=80.0)
    assert r["is_true_three_strike"] is False
    assert r["gates"]["大盘非高位"] is False
    assert "大盘非高位" in r["reject_reasons"]


def test_lower_threshold_does_not_bypass_capital_gate():
    """资金不活跃时即便高分也不触发（六道门闩逐道仍生效）。"""
    ind, mkt, indu, cap = _four_ok(capital_score=20.0)
    cap = {"active": False, "score": 20.0, "detail": "量比不足"}
    r = res.calculate_resonance_score(ind, mkt, indu, cap,
                                      true_threshold=80.0)
    assert r["is_true_three_strike"] is False
    assert "资金活跃" in r["reject_reasons"]
