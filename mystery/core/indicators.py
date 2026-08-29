"""mystery.core.indicators — 技术指标纯函数（迁自 stock_analyzer/indicators/*）。

零 IO。列名与旧仓一致（中文列：收盘价/成交量/换手率…），保证数学完全一致。
main.py 的加工链由 enrich_indicators() 一次性复刻。

性能说明（2026-08-29 修复）：pandas 3.0 默认 Arrow 后端下，逐行 .iloc[i]['col']
访问极慢（单票 enrich 曾 >80s）。热点函数已改为 numpy 数组向量化/数组循环，
数学结果与旧实现逐元素一致（有 /tmp/indicators_old.py 对照验证）。
"""
from __future__ import annotations

from typing import List, Optional

import numpy as np
import pandas as pd

# ---------------- 均线 ----------------

def calculate_ma(data: pd.DataFrame, periods: Optional[List[int]] = None) -> pd.DataFrame:
    """计算均线（默认 [5,10,20,60,250,377,610]，docs/081601.md）"""
    if periods is None:
        periods = [5, 10, 20, 60, 250, 377, 610]
    result = data.copy()
    for period in periods:
        result[f'MA{period}'] = result['收盘价'].rolling(window=period).mean()
    return result


def calculate_ema(data: pd.DataFrame, periods: Optional[List[int]] = None) -> pd.DataFrame:
    if periods is None:
        periods = [20]
    result = data.copy()
    close_col = '收盘价' if '收盘价' in result.columns else 'close'
    for p in periods:
        result[f'EMA{p}'] = result[close_col].ewm(span=p, adjust=False).mean()
    return result


def calculate_ma_slope(data: pd.DataFrame, period: int = 5, slope_period: int = 5) -> pd.DataFrame:
    result = data.copy()
    ma_col = f'MA{period}'
    if ma_col in result.columns:
        result[f'{ma_col}_斜率'] = result[ma_col].diff(periods=slope_period) / slope_period
    return result


def calculate_ma_arrangement(data: pd.DataFrame) -> pd.DataFrame:
    """均线排列：1 多头 / 0 混合 / -1 空头；缺列返回原样。"""
    result = data.copy()
    ma_periods = [5, 10, 20, 60]
    ma_cols = [f'MA{p}' for p in ma_periods]
    if any(c not in result.columns for c in ma_cols):
        return result
    ma = [result[c].to_numpy(dtype=float) for c in ma_cols]
    n = len(result)
    arrangement = np.full(n, np.nan)
    for i in range(n):
        valid_values = [ma[j][i] for j in range(4) if not np.isnan(ma[j][i])]
        if len(valid_values) >= 3:
            is_bullish = all(valid_values[j] > valid_values[j + 1]
                             for j in range(len(valid_values) - 1))
            is_bearish = all(valid_values[j] < valid_values[j + 1]
                             for j in range(len(valid_values) - 1))
            if is_bullish:
                arrangement[i] = 1
            elif is_bearish:
                arrangement[i] = -1
            else:
                arrangement[i] = 0
    result['均线排列'] = arrangement
    # 排列强度（多头排列的均线对数）：仅均线排列==1 时统计
    order = np.column_stack(ma)
    valid_pair = np.isfinite(order[:, :-1]) & np.isfinite(order[:, 1:])
    strength_full = np.sum(valid_pair & (order[:, :-1] > order[:, 1:]), axis=1)
    result['多头排列强度'] = np.where(
        arrangement == 1, strength_full,
        np.where(np.isnan(arrangement), np.nan, 0.0))
    return result


def analyze_ma_signals(data: pd.DataFrame) -> pd.DataFrame:
    """均线金叉/死叉信号 + 突破MA20信号。"""
    result = data.copy()
    required_cols = ['收盘价', 'MA5', 'MA10', 'MA20', 'MA60', 'MA250']
    if any(c not in result.columns for c in required_cols):
        return result
    ma5 = result['MA5'].to_numpy(dtype=float)
    ma10 = result['MA10'].to_numpy(dtype=float)
    close = result['收盘价'].to_numpy(dtype=float)
    ma20 = result['MA20'].to_numpy(dtype=float)
    n = len(result)

    prev_ok = np.isfinite(ma5[:-1]) & np.isfinite(ma10[:-1])
    cur_ok = np.isfinite(ma5[1:]) & np.isfinite(ma10[1:])
    gold = prev_ok & cur_ok & (ma5[:-1] <= ma10[:-1]) & (ma5[1:] > ma10[1:])
    dead = prev_ok & cur_ok & (ma5[:-1] >= ma10[:-1]) & (ma5[1:] < ma10[1:])
    sig = np.zeros(n, dtype=np.int64)
    sig[1:] = gold.astype(np.int64) - dead.astype(np.int64)
    result['均线信号'] = sig

    prev_ok2 = np.isfinite(close[:-1]) & np.isfinite(ma20[:-1])
    cur_ok2 = np.isfinite(close[1:]) & np.isfinite(ma20[1:])
    up = prev_ok2 & cur_ok2 & (close[:-1] <= ma20[:-1]) & (close[1:] > ma20[1:])
    down = prev_ok2 & cur_ok2 & (close[:-1] >= ma20[:-1]) & (close[1:] < ma20[1:])
    brk = np.zeros(n, dtype=np.int64)
    brk[1:] = up.astype(np.int64) - down.astype(np.int64)
    result['突破信号'] = brk
    return result


# ---------------- 趋势（MACD / RSI） ----------------

def calculate_macd(data: pd.DataFrame, fast_period: int = 12, slow_period: int = 26,
                   signal_period: int = 9) -> pd.DataFrame:
    result = data.copy()
    result['EMA12'] = result['收盘价'].ewm(span=fast_period, adjust=False).mean()
    result['EMA26'] = result['收盘价'].ewm(span=slow_period, adjust=False).mean()
    result['MACD'] = result['EMA12'] - result['EMA26']
    result['MACD_Signal'] = result['MACD'].ewm(span=signal_period, adjust=False).mean()
    result['MACD_Histogram'] = result['MACD'] - result['MACD_Signal']
    result['MACD_Area'] = result['MACD'].cumsum()
    result = result.drop(['EMA12', 'EMA26'], axis=1)
    return result


def analyze_macd_signals(data: pd.DataFrame) -> pd.DataFrame:
    result = data.copy()
    required_cols = ['MACD', 'MACD_Signal', 'MACD_Histogram']
    if any(c not in result.columns for c in required_cols):
        return result
    macd = result['MACD'].to_numpy(dtype=float)
    sig_line = result['MACD_Signal'].to_numpy(dtype=float)
    hist = result['MACD_Histogram'].to_numpy(dtype=float)
    n = len(result)

    prev_ok = np.isfinite(macd[:-1]) & np.isfinite(sig_line[:-1])
    cur_ok = np.isfinite(macd[1:]) & np.isfinite(sig_line[1:])
    gold = prev_ok & cur_ok & (macd[:-1] <= sig_line[:-1]) & (macd[1:] > sig_line[1:])
    dead = prev_ok & cur_ok & (macd[:-1] >= sig_line[:-1]) & (macd[1:] < sig_line[1:])
    s1 = np.zeros(n, dtype=np.int64)
    s1[1:] = gold.astype(np.int64) - dead.astype(np.int64)
    result['MACD_信号'] = s1

    prev_ok = np.isfinite(macd[:-1]) & np.isfinite(macd[1:])
    up0 = prev_ok & (macd[:-1] <= 0) & (macd[1:] > 0)
    down0 = prev_ok & (macd[:-1] >= 0) & (macd[1:] < 0)
    s2 = np.zeros(n, dtype=np.int64)
    s2[1:] = up0.astype(np.int64) - down0.astype(np.int64)
    result['MACD_零轴信号'] = s2

    prev_ok = np.isfinite(hist[:-1]) & np.isfinite(hist[1:])
    uph = prev_ok & (hist[:-1] <= 0) & (hist[1:] > 0)
    downh = prev_ok & (hist[:-1] >= 0) & (hist[1:] < 0)
    s3 = np.zeros(n, dtype=np.int64)
    s3[1:] = uph.astype(np.int64) - downh.astype(np.int64)
    result['MACD_柱状图信号'] = s3
    return result


def calculate_rsi(data: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    result = data.copy()
    delta = result['收盘价'].diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs = avg_gain / avg_loss
    result['RSI'] = 100 - (100 / (1 + rs))
    result = result.drop(['delta', 'gain', 'loss', 'avg_gain', 'avg_loss', 'rs'],
                         axis=1, errors='ignore')
    return result


def analyze_rsi_signals(data: pd.DataFrame, oversold: float = 30,
                        overbought: float = 70) -> pd.DataFrame:
    result = data.copy()
    if 'RSI' not in result.columns:
        return result
    rsi = result['RSI'].to_numpy(dtype=float)
    n = len(result)

    prev_ok = np.isfinite(rsi[:-1]) & np.isfinite(rsi[1:])
    up = prev_ok & (rsi[:-1] <= oversold) & (rsi[1:] > oversold)
    down = prev_ok & (rsi[:-1] >= overbought) & (rsi[1:] < overbought)
    s1 = np.zeros(n, dtype=np.int64)
    s1[1:] = up.astype(np.int64) - down.astype(np.int64)
    result['RSI_信号'] = s1

    tri_ok = np.isfinite(rsi[:-2]) & np.isfinite(rsi[1:-1]) & np.isfinite(rsi[2:])
    up3 = tri_ok & (rsi[:-2] < rsi[1:-1]) & (rsi[1:-1] < rsi[2:])
    down3 = tri_ok & (rsi[:-2] > rsi[1:-1]) & (rsi[1:-1] > rsi[2:])
    s2 = np.zeros(n, dtype=np.int64)
    s2[2:] = up3.astype(np.int64) - down3.astype(np.int64)
    result['RSI_趋势信号'] = s2
    return result


def calculate_trend_strength(data: pd.DataFrame) -> pd.DataFrame:
    result = data.copy()
    required_cols = ['收盘价', 'MA5', 'MA10', 'MA20', 'MA60']
    if any(c not in result.columns for c in required_cols):
        return result
    close = result['收盘价'].to_numpy(dtype=float)
    n = len(result)
    dist_sum = np.zeros(n)
    dist_count = np.zeros(n)
    for period in [5, 10, 20, 60]:
        ma = result[f'MA{period}'].to_numpy(dtype=float)
        valid = np.isfinite(ma) & (ma != 0)
        dist_sum[valid] += np.abs(close[valid] - ma[valid]) / np.abs(ma[valid]) * 100
        dist_count[valid] += 1
    arrangement = result['均线排列'].to_numpy(dtype=float) \
        if '均线排列' in result.columns else np.full(n, np.nan)
    trend = np.zeros(n)
    has_dist = dist_count > 0
    trend[has_dist] = dist_sum[has_dist] / dist_count[has_dist] * 0.7
    has_arr = has_dist & np.isfinite(arrangement)
    trend[has_arr] += arrangement[has_arr] * 30
    result['趋势强度'] = trend
    min_strength = result['趋势强度'].min()
    max_strength = result['趋势强度'].max()
    if max_strength > min_strength:
        result['趋势强度_normalized'] = ((result['趋势强度'] - min_strength)
                                          / (max_strength - min_strength) * 100)
    else:
        result['趋势强度_normalized'] = 0
    return result


# ---------------- 动能（量比 / 换手率 / 量价） ----------------

def calculate_volume_ratio(data: pd.DataFrame, period: int = 5) -> pd.DataFrame:
    result = data.copy()
    result['VMA'] = result['成交量'].rolling(window=period).mean()
    result['量比'] = result['成交量'] / result['VMA']
    result = result.drop(['VMA'], axis=1)
    return result


def calculate_turnover_rate(data: pd.DataFrame) -> pd.DataFrame:
    result = data.copy()
    result['换手率MA5'] = result['换手率'].rolling(window=5).mean()
    result['换手率MA10'] = result['换手率'].rolling(window=10).mean()
    result['换手率MA20'] = result['换手率'].rolling(window=20).mean()
    result['换手率变化率'] = result['换手率'].pct_change() * 100
    result['换手率相对位置'] = ((result['换手率'] - result['换手率MA20'])
                              / result['换手率MA20'] * 100)
    turnover = result['换手率'].to_numpy(dtype=float)
    zone = np.full(len(result), '未知', dtype=object)
    finite = np.isfinite(turnover)
    zone[finite & (turnover < 1)] = '低迷'
    zone[finite & (turnover >= 1) & (turnover < 3)] = '温和'
    zone[finite & (turnover >= 3) & (turnover < 5)] = '吸筹'
    zone[finite & (turnover >= 5) & (turnover < 8)] = '活跃'
    zone[finite & (turnover >= 8)] = '放量'
    result['换手率区域'] = zone
    return result


def calculate_volume_signals(data: pd.DataFrame) -> pd.DataFrame:
    result = data.copy()
    required_cols = ['成交量', '量比', '换手率']
    if any(c not in result.columns for c in required_cols):
        return result
    vr = result['量比'].to_numpy(dtype=float)
    vol = result['成交量'].to_numpy(dtype=float)
    n = len(result)
    s1 = np.zeros(n, dtype=np.int64)
    v_ok = np.isfinite(vr)
    s1[v_ok & (vr > 1.5)] = 1
    s1[v_ok & (vr < 0.5)] = -1
    result['成交量信号'] = s1

    vol_ma20 = result['成交量'].rolling(window=20).mean().to_numpy(dtype=float)
    prev_ok = np.isfinite(vol[:-1]) & np.isfinite(vol_ma20[:-1])
    cur_ok = np.isfinite(vol[1:]) & np.isfinite(vol_ma20[1:])
    up = prev_ok & cur_ok & (vol[:-1] <= 1.5 * vol_ma20[:-1]) \
        & (vol[1:] > 1.5 * vol_ma20[1:])
    # 注意：旧版 down 条件第二项用的是 i-1 的 MA20（不对称，为数学一致逐字复刻）
    down = prev_ok & cur_ok & (vol[:-1] >= 0.5 * vol_ma20[:-1]) \
        & (vol[1:] < 0.5 * vol_ma20[:-1])
    s2 = np.zeros(n, dtype=np.int64)
    s2[1:] = up.astype(np.int64) - down.astype(np.int64)
    result['成交量突破信号'] = s2
    return result


def calculate_price_momentum(data: pd.DataFrame) -> pd.DataFrame:
    result = data.copy()
    result['价格变化率1日'] = result['收盘价'].pct_change() * 100
    result['价格变化率5日'] = result['收盘价'].pct_change(periods=5) * 100
    result['价格变化率10日'] = result['收盘价'].pct_change(periods=10) * 100
    result['价格变化率20日'] = result['收盘价'].pct_change(periods=20) * 100
    result['价格加速度5日'] = result['价格变化率5日'].diff()
    result['价格加速度10日'] = result['价格变化率10日'].diff()
    result['价格动能'] = result['价格变化率5日'] * 0.6 + result['价格变化率10日'] * 0.4
    if 'MA20' in result.columns:
        result['相对强度'] = (result['收盘价'] - result['MA20']) / result['MA20'] * 100
    result['价格波动率'] = (result['收盘价'].rolling(window=20).std()
                            / result['收盘价'].rolling(window=20).mean() * 100)
    momentum = result['价格动能'].to_numpy(dtype=float)
    momentum = np.where(np.isfinite(momentum), momentum, 0.0)  # NaN → 0（与旧逻辑一致）
    state = np.full(len(result), '低迷', dtype=object)
    state[momentum > 5] = '强势'
    state[(momentum > 0) & (momentum <= 5)] = '温和'
    state[(momentum > -5) & (momentum <= 0)] = '弱势'
    result['动能状态'] = state
    return result


def calculate_volume_price_relation(data: pd.DataFrame) -> pd.DataFrame:
    result = data.copy()
    required_cols = ['收盘价', '成交量', '最高价', '最低价']
    if any(c not in result.columns for c in required_cols):
        return result
    close = result['收盘价'].to_numpy(dtype=float)
    vol = result['成交量'].to_numpy(dtype=float)
    n = len(result)
    price_change = np.full(n, np.nan)
    volume_change = np.full(n, np.nan)
    price_change[1:] = close[1:] - close[:-1]
    volume_change[1:] = vol[1:] - vol[:-1]

    p_ok = np.isfinite(price_change)
    v_ok = np.isfinite(volume_change)
    ok = p_ok & v_ok
    fit = np.zeros(n)
    fit[ok & (price_change > 0) & (volume_change > 0)] = 1.0
    fit[ok & (price_change > 0) & (volume_change <= 0)] = -1.0
    fit[ok & (price_change <= 0) & (volume_change > 0)] = -1.0
    result['量价配合度'] = fit

    # OBV（递推，numpy 数组循环：仅价格变化非 NaN 时更新，与旧逻辑一致）
    obv = np.zeros(n)
    for i in range(1, n):
        if not np.isnan(price_change[i]):
            if price_change[i] > 0:
                obv[i] = obv[i - 1] + vol[i]
            elif price_change[i] < 0:
                obv[i] = obv[i - 1] - vol[i]
            else:
                obv[i] = obv[i - 1]
    result['OBV'] = obv
    obv_ma = pd.Series(obv).rolling(window=20).mean().to_numpy(dtype=float)
    result['OBV_MA'] = obv_ma
    prev_ok = np.isfinite(obv[:-1]) & np.isfinite(obv_ma[:-1])
    cur_ok = np.isfinite(obv[1:]) & np.isfinite(obv_ma[1:])
    up = prev_ok & cur_ok & (obv[:-1] <= obv_ma[:-1]) \
        & (obv[1:] > obv_ma[1:])
    down = prev_ok & cur_ok & (obv[:-1] >= obv_ma[:-1]) \
        & (obv[1:] < obv_ma[1:])
    obv_sig = np.zeros(n, dtype=float)
    obv_sig[1:] = up.astype(float) - down.astype(float)
    result['OBV信号'] = obv_sig
    return result


# ---------------- 加工链（main.py _calculate_all_indicators 复刻） ----------------

def enrich_indicators(data: pd.DataFrame) -> pd.DataFrame:
    """按 main.py 顺序加工全部指标（均线→排列→斜率→信号→MACD→RSI→动能）。"""
    df = data.copy()
    df = calculate_ma(df)
    df = calculate_ma_arrangement(df)
    df = calculate_ma_slope(df)
    df = analyze_ma_signals(df)
    df = calculate_macd(df)
    df = calculate_rsi(df)
    df = calculate_trend_strength(df)
    df = analyze_macd_signals(df)
    df = analyze_rsi_signals(df)
    df = calculate_volume_ratio(df)
    df = calculate_turnover_rate(df)
    df = calculate_price_momentum(df)
    df = calculate_volume_price_relation(df)
    df = calculate_volume_signals(df)
    return df
