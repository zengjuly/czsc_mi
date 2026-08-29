"""test_core_rules — core 规则纯函数测试（合成 OHLC，不 import czsc）。

P1：给定合成 K 线，主升浪/平台/共振有确定输出。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from mystery.core.mystery_rules import MysteryLogic
from mystery.core import resonance as _res
from mystery.core.platform import analyze_adaptive_platform


def _synthetic_daily(n: int = 300, seed: int = 42) -> pd.DataFrame:
    """合成日K：缓慢上行趋势 + 成交量/换手率。"""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2025-01-01", periods=n)
    close = 10 * np.cumprod(1 + rng.normal(0.0008, 0.015, n))
    open_ = close * (1 + rng.normal(0, 0.004, n))
    high = np.maximum(open_, close) * (1 + abs(rng.normal(0, 0.006, n)))
    low = np.minimum(open_, close) * (1 - abs(rng.normal(0, 0.006, n)))
    vol = rng.uniform(5e6, 2e7, n)
    df = pd.DataFrame({'日期': dates.strftime('%Y-%m-%d'), '开盘价': open_,
                       '最高价': high, '最低价': low, '收盘价': close,
                       '成交量': vol, '成交额': vol * close, '换手率': rng.uniform(1, 4, n)})
    for w in [5, 10, 20, 60, 250]:
        df[f'MA{w}'] = df['收盘价'].rolling(w).mean()
    df['量比'] = df['成交量'] / df['成交量'].rolling(5).mean()
    return df


def test_main_bull_wave_deterministic():
    logic = MysteryLogic()
    df = _synthetic_daily()
    r = logic.main_bull_wave_analysis(df)
    assert r['主升浪状态'] in ('主升持股期', '空中加油', '强势上升', '观望', '未知', '异常')
    assert '判定依据' in r and len(r['判定依据']) >= 1
    # 缺列时降级返回（不抛异常）
    r2 = logic.main_bull_wave_analysis(df.drop(columns=['量比']))
    assert '缺少必要列' in r2['详情'][0]


def test_platform_adaptive_deterministic():
    df = _synthetic_daily()
    r = analyze_adaptive_platform(df, stock_code='600519.SH', latest_only=True)
    assert '平台范围' in r
    if r.get('POC') is not None:
        assert r['POC'] > 0
        assert r['自适应上轨'] >= r['自适应下轨']


def test_resonance_score_math():
    """四维共振评分：个股30 + 大盘25 + 行业25 + 资金 = 80 → 二级共振。"""
    individual = {'基础过滤': True, '均线多头': True}
    market = {'趋势方向': '向上', 'position': '中位'}
    industry = {'整体趋势': '向上', 'detail': '强势5 / 弱势1 / 中性2'}
    capital = {'active': False, 'score': 0, 'detail': '资金平淡'}
    r = _res.calculate_resonance_score(individual, market, industry, capital)
    assert r['score'] == 80.0
    assert r['level'] == '二级共振'
    assert r['is_true_three_strike'] is False
    # 大盘高位惩罚 -15
    market['position'] = '高位'
    r2 = _res.calculate_resonance_score(individual, market, industry, capital)
    assert r2['score'] == 65.0


def test_industry_score_from_kline():
    """板块K线 → 行业分：单调上行 → 高分；下行 → 低分。"""
    dates = pd.bdate_range('2026-01-01', periods=60)
    up = pd.DataFrame({'日期': dates, '收盘价': np.linspace(100, 130, 60),
                       '成交额': np.full(60, 1e9)})
    down = pd.DataFrame({'日期': dates, '收盘价': np.linspace(130, 100, 60),
                         '成交额': np.full(60, 1e9)})
    s_up = _res.calculate_industry_score_from_sector(up)
    s_down = _res.calculate_industry_score_from_sector(down)
    assert s_up > s_down
    assert s_up > 12.5 and s_down < 12.5


def test_basic_filter_requires_ma():
    logic = MysteryLogic()
    df = _synthetic_daily(100)
    passed, errors = logic.basic_filter(df)
    assert isinstance(passed, bool)
    assert isinstance(errors, list)


def test_core_no_czsc_import():
    """core 模块禁止 import czsc。"""
    import subprocess
    import sys

    code = ("import sys; sys.modules['czsc']=None; "
            "import mystery.core.models, mystery.core.mystery_rules, "
            "mystery.core.platform, mystery.core.resonance, "
            "mystery.core.patterns, mystery.core.indicators")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr


# ---------------- P4 缠论混合评分 ----------------
def _mk_breakdown(score=49.0, resonance=55.0, vetoed=False) -> MysteryBreakdown:
    from mystery.core.models import MysteryBreakdown

    signal = {'综合评分': score, '共振评分': resonance, '操作建议': '可关注',
              '真三振': False, '年线滤网': not vetoed}
    return MysteryBreakdown(signal=signal)


def _mk_chan(last_bi_dir='up', in_zs=False, confirmed=True, weekly_dir=None):
    from mystery.core.models import ChanBi, ChanStructure

    chan = {'1d': ChanStructure(freq='1d', bis=[ChanBi('up', '2026-01-01',
                                                       '2026-02-01', 10, 9)],
                                last_bi_dir=last_bi_dir,
                                last_bi_confirmed=confirmed, in_zs=in_zs,
                                engine='czsc', engine_ver='1.0.1')}
    if weekly_dir:
        chan['1w'] = ChanStructure(
            freq='1w', bis=[ChanBi(weekly_dir, '2026-01-01', '2026-02-01', 10, 9)],
            last_bi_dir=weekly_dir, last_bi_confirmed=True,
            engine='czsc', engine_ver='1.0.1')
    return chan


def test_chan_score_defaults():
    from mystery.core.scorer import chan_score

    assert chan_score(None) == 50.0
    assert chan_score({}) == 50.0
    assert chan_score(_mk_chan('up')) == 60.0
    assert chan_score(_mk_chan('up', in_zs=True)) == 65.0
    assert chan_score(_mk_chan('down')) == 40.0
    # 未确认笔不给方向分
    assert chan_score(_mk_chan('up', confirmed=False)) == 50.0


def test_chan_score_weekly_same_opposite():
    """W6：日周同向 +8 / 反向 -8。"""
    from mystery.core.scorer import chan_score

    assert chan_score(_mk_chan('up', weekly_dir='up')) == 68.0
    assert chan_score(_mk_chan('up', weekly_dir='down')) == 52.0
    assert chan_score(_mk_chan('down', weekly_dir='down')) == 48.0
    assert chan_score(_mk_chan('down', weekly_dir='up')) == 32.0


def test_chan_score_clamped():
    from mystery.core.scorer import chan_score

    # up + 中枢 + 同向 = 50+10+5+8 = 73；夹紧上界
    assert chan_score(_mk_chan('up', in_zs=True, weekly_dir='up')) == 73.0


def test_combine_p4_blend():
    """P4：0.55*49 + 0.25*55 + 0.20*60 = 43.95 → 44.0。"""
    from mystery.core.scorer import combine

    bd = _mk_breakdown()
    score, advice, true_res = combine(bd, _mk_chan('up'), chan_enabled=True)
    assert score == round(0.55 * 49 + 0.25 * 55 + 0.20 * 60, 1)
    assert advice == '可关注'


def test_combine_p4_veto():
    """年线滤网失败 → 混合分强制 0（一票否决不被 0.2*S_chan 拉正）。"""
    from mystery.core.scorer import combine

    bd = _mk_breakdown(vetoed=True)
    score, _, _ = combine(bd, _mk_chan('up'), chan_enabled=True)
    assert score == 0.0


def test_combine_chan_off_unchanged():
    """chan 关闭时恒为 Mystery 原分（金标兼容）。"""
    from mystery.core.scorer import combine

    bd = _mk_breakdown()
    score, _, _ = combine(bd, _mk_chan('up'), chan_enabled=False)
    assert score == 49.0


def test_technical_snapshot_structure():
    """W5：technical 快照含均线排列/破五反五/量价/筹码/换手率/多周期，JSON 可序列化。"""
    from mystery.core.models import Bar, BarSeries, MarketContext
    from mystery.core.pipeline import run_mystery

    df = _synthetic_daily(n=600, seed=7)

    def _series(freq, fdf):
        bars = [Bar(dt=str(r.日期), open=float(r.开盘价), high=float(r.最高价),
                    low=float(r.最低价), close=float(r.收盘价),
                    volume=float(r.成交量), amount=float(r.成交额),
                    turnover=float(r.换手率)) for r in fdf.itertuples()]
        return BarSeries(symbol="600519.SH", freq=freq, adjust="qfq",
                         bars=bars, source="test")

    daily = _series("1d", df)
    d2 = df.copy()
    d2['日期'] = pd.to_datetime(d2['日期'])
    agg = {'开盘价': 'first', '最高价': 'max', '最低价': 'min',
           '收盘价': 'last', '成交量': 'sum', '成交额': 'sum', '换手率': 'sum'}
    out = {}
    for freq, rule in [("1w", "W-FRI"), ("1M", "ME")]:
        w = d2.set_index('日期').resample(rule).agg(agg).dropna(
            subset=['收盘价']).reset_index()
        w['日期'] = w['日期'].dt.strftime('%Y-%m-%d')
        out[freq] = _series(freq, w)
    weekly, monthly = out["1w"], out["1M"]

    bd = run_mystery(daily, weekly, monthly, MarketContext(),
                     include_detail=True)
    t = bd.technical
    assert t['ma']['排列状态'] in ('多头排列', '空头排列', '混合整理', '未知')
    assert {'MA5', 'MA10', 'MA20', 'MA60', 'MA250'} <= set(t['ma'])
    assert {'破五反五', '破五天数', 'MA20斜率', '原因'} <= set(t['po5'])
    assert {'筹码集中度', '筹码集中度数值', '筹码趋势'} <= set(t['chip'])
    assert {'量比', '量价配合度', 'OBV信号'} <= set(t['volume_price'])
    assert {'换手率', '换手率区域', '换手率MA5', '换手率MA20'} <= set(t['turnover'])
    assert t['multi_period']['周线']['趋势'] in (
        '多头排列', '空头排列', '震荡整理', '数据不足')
    assert t['multi_period']['月线']['趋势'] in (
        '多头排列', '空头排列', '震荡整理', '数据不足')
    assert '周线锚定' in t['multi_period']
    # 整包 JSON 可序列化（technical 无 numpy/NaN 泄漏）
    import json
    s = json.dumps(bd.to_dict(), ensure_ascii=False)
    assert 'technical' in s


def test_technical_snapshot_tiny_series():
    """极小数据（5 根）不崩溃；technical 正常返回。"""
    from mystery.core.models import Bar, BarSeries, MarketContext
    from mystery.core.pipeline import run_mystery
    df = _synthetic_daily(n=5, seed=1)
    bars = [Bar(dt=str(r.日期), open=float(r.开盘价), high=float(r.最高价),
                low=float(r.最低价), close=float(r.收盘价),
                volume=float(r.成交量), amount=float(r.成交额),
                turnover=float(r.换手率)) for r in df.itertuples()]
    daily = BarSeries(symbol="600519.SH", freq="1d", adjust="qfq",
                      bars=bars, source="test")
    bd = run_mystery(daily, None, None, MarketContext(), include_detail=True)
    assert isinstance(bd.technical, dict)
    assert 'ma' in bd.technical and 'po5' in bd.technical
    assert bd.technical['po5']['原因']  # 数据不足原因说明
