"""mystery.core.resonance — 共振/板块强度（迁自 resonance_analyzer.py）。

改造：calculate_industry_score_from_sector 改为吃板块 K 线 DataFrame
（收盘价/成交额，升序），不再自己查库（core 零 IO 约束）。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def analyze_market_trend(index_data: pd.DataFrame) -> Dict[str, Any]:
    """大盘趋势：收盘价 vs MA20/MA60 + 近120日位置。"""
    result = {'趋势方向': '未知', '强度': 0, 'MA20状态': '未知', 'MA60状态': '未知',
              'position': '未知', '近20日涨幅': None, '详情': []}
    try:
        if index_data is None or index_data.empty or len(index_data) < 60:
            result['详情'].append('数据不足，无法分析市场趋势')
            return result
        df = index_data.copy()
        if '收盘价' not in df.columns:
            result['详情'].append('缺少收盘价列')
            return result
        if 'MA20' not in df.columns:
            df['MA20'] = df['收盘价'].rolling(window=20).mean()
        if 'MA60' not in df.columns:
            df['MA60'] = df['收盘价'].rolling(window=60).mean()
        latest = df.iloc[-1]
        close = float(latest['收盘价'])
        ma20, ma60 = latest['MA20'], latest['MA60']
        if pd.notna(ma20):
            result['MA20状态'] = '上方' if close > ma20 else '下方'
        if pd.notna(ma60):
            result['MA60状态'] = '上方' if close > ma60 else '下方'
        if result['MA20状态'] == '上方' and result['MA60状态'] == '上方':
            result['趋势方向'] = '向上'
        elif result['MA20状态'] == '下方' and result['MA60状态'] == '下方':
            result['趋势方向'] = '向下'
        else:
            result['趋势方向'] = '震荡'
        if len(df) >= 20:
            past = float(df['收盘价'].iloc[-20])
            if past > 0:
                chg = (close / past - 1) * 100
                result['近20日涨幅'] = round(chg, 2)
                result['强度'] = min(abs(chg), 100.0)
        if len(df) >= 120:
            high_120 = float(df['收盘价'].iloc[-120:].max())
            low_120 = float(df['收盘价'].iloc[-120:].min())
            if high_120 > low_120:
                pos_pct = (close - low_120) / (high_120 - low_120)
                if pos_pct >= 0.85:
                    result['position'] = '高位'
                elif pos_pct <= 0.15:
                    result['position'] = '低位'
                else:
                    result['position'] = '中位'
                result['详情'].append(f'近120日位置 {pos_pct:.0%}')
        return result
    except Exception as e:
        logger.error(f'❌ 分析市场趋势异常: {e}')
        return {'趋势方向': '异常', '详情': [f'分析异常: {e}']}


def analyze_industry_trend(industry_data: Dict[str, pd.DataFrame], lookback: int = 10) -> Dict[str, Any]:
    """行业趋势（industry_data: {名称: K线df}）。"""
    empty_result = {'强势行业': [], '弱势行业': [], '中性行业': [], '整体趋势': '未知',
                    '强度': 0, 'strong_count': 0, 'weak_count': 0, 'neutral_count': 0,
                    'detail': '无行业数据', 'top_industries': [], 'top_detail': [],
                    '详情': ['无行业数据']}
    try:
        if not industry_data:
            return empty_result
        strong, weak, neutral = [], [], []
        industry_scores = []
        for name, df in industry_data.items():
            if df is None or df.empty or len(df) < 5:
                continue
            df = df.copy()
            if '收盘价' not in df.columns:
                continue
            if 'MA20' not in df.columns:
                df['MA20'] = df['收盘价'].rolling(window=20).mean()
            latest = df.iloc[-1]
            close = float(latest['收盘价'])
            bias = 0.0
            ma20 = latest.get('MA20')
            if pd.notna(ma20) and ma20 > 0:
                bias = (close / float(ma20) - 1) * 100
            change_n = 0.0
            if len(df) >= lookback:
                past = float(df['收盘价'].iloc[-lookback])
                if past > 0:
                    change_n = (close / past - 1) * 100
            amount_score = 0
            if '成交额' in df.columns and len(df) >= 6:
                amount_ma = df['成交额'].iloc[-6:-1].mean()
                if amount_ma and amount_ma > 0:
                    amount_ratio = float(latest['成交额']) / amount_ma
                    if amount_ratio >= 1.5:
                        amount_score = 1
            score = 0
            if bias < -5:
                score = -2
            elif bias < -2:
                score = -1
            elif bias > 5 and change_n > 3:
                score = 2 + amount_score
            elif bias > 2 and change_n > 0:
                score = 1 + amount_score
            if score >= 2:
                strong.append(name)
            elif score <= -2:
                weak.append(name)
            else:
                neutral.append(name)
            industry_scores.append({'name': name, 'score': score, 'bias': round(bias, 2),
                                    'change_n': round(change_n, 2), 'amount_score': amount_score})
        strong_cnt, weak_cnt = len(strong), len(weak)
        total = max(strong_cnt + weak_cnt + len(neutral), 1)
        if strong_cnt >= weak_cnt + 2 and strong_cnt >= max(3, int(total * 0.25)):
            trend = '向上'
            strength = min(100, int(strong_cnt / total * 100) + 20)
        elif weak_cnt >= strong_cnt + 2 and weak_cnt >= max(3, int(total * 0.25)):
            trend = '向下'
            strength = min(100, int(weak_cnt / total * 100) + 20)
        else:
            trend = '震荡'
            strength = 30
        top_detail = sorted([x for x in industry_scores if x['score'] >= 2],
                            key=lambda x: (x['score'], x['change_n']), reverse=True)[:5]
        return {'强势行业': strong, '弱势行业': weak, '中性行业': neutral, '整体趋势': trend,
                '强度': strength, 'strong_count': strong_cnt, 'weak_count': weak_cnt,
                'neutral_count': len(neutral), 'detail': f'强势{strong_cnt} / 弱势{weak_cnt} / 中性{len(neutral)}',
                'top_industries': [x['name'] for x in top_detail], 'top_detail': top_detail,
                '详情': [f'强势{strong_cnt} / 弱势{weak_cnt} / 中性{len(neutral)}',
                         f"最强: {[x['name'] for x in top_detail[:3]]}"]}
    except Exception as e:
        logger.error(f'❌ 分析行业趋势异常: {e}')
        return {**empty_result, '详情': [f'分析异常: {e}']}


def calculate_industry_score_from_sector(sector_kline: pd.DataFrame) -> float:
    """板块指数 K 线（升序，含 收盘价/成交额）→ 行业强度分（0~25，>12.5 向上）。

    数学与旧仓 calculate_industry_score_from_sector(marketdb_df=...) 分支完全一致：
    bias(MA20) + 近10日涨幅 + 量能（近5日均额/近20日均额）。
    """
    try:
        if sector_kline is None or sector_kline.empty:
            return 12.5
        df = sector_kline.copy()
        closes = pd.to_numeric(df['收盘价'], errors='coerce').dropna().astype(float).tolist()[-60:]
        amounts = (pd.to_numeric(df['成交额'], errors='coerce').dropna().astype(float).tolist()[-60:]
                   if '成交额' in df.columns else [0.0] * len(closes))
        if len(closes) < 20:
            return 12.5
        closes = np.array(closes, dtype=float)
        amounts = np.array(amounts, dtype=float)
        cur = closes[-1]
        ma20 = closes[-20:].mean()
        bias = (cur - ma20) / ma20 if ma20 > 0 else 0
        bias_score = min(10.0, max(0.0, bias * 100 + 5.0))
        ret10 = (closes[-1] - closes[-10]) / closes[-10] if closes[-10] > 0 else 0
        ret_score = min(7.5, max(0.0, ret10 * 100 + 3.75))
        recent_amt = amounts[-5:].mean()
        hist_amt = amounts[-20:].mean()
        ratio = recent_amt / (hist_amt + 1e-06)
        vol_score = min(7.5, max(0.0, ratio * 3.75))
        return float(round(bias_score + ret_score + vol_score, 2))
    except Exception as e:
        logger.error(f'❌ 行业指数趋势分异常: {e}')
        return 12.5


def industry_trend_from_kline(sector_kline: pd.DataFrame) -> Optional[bool]:
    """板块指数 K 线 → 行业趋势布尔（>12.5 向上）。"""
    score = calculate_industry_score_from_sector(sector_kline)
    if score is None:
        return None
    return bool(score > 12.5)


def analyze_capital_flow(stock_data: Optional[pd.DataFrame]) -> Dict[str, Any]:
    """资金活跃度：量比/成交额/换手率。"""
    empty = {'active': False, 'score': 0, 'volume_ratio': 0.0, 'detail': '资金平淡'}
    try:
        if stock_data is None or stock_data.empty or len(stock_data) < 6:
            return empty
        df = stock_data.copy()
        latest = df.iloc[-1]
        vol_col = '成交量' if '成交量' in df.columns else 'volume'
        amount_col = '成交额' if '成交额' in df.columns else 'amount'
        turn_col = '换手率' if '换手率' in df.columns else None
        if vol_col not in df.columns:
            return empty
        vol_ma5 = float(df[vol_col].iloc[-6:-1].mean())
        volume_ratio = float(latest[vol_col]) / (vol_ma5 + 1e-08)
        score = 0
        reasons = []
        if volume_ratio >= 1.8:
            score += 12
            reasons.append(f'量比{volume_ratio:.1f}')
        elif volume_ratio >= 1.5:
            score += 8
            reasons.append(f'量比{volume_ratio:.1f}')
        if amount_col in df.columns and len(df) >= 6:
            amount_ma5 = df[amount_col].iloc[-6:-1].mean()
            if amount_ma5 and amount_ma5 > 0:
                amount_ratio = float(latest[amount_col]) / amount_ma5
                if amount_ratio >= 1.6:
                    score += 5
                    reasons.append('成交额放大')
        if turn_col and turn_col in df.columns:
            turnover = float(latest[turn_col]) if pd.notna(latest[turn_col]) else 0.0
            if turnover >= 3.0:
                score += 3
                reasons.append(f'换手{turnover:.1f}%')
        score = min(score, 20)
        active = score >= 8 or volume_ratio >= 1.5
        return {'active': active, 'score': score, 'volume_ratio': round(volume_ratio, 2),
                'detail': ' | '.join(reasons) if reasons else '资金平淡'}
    except Exception as e:
        logger.error(f'❌ 资金活跃度分析异常: {e}')
        return empty


def calculate_resonance_score(individual_result: Dict, market_result: Dict,
                              industry_result: Dict,
                              capital_result: Optional[Dict] = None) -> Dict[str, Any]:
    """四维共振评分 → score/level/advice/is_true_three_strike。"""
    try:
        score = 0.0
        details = []
        stock_ok = bool(individual_result.get('基础过滤', False)) and bool(individual_result.get('均线多头', False))
        if stock_ok:
            score += 30
            details.append('个股趋势✓(+30)')
        else:
            details.append('个股趋势✗')
        market_trend = market_result.get('趋势方向', market_result.get('trend', '未知'))
        if market_trend == '向上':
            score += 25
            details.append('大盘向上✓(+25)')
        else:
            details.append(f'大盘{market_trend}')
        industry_trend = industry_result.get('整体趋势', industry_result.get('trend', '未知'))
        if industry_trend == '向上':
            score += 25
            details.append(f"行业向上✓(+25) [{industry_result.get('detail', '')}]")
        else:
            details.append(f'行业{industry_trend}')
        capital_score = 0
        capital_active = False
        if capital_result:
            capital_score = float(capital_result.get('score', 0) or 0)
            capital_active = bool(capital_result.get('active', False))
            score += capital_score
            details.append(f"资金(+{capital_score:.0f}) {capital_result.get('detail', '')}")
        market_position = market_result.get('position', '未知')
        if market_position == '高位':
            score = max(0.0, score - 15)
            details.append('大盘高位惩罚(-15)')
        is_true = score >= 85 and capital_active and (market_trend == '向上') \
            and (industry_trend == '向上') and stock_ok
        if is_true:
            level = '真三振（三级）'
            advice = '强烈建议关注！可能是大级别行情启动窗口，大资金跨层级共振'
        elif score >= 70:
            level = '二级共振'
            advice = '可关注，需持续观察资金与板块持续性'
        elif score >= 45:
            level = '一级共振'
            advice = '观望为主，等待更明确的资金与板块信号'
        else:
            level = '无共振'
            advice = '建议观望，留住本金，等待真正的三振机会'
        return {'个股共振': 30 if stock_ok else 0,
                '市场共振': 25 if market_trend == '向上' else 0,
                '行业共振': 25 if industry_trend == '向上' else 0,
                '总共振评分': round(score, 1), '共振级别': level, 'score': round(score, 1),
                'level': level, 'advice': advice, 'is_true_three_strike': is_true,
                'details': details, 'capital_active': capital_active,
                'industry_top': industry_result.get('top_industries', []),
                'market_position': market_position, '详情': details}
    except Exception as e:
        logger.error(f'❌ 计算共振评分异常: {e}')
        return {'总共振评分': 0, '共振级别': '异常', 'score': 0, 'level': '异常',
                'is_true_three_strike': False, '详情': [f'分析异常: {e}']}


class ResonanceAnalyzer:
    """兼容容器（纯函数门面，供旧调用风格使用）。"""

    def analyze_market_trend(self, index_data: pd.DataFrame) -> Dict[str, Any]:
        return analyze_market_trend(index_data)

    def analyze_industry_trend(self, industry_data: Dict[str, pd.DataFrame],
                               lookback: int = 10) -> Dict[str, Any]:
        return analyze_industry_trend(industry_data, lookback=lookback)

    def calculate_industry_score_from_sector(self, sector_code: str, db_path: str = None,
                                             marketdb_df: pd.DataFrame = None) -> float:
        if marketdb_df is not None:
            return calculate_industry_score_from_sector(marketdb_df)
        raise RuntimeError("core 层禁止查库：请先取板块K线 DataFrame 再调用")

    def analyze_capital_flow(self, stock_data: Optional[pd.DataFrame]) -> Dict[str, Any]:
        return analyze_capital_flow(stock_data)

    def calculate_resonance_score(self, individual_result: Dict, market_result: Dict,
                                  industry_result: Dict,
                                  capital_result: Optional[Dict] = None) -> Dict[str, Any]:
        return calculate_resonance_score(individual_result, market_result,
                                         industry_result, capital_result)
