"""mystery.core.indicators — 技术指标纯函数（迁自 stock_analyzer/indicators/*）。

零 IO。列名与旧仓一致（中文列：收盘价/成交量/换手率…），保证数学完全一致。
main.py 的加工链由 enrich_indicators() 一次性复刻。
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
    arrangement = []
    for i in range(len(result)):
        ma_values = [result.loc[i, col] for col in ma_cols]
        valid_values = [v for v in ma_values if pd.notna(v)]
        if len(valid_values) >= 3:
            is_bullish = True
            is_bearish = True
            for j in range(len(valid_values) - 1):
                if valid_values[j] <= valid_values[j + 1]:
                    is_bullish = False
                if valid_values[j] >= valid_values[j + 1]:
                    is_bearish = False
            if is_bullish:
                arrangement.append(1)
            elif is_bearish:
                arrangement.append(-1)
            else:
                arrangement.append(0)
        else:
            arrangement.append(np.nan)
    result['均线排列'] = arrangement
    # 排列强度（多头排列的均线对数）
    bullish_count = []
    for i in range(len(result)):
        if pd.notna(result.loc[i, '均线排列']) and result.loc[i, '均线排列'] == 1:
            cur = [result.loc[i, col] for col in ma_cols]
            bullish_count.append(sum(
                1 for j in range(len(cur) - 1)
                if pd.notna(cur[j]) and pd.notna(cur[j + 1]) and cur[j] > cur[j + 1]))
        elif pd.notna(result.loc[i, '均线排列']):
            bullish_count.append(0)
        else:
            bullish_count.append(np.nan)
    result['多头排列强度'] = bullish_count
    return result


def analyze_ma_signals(data: pd.DataFrame) -> pd.DataFrame:
    """均线金叉/死叉信号 + 突破MA20信号。"""
    result = data.copy()
    required_cols = ['收盘价', 'MA5', 'MA10', 'MA20', 'MA60', 'MA250']
    if any(c not in result.columns for c in required_cols):
        return result
    result['均线信号'] = 0
    for i in range(1, len(result)):
        if (pd.notna(result.iloc[i - 1]['MA5']) and pd.notna(result.iloc[i - 1]['MA10'])
                and pd.notna(result.iloc[i]['MA5']) and pd.notna(result.iloc[i]['MA10'])
                and result.iloc[i - 1]['MA5'] <= result.iloc[i - 1]['MA10']
                and result.iloc[i]['MA5'] > result.iloc[i]['MA10']):
            result.iloc[i, result.columns.get_loc('均线信号')] = 1
        elif (pd.notna(result.iloc[i - 1]['MA5']) and pd.notna(result.iloc[i - 1]['MA10'])
              and pd.notna(result.iloc[i]['MA5']) and pd.notna(result.iloc[i]['MA10'])
              and result.iloc[i - 1]['MA5'] >= result.iloc[i - 1]['MA10']
              and result.iloc[i]['MA5'] < result.iloc[i]['MA10']):
            result.iloc[i, result.columns.get_loc('均线信号')] = -1
    result['突破信号'] = 0
    for i in range(1, len(result)):
        if (pd.notna(result.iloc[i - 1]['收盘价']) and pd.notna(result.iloc[i - 1]['MA20'])
                and pd.notna(result.iloc[i]['收盘价']) and pd.notna(result.iloc[i]['MA20'])
                and result.iloc[i - 1]['收盘价'] <= result.iloc[i - 1]['MA20']
                and result.iloc[i]['收盘价'] > result.iloc[i]['MA20']):
            result.iloc[i, result.columns.get_loc('突破信号')] = 1
        elif (pd.notna(result.iloc[i - 1]['收盘价']) and pd.notna(result.iloc[i - 1]['MA20'])
              and pd.notna(result.iloc[i]['收盘价']) and pd.notna(result.iloc[i]['MA20'])
              and result.iloc[i - 1]['收盘价'] >= result.iloc[i - 1]['MA20']
              and result.iloc[i]['收盘价'] < result.iloc[i]['MA20']):
            result.iloc[i, result.columns.get_loc('突破信号')] = -1
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
    result['MACD_信号'] = 0
    for i in range(1, len(result)):
        if (pd.notna(result.iloc[i - 1]['MACD']) and pd.notna(result.iloc[i - 1]['MACD_Signal'])
                and pd.notna(result.iloc[i]['MACD']) and pd.notna(result.iloc[i]['MACD_Signal'])
                and result.iloc[i - 1]['MACD'] <= result.iloc[i - 1]['MACD_Signal']
                and result.iloc[i]['MACD'] > result.iloc[i]['MACD_Signal']):
            result.iloc[i, result.columns.get_loc('MACD_信号')] = 1
        elif (pd.notna(result.iloc[i - 1]['MACD']) and pd.notna(result.iloc[i - 1]['MACD_Signal'])
              and pd.notna(result.iloc[i]['MACD']) and pd.notna(result.iloc[i]['MACD_Signal'])
              and result.iloc[i - 1]['MACD'] >= result.iloc[i - 1]['MACD_Signal']
              and result.iloc[i]['MACD'] < result.iloc[i]['MACD_Signal']):
            result.iloc[i, result.columns.get_loc('MACD_信号')] = -1
    result['MACD_零轴信号'] = 0
    for i in range(1, len(result)):
        if (pd.notna(result.iloc[i - 1]['MACD']) and pd.notna(result.iloc[i]['MACD'])
                and result.iloc[i - 1]['MACD'] <= 0 and result.iloc[i]['MACD'] > 0):
            result.iloc[i, result.columns.get_loc('MACD_零轴信号')] = 1
        elif (pd.notna(result.iloc[i - 1]['MACD']) and pd.notna(result.iloc[i]['MACD'])
              and result.iloc[i - 1]['MACD'] >= 0 and result.iloc[i]['MACD'] < 0):
            result.iloc[i, result.columns.get_loc('MACD_零轴信号')] = -1
    result['MACD_柱状图信号'] = 0
    for i in range(1, len(result)):
        if (pd.notna(result.iloc[i - 1]['MACD_Histogram']) and pd.notna(result.iloc[i]['MACD_Histogram'])
                and result.iloc[i - 1]['MACD_Histogram'] <= 0 and result.iloc[i]['MACD_Histogram'] > 0):
            result.iloc[i, result.columns.get_loc('MACD_柱状图信号')] = 1
        elif (pd.notna(result.iloc[i - 1]['MACD_Histogram']) and pd.notna(result.iloc[i]['MACD_Histogram'])
              and result.iloc[i - 1]['MACD_Histogram'] >= 0 and result.iloc[i]['MACD_Histogram'] < 0):
            result.iloc[i, result.columns.get_loc('MACD_柱状图信号')] = -1
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
    result['RSI_信号'] = 0
    for i in range(1, len(result)):
        if (pd.notna(result.iloc[i - 1]['RSI']) and pd.notna(result.iloc[i]['RSI'])
                and result.iloc[i - 1]['RSI'] <= oversold and result.iloc[i]['RSI'] > oversold):
            result.iloc[i, result.columns.get_loc('RSI_信号')] = 1
        elif (pd.notna(result.iloc[i - 1]['RSI']) and pd.notna(result.iloc[i]['RSI'])
              and result.iloc[i - 1]['RSI'] >= overbought and result.iloc[i]['RSI'] < overbought):
            result.iloc[i, result.columns.get_loc('RSI_信号')] = -1
    result['RSI_趋势信号'] = 0
    for i in range(2, len(result)):
        if (pd.notna(result.iloc[i - 2]['RSI']) and pd.notna(result.iloc[i - 1]['RSI'])
                and pd.notna(result.iloc[i]['RSI'])
                and result.iloc[i - 2]['RSI'] < result.iloc[i - 1]['RSI'] < result.iloc[i]['RSI']):
            result.iloc[i, result.columns.get_loc('RSI_趋势信号')] = 1
        elif (pd.notna(result.iloc[i - 2]['RSI']) and pd.notna(result.iloc[i - 1]['RSI'])
              and pd.notna(result.iloc[i]['RSI'])
              and result.iloc[i - 2]['RSI'] > result.iloc[i - 1]['RSI'] > result.iloc[i]['RSI']):
            result.iloc[i, result.columns.get_loc('RSI_趋势信号')] = -1
    return result


def calculate_trend_strength(data: pd.DataFrame) -> pd.DataFrame:
    result = data.copy()
    required_cols = ['收盘价', 'MA5', 'MA10', 'MA20', 'MA60']
    if any(c not in result.columns for c in required_cols):
        return result
    result['趋势强度'] = 0.0
    for i in range(len(result)):
        distances = []
        for period in [5, 10, 20, 60]:
            ma_col = f'MA{period}'
            if ma_col in result.columns and pd.notna(result.loc[i, ma_col]):
                distances.append(abs(result.loc[i, '收盘价'] - result.loc[i, ma_col])
                                 / result.loc[i, ma_col] * 100)
        if distances:
            arrangement_score = 0
            if '均线排列' in result.columns and pd.notna(result.loc[i, '均线排列']):
                arrangement_score = result.loc[i, '均线排列']
            result.loc[i, '趋势强度'] = np.mean(distances) * 0.7 + arrangement_score * 30
    if '趋势强度' in result.columns:
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
    result['换手率区域'] = '未知'
    for i in range(len(result)):
        turnover = result.loc[i, '换手率']
        if pd.notna(turnover):
            if turnover < 1:
                result.loc[i, '换手率区域'] = '低迷'
            elif turnover < 3:
                result.loc[i, '换手率区域'] = '温和'
            elif turnover < 5:
                result.loc[i, '换手率区域'] = '吸筹'
            elif turnover < 8:
                result.loc[i, '换手率区域'] = '活跃'
            else:
                result.loc[i, '换手率区域'] = '放量'
    return result


def calculate_volume_signals(data: pd.DataFrame) -> pd.DataFrame:
    result = data.copy()
    required_cols = ['成交量', '量比', '换手率']
    if any(c not in result.columns for c in required_cols):
        return result
    result['成交量信号'] = 0
    for i in range(1, len(result)):
        if pd.notna(result.iloc[i]['量比']) and result.iloc[i]['量比'] > 1.5:
            result.iloc[i, result.columns.get_loc('成交量信号')] = 1
        elif pd.notna(result.iloc[i]['量比']) and result.iloc[i]['量比'] < 0.5:
            result.iloc[i, result.columns.get_loc('成交量信号')] = -1
    result['成交量突破信号'] = 0
    result['成交量MA20'] = result['成交量'].rolling(window=20).mean()
    for i in range(1, len(result)):
        if (pd.notna(result.iloc[i - 1]['成交量']) and pd.notna(result.iloc[i - 1]['成交量MA20'])
                and pd.notna(result.iloc[i]['成交量']) and pd.notna(result.iloc[i]['成交量MA20'])
                and result.iloc[i - 1]['成交量'] <= 1.5 * result.iloc[i - 1]['成交量MA20']
                and result.iloc[i]['成交量'] > 1.5 * result.iloc[i]['成交量MA20']):
            result.iloc[i, result.columns.get_loc('成交量突破信号')] = 1
        elif (pd.notna(result.iloc[i - 1]['成交量']) and pd.notna(result.iloc[i - 1]['成交量MA20'])
              and pd.notna(result.iloc[i]['成交量']) and pd.notna(result.iloc[i]['成交量MA20'])
              and result.iloc[i - 1]['成交量'] >= 0.5 * result.iloc[i - 1]['成交量MA20']
              and result.iloc[i]['成交量'] < 0.5 * result.iloc[i - 1]['成交量MA20']):
            result.iloc[i, result.columns.get_loc('成交量突破信号')] = -1
    result = result.drop(['成交量MA20'], axis=1)
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
    result['动能状态'] = '未知'
    for i in range(len(result)):
        momentum = (result.loc[i, '价格动能'] if '价格动能' in result.columns
                    and pd.notna(result.loc[i, '价格动能']) else 0)
        if pd.notna(momentum):
            if momentum > 5:
                result.loc[i, '动能状态'] = '强势'
            elif momentum > 0:
                result.loc[i, '动能状态'] = '温和'
            elif momentum > -5:
                result.loc[i, '动能状态'] = '弱势'
            else:
                result.loc[i, '动能状态'] = '低迷'
    return result


def calculate_volume_price_relation(data: pd.DataFrame) -> pd.DataFrame:
    result = data.copy()
    required_cols = ['收盘价', '成交量', '最高价', '最低价']
    if any(c not in result.columns for c in required_cols):
        return result
    price_change = result['收盘价'].diff()
    volume_change = result['成交量'].diff()
    result['量价配合度'] = 0
    for i in range(1, len(result)):
        if pd.notna(price_change.iloc[i]) and pd.notna(volume_change.iloc[i]):
            if price_change.iloc[i] > 0 and volume_change.iloc[i] > 0:
                result.loc[i, '量价配合度'] = 1
            elif price_change.iloc[i] > 0 and volume_change.iloc[i] <= 0:
                result.loc[i, '量价配合度'] = -1
            elif price_change.iloc[i] <= 0 and volume_change.iloc[i] > 0:
                result.loc[i, '量价配合度'] = -1
            else:
                result.loc[i, '量价配合度'] = 0
    result['OBV'] = 0
    for i in range(1, len(result)):
        if pd.notna(price_change.iloc[i]):
            if price_change.iloc[i] > 0:
                result.loc[i, 'OBV'] = result.loc[i - 1, 'OBV'] + result.loc[i, '成交量']
            elif price_change.iloc[i] < 0:
                result.loc[i, 'OBV'] = result.loc[i - 1, 'OBV'] - result.loc[i, '成交量']
            else:
                result.loc[i, 'OBV'] = result.loc[i - 1, 'OBV']
    result['OBV_MA'] = result['OBV'].rolling(window=20).mean()
    result['OBV信号'] = 0
    for i in range(1, len(result)):
        if (pd.notna(result.iloc[i - 1]['OBV_MA']) and pd.notna(result.iloc[i]['OBV_MA'])
                and pd.notna(result.iloc[i - 1]['OBV']) and pd.notna(result.iloc[i]['OBV'])):
            if (result.iloc[i - 1]['OBV'] <= result.iloc[i - 1]['OBV_MA']
                    and result.iloc[i]['OBV'] > result.iloc[i]['OBV_MA']):
                result.iloc[i, result.columns.get_loc('OBV信号')] = 1
            elif (result.iloc[i - 1]['OBV'] >= result.iloc[i - 1]['OBV_MA']
                  and result.iloc[i]['OBV'] < result.iloc[i]['OBV_MA']):
                result.iloc[i, result.columns.get_loc('OBV信号')] = -1
    result = result.drop(['price_change', 'volume_change'], axis=1, errors='ignore')
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
