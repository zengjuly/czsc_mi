"""mystery.core.platform — 震荡平台 / 自适应 VAP-ATR（迁自 adaptive_platform.py）。

纯函数，零 IO。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd


def calculate_adaptive_lookback(data: pd.DataFrame, min_lookup: int = 10,
                                max_lookup: int = 60,
                                turnover_col: Optional[str] = None) -> Dict[str, Any]:
    """换手率自适应周期：theoretical_n = 70/avg_turnover，clip 到 [10,60]。"""
    result = {'adaptive_n': None, 'avg_turnover': None, 'theoretical_n': None,
              'min_lookup': min_lookup, 'max_lookup': max_lookup}
    if data is None or len(data) < 20:
        result['adaptive_n'] = 30
        result['detail'] = '数据不足20日，使用默认周期30'
        return result
    if turnover_col is None:
        for candidate in ['换手率', 'turnover_rate', 'turn']:
            if candidate in data.columns:
                turnover_col = candidate
                break
    if turnover_col is None or turnover_col not in data.columns:
        result['adaptive_n'] = 30
        result['detail'] = f'缺少换手率列({turnover_col})，使用默认周期30'
        return result
    turnover = pd.to_numeric(data[turnover_col], errors='coerce')
    avg_turnover = turnover.tail(20).mean()
    if pd.isna(avg_turnover) or avg_turnover <= 0:
        result['adaptive_n'] = 30
        result['detail'] = '换手率数据异常，使用默认周期30'
        return result
    theoretical_n = 70.0 / avg_turnover
    adaptive_n = int(round(np.clip(theoretical_n, min_lookup, max_lookup)))
    result['adaptive_n'] = adaptive_n
    result['avg_turnover'] = round(float(avg_turnover), 2)
    result['theoretical_n'] = round(float(theoretical_n), 1)
    result['detail'] = (f'换手率自适应周期: 近20日日均换手{avg_turnover:.2f}%, '
                        f'理论N={theoretical_n:.1f}日, 自适应N={adaptive_n}日')
    return result


def calculate_adaptive_vap_atr(data: pd.DataFrame, n: int = 60, atr_m: int = 14,
                               k: float = 1.8, market_type: str = 'MainBoard',
                               latest_only: bool = False) -> pd.DataFrame:
    """VAP-ATR 平台计算：poc + k*matr 上下轨 + 突破信号。"""
    df = data.copy()
    col_close = '收盘价' if '收盘价' in df.columns else 'close'
    col_high = '最高价' if '最高价' in df.columns else 'high'
    col_low = '最低价' if '最低价' in df.columns else 'low'
    col_open = '开盘价' if '开盘价' in df.columns else 'open'
    col_vol = '成交量' if '成交量' in df.columns else 'volume'
    limit_ratio = 0.2 if market_type == 'ChiNext_STAR' else 0.1
    prev_close = df[col_close].shift(1)
    raw_tr = np.maximum(df[col_high] - df[col_low],
                        np.maximum((df[col_high] - prev_close).abs(),
                                   (df[col_low] - prev_close).abs()))
    is_limit_up = df[col_close] >= np.round(prev_close * (1 + limit_ratio), 2)
    ma_tr = raw_tr.rolling(window=atr_m, min_periods=1).mean()
    df['mtr'] = np.where(is_limit_up.fillna(False), ma_tr, raw_tr)
    df['matr'] = df['mtr'].rolling(window=atr_m).mean()
    price_range = df[col_high] - df[col_low]
    price_range = np.where(price_range == 0, 0.001, price_range)
    gravity = (df[col_close] - df[col_low]) / price_range
    df['gravity'] = gravity
    df['p_core'] = df[col_low] + gravity * (df[col_high] - df[col_low])

    def get_cn_poc(window_df: pd.DataFrame) -> float:
        if len(window_df) < n:
            return np.nan
        bins = np.linspace(window_df['p_core'].min(), window_df['p_core'].max(), 50)
        hist, bin_edges = np.histogram(window_df['p_core'], bins=bins,
                                       weights=window_df[col_vol])
        max_idx = np.argmax(hist)
        return float((bin_edges[max_idx] + bin_edges[max_idx + 1]) / 2)

    if latest_only:
        poc_series = [np.nan] * (len(df) - 1) + \
            [get_cn_poc(df.iloc[-n:]) if len(df) >= n else np.nan]
    else:
        poc_series = []
        for i in range(len(df)):
            if i < n - 1:
                poc_series.append(np.nan)
            else:
                poc_series.append(get_cn_poc(df.iloc[i - n + 1:i + 1]))
    df['poc'] = poc_series
    df['platform_upper'] = df['poc'] + k * df['matr']
    df['platform_lower'] = df['poc'] - k * df['matr']
    is_breakout = ((df[col_close] > df['platform_upper']) & (df[col_close] > df[col_open])
                   & (gravity > 0.5) & ~is_limit_up.shift(1).fillna(False))
    df['is_breakout'] = is_breakout.fillna(False)
    return df


def analyze_adaptive_platform(data: pd.DataFrame, stock_code: str = '',
                              n: Optional[int] = None, atr_m: Optional[int] = None,
                              k: Optional[float] = None,
                              latest_only: bool = True) -> Dict[str, Any]:
    """自适应 VAP-ATR 平台分析（gemmi 优化）。"""
    result = {'平台方式': '自适应VAP-ATR', 'POC': None, '自适应上轨': None,
              '自适应下轨': None, 'ATR': None, '突破信号': False, '平台范围': None,
              '自适应周期': None, '详情': []}
    if data is None or len(data) < 30:
        result['详情'].append('数据不足30日，无法计算自适应平台')
        return result
    digits = ''.join(ch for ch in str(stock_code) if ch.isdigit())[:6]
    market_type = 'ChiNext_STAR' if digits.startswith(('300', '301', '688')) else 'MainBoard'
    adaptive_info = calculate_adaptive_lookback(data)
    adaptive_n = adaptive_info.get('adaptive_n')
    if n is None:
        n = adaptive_n if adaptive_n else 30
    if atr_m is None:
        atr_m = int(np.clip(round(n / 4), 10, 14))
    if k is None:
        avg_turnover = adaptive_info.get('avg_turnover')
        if avg_turnover is not None:
            if avg_turnover >= 10:
                k = 2.2
            elif avg_turnover >= 3:
                k = 1.8
            else:
                k = 1.5
        else:
            k = 1.8
    try:
        df = calculate_adaptive_vap_atr(data, n=n, atr_m=atr_m, k=k,
                                        market_type=market_type,
                                        latest_only=latest_only)
        latest = df.iloc[-1]
        poc = latest.get('poc')
        upper = latest.get('platform_upper')
        lower = latest.get('platform_lower')
        atr_val = latest.get('matr')
        is_brk = bool(latest.get('is_breakout', False))
        result['自适应周期'] = {'adaptive_n': n, 'atr_m': atr_m, 'k': k,
                               'avg_turnover': adaptive_info.get('avg_turnover'),
                               'theoretical_n': adaptive_info.get('theoretical_n')}
        if pd.notna(poc) and pd.notna(upper) and pd.notna(lower):
            result['POC'] = round(float(poc), 2)
            result['自适应上轨'] = round(float(upper), 2)
            result['自适应下轨'] = round(float(lower), 2)
            result['ATR'] = round(float(atr_val), 4) if pd.notna(atr_val) else None
            result['突破信号'] = is_brk
            result['平台范围'] = {'上沿': round(float(upper), 2), '下沿': round(float(lower), 2),
                                  'POC': round(float(poc), 2), '周期': n, '方式': '自适应VAP-ATR'}
            if adaptive_info.get('detail'):
                result['详情'].append(adaptive_info['detail'])
            result['详情'].append(
                f'自适应平台: POC={poc:.2f}, 上轨={upper:.2f}, 下轨={lower:.2f} '
                f'(慢窗口{n}日, 快ATR{atr_m}日, k={k})')
            if is_brk:
                result['详情'].append('✅ 实体突破上轨: 收盘价>上轨 且 阳线 且 重心>0.5')
            else:
                result['详情'].append('未突破自适应上轨（需收盘价>上轨且阳线且重心>0.5）')
        else:
            result['详情'].append(f'POC数据不足（需{n}日以上数据）')
    except Exception as e:
        result['详情'].append(f'自适应平台计算异常: {e}')
    return result


def cns_adaptive_vap_atr(df: pd.DataFrame, n: int = 60, atr_m: int = 14,
                         k: float = 1.8, market_type: str = 'MainBoard') -> pd.DataFrame:
    return calculate_adaptive_vap_atr(df, n=n, atr_m=atr_m, k=k, market_type=market_type)
