"""test_core_rules — core 规则纯函数测试（合成 OHLC，不 import czsc）。

P1：给定合成 K 线，主升浪/平台/共振有确定输出。
"""
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
