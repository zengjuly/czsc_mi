"""mystery.core.mystery_rules — Mystery 规则（迁自 stock_analyzer/analysis/mystery_logic.py）。

改造：去掉一切数据客户端与 indicators 死依赖；resonance/platform 走 core 模块。
入参 DataFrame（中文列），返回 dict，数学与旧仓完全一致。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from . import resonance as _res
from .platform import analyze_adaptive_platform

logger = logging.getLogger(__name__)


class MysteryLogic:
    """Mystery 规则引擎（纯函数容器，零 IO）。"""

    def __init__(self):
        self.logger = logger

    # ---------- 基础过滤 ----------
    def basic_filter(self, data: pd.DataFrame) -> Tuple[bool, List[str]]:
        """一票否决制：年线/60日线/均线多头。"""
        errors = []
        passed = True
        try:
            required_cols = ['收盘价', 'MA5', 'MA10', 'MA20', 'MA60', 'MA250']
            missing_cols = [col for col in required_cols if col not in data.columns]
            if missing_cols:
                errors.append(f'缺少必要技术指标列: {missing_cols}')
                return (False, errors)
            latest_data = data.iloc[-1]
            if pd.notna(latest_data['MA250']) and pd.notna(latest_data['收盘价']):
                if latest_data['收盘价'] < latest_data['MA250']:
                    errors.append('股价未运行在250日均线上方')
                    passed = False
            else:
                errors.append('年线数据缺失')
                passed = False
            if pd.notna(latest_data['MA60']) and pd.notna(latest_data['收盘价']):
                if latest_data['收盘价'] < latest_data['MA60']:
                    errors.append('股价未运行在60日均线上方')
                    passed = False
            if '均线排列' in data.columns and pd.notna(latest_data['均线排列']):
                if latest_data['均线排列'] != 1:
                    errors.append('均线未呈现多头顺次排列')
                    passed = False
            else:
                ma_check = True
                ma_periods = [5, 10, 20, 60]
                for i in range(len(ma_periods) - 1):
                    ma_col1 = f'MA{ma_periods[i]}'
                    ma_col2 = f'MA{ma_periods[i + 1]}'
                    if (pd.notna(latest_data[ma_col1]) and pd.notna(latest_data[ma_col2])
                            and latest_data[ma_col1] <= latest_data[ma_col2]):
                        ma_check = False
                        break
                if not ma_check:
                    errors.append('均线未呈现多头顺次排列')
                    passed = False
            if pd.notna(latest_data.get('MA250')):
                for w in ['MA5', 'MA10', 'MA20', 'MA60']:
                    v = latest_data.get(w)
                    if pd.notna(v) and v <= latest_data['MA250']:
                        errors.append(f'{w}未运行在年线(MA250)上方')
                        passed = False
                        break
            self.logger.info(f"{'✅' if passed else '❌'} 基础过滤 "
                             f"{'通过' if passed else '失败'}: {len(errors)} 个错误")
            return (passed, errors)
        except Exception as e:
            self.logger.error(f'❌ 基础过滤异常: {e}')
            errors.append(f'基础过滤异常: {e}')
            return (False, errors)

    # ---------- 三振共振 ----------
    def three_resonance_analysis(self, data: pd.DataFrame, market_data: Dict = None,
                                 industry_trend: bool = None,
                                 industry_data: Dict = None) -> Dict[str, Any]:
        """四维共振：个股 + 大盘 + 行业 + 资金。"""
        try:
            result = {'个股趋势': False, '行业趋势': False, '大盘趋势': False,
                      '三级共振': False, '共振评分': 0, '共振级别': '无共振',
                      '共振建议': '', '资金活跃': False, '最强板块': [], '大盘位置': '未知',
                      '真三振': False, '详情': []}
            stock_ok = False
            if '均线排列' in data.columns and pd.notna(data.iloc[-1]['均线排列']):
                if data.iloc[-1]['均线排列'] == 1:
                    stock_ok = True
                    result['详情'].append('个股均线多头排列')
            if 'MA20' in data.columns and '收盘价' in data.columns:
                if (pd.notna(data.iloc[-1]['MA20']) and pd.notna(data.iloc[-1]['收盘价'])
                        and data.iloc[-1]['收盘价'] > data.iloc[-1]['MA20']):
                    stock_ok = True
                    result['详情'].append('股价运行在20日均线上方')
            if not stock_ok:
                result['详情'].append('个股趋势：均线未多头排列或股价在20日线下方')
            basic_passed = True
            try:
                basic_passed, _ = self.basic_filter(data)
            except Exception:
                basic_passed = False
            result['个股趋势'] = stock_ok
            individual_result = {'基础过滤': basic_passed, '均线多头': stock_ok}
            if industry_data:
                industry_result = _res.analyze_industry_trend(industry_data)
                ind_trend = industry_result.get('整体趋势', '未知')
                result['行业趋势'] = ind_trend == '向上'
                result['最强板块'] = industry_result.get('top_industries', [])
                if result['最强板块']:
                    result['详情'].append(f"最强板块: {result['最强板块'][:3]}")
                result['详情'].append(industry_result.get('detail', ''))
            else:
                if industry_trend is True:
                    result['行业趋势'] = True
                    result['详情'].append('行业板块同步走强')
                elif industry_trend is False:
                    result['详情'].append('行业板块走弱')
                else:
                    result['详情'].append('行业趋势数据缺失')
                industry_result = {'整体趋势': '向上' if result['行业趋势']
                                   else '向下' if industry_trend is False else '未知',
                                   'detail': '行业板块同步走强' if result['行业趋势']
                                   else '行业趋势数据缺失', 'top_industries': []}
            index_data = None
            index_name = None
            if market_data:
                index_name = ('上证指数' if '上证指数' in market_data
                              else list(market_data.keys())[0] if market_data else None)
                if index_name:
                    index_data = market_data[index_name]
            if index_data is not None and (not index_data.empty):
                market_result = _res.analyze_market_trend(index_data)
                mk_trend = market_result.get('趋势方向', '未知')
                result['大盘趋势'] = mk_trend == '向上'
                result['大盘位置'] = market_result.get('position', '未知')
                result['详情'].append(f"大盘{index_name} {mk_trend}（位置:{result['大盘位置']}）")
            else:
                result['大盘趋势'] = False
                result['详情'].append('大盘趋势数据缺失')
                market_result = {'趋势方向': '未知', 'position': '未知', '详情': []}
            capital_result = _res.analyze_capital_flow(data)
            result['资金活跃'] = capital_result.get('active', False)
            if capital_result.get('detail'):
                result['详情'].append(f"资金: {capital_result.get('detail')}"
                                      f"(得分{capital_result.get('score', 0)})")
            resonance = _res.calculate_resonance_score(
                individual_result=individual_result, market_result=market_result,
                industry_result=industry_result, capital_result=capital_result)
            result['共振评分'] = resonance.get('score', 0)
            result['共振级别'] = resonance.get('level', '无共振')
            result['共振建议'] = resonance.get('advice', '')
            result['真三振'] = resonance.get('is_true_three_strike', False)
            result['三级共振'] = bool(result['个股趋势'] and result['行业趋势']
                                      and result['大盘趋势'])
            for d in resonance.get('details', []):
                result['详情'].append(d)
            if result['真三振']:
                result['详情'].append('✅ 真三振（三级）成立：四维共振+资金活跃+大盘非高位')
            elif result['三级共振']:
                result['详情'].append('✅ 三级共振成立（未达真三振：资金/位置条件不足）')
            else:
                result['详情'].append('❌ 三振共振不成立')
            return result
        except Exception as e:
            self.logger.error(f'❌ 三振共振分析异常: {e}')
            return {'三级共振': False, '共振评分': 0, '共振级别': '异常', '真三振': False,
                    '详情': [f'分析异常: {e}']}

    # ---------- 周线锚定 ----------
    def weekly_anchor_check(self, weekly_df: Optional[pd.DataFrame],
                            lookback: int = 60) -> Dict[str, Any]:
        """周线收盘价 > 60周均线 且 均线斜率 >= 0。"""
        result = {'锚定': False, '原因': '周线数据不足', '收盘价': None,
                  'MA60_W': None, '斜率': 0.0}
        try:
            if weekly_df is None or weekly_df.empty or len(weekly_df) < 10:
                return result
            df = weekly_df.copy()
            close_col = '收盘价' if '收盘价' in df.columns else 'close'
            if close_col not in df.columns:
                return result
            win = min(lookback, len(df))
            df['MA60_W'] = df[close_col].rolling(win).mean()
            latest = df.iloc[-1]
            ma60 = latest['MA60_W']
            if pd.isna(ma60):
                result['原因'] = '60周均线数据不足'
                return result
            above = float(latest[close_col]) > float(ma60)
            slope = 0.0
            if len(df) >= 4 and pd.notna(df['MA60_W'].iloc[-4]):
                slope = float(ma60) - float(df['MA60_W'].iloc[-4])
            slope_ok = slope >= -1e-09
            anchored = above and slope_ok
            result.update({'锚定': anchored, '收盘价': round(float(latest[close_col]), 3),
                           'MA60_W': round(float(ma60), 3), '斜率': round(slope, 3),
                           '原因': (f'周线{float(latest[close_col]):.2f} > 60周线'
                                    f'{float(ma60):.2f}（斜率{slope:+.3f}）' if anchored
                                    else '周线跌破60周线或60周均线拐头向下')})
            return result
        except Exception as e:
            self.logger.error(f'❌ 周线锚定分析异常: {e}')
            return {'锚定': False, '原因': f'分析异常: {e}', 'MA60_W': None, '斜率': 0.0}

    # ---------- 破五反五 ----------
    def check_po5_fan5(self, df: pd.DataFrame, lookback: int = 5) -> Dict[str, Any]:
        """破五反五：跌破MA5后 ≤2 日内收回且 MA20 向上。"""
        result = {'破五反五': False, '破五天数': None, 'MA20斜率': None, '原因': '数据不足'}
        try:
            if df is None or df.empty or len(df) < 5:
                return result
            d = df.copy()
            close_col = '收盘价' if '收盘价' in d.columns else 'close'
            if close_col not in d.columns:
                return result
            if 'MA5' not in d.columns:
                d['MA5'] = d[close_col].rolling(5).mean()
            if 'MA20' not in d.columns:
                d['MA20'] = d[close_col].rolling(20).mean()
            recent = d.iloc[-lookback:]
            broke_mask = recent[close_col] < recent['MA5']
            if not broke_mask.any():
                return {**result, '原因': '未破五（股价始终在MA5上方）'}
            last_broke_idx = recent.index[broke_mask][-1]
            latest = d.iloc[-1]
            if float(latest[close_col]) <= float(latest['MA5']):
                return {**result, '原因': '仍处破五状态（未收回MA5）'}
            days_since_break = len(d.loc[last_broke_idx:]) - 1
            ma20_slope = None
            if len(d) >= 3 and pd.notna(d['MA20'].iloc[-1]) and pd.notna(d['MA20'].iloc[-3]):
                ma20_slope = float(d['MA20'].iloc[-1]) - float(d['MA20'].iloc[-3])
            slope_up = ma20_slope is not None and ma20_slope > 0
            valid = days_since_break <= 2 and slope_up
            return {'破五反五': valid, '破五天数': int(days_since_break),
                    'MA20斜率': round(ma20_slope, 3) if ma20_slope is not None else None,
                    '原因': (f'破五后{int(days_since_break)}日内收回且MA20向上'
                            f'（斜率{ma20_slope:+.3f}）' if valid
                            else f'破五后{int(days_since_break)}日收回但MA20未向上'
                                 f'（斜率{ma20_slope:+.3f}）')}
        except Exception as e:
            self.logger.error(f'❌ 破五反五分析异常: {e}')
            return {**result, '原因': f'分析异常: {e}'}

    # ---------- 主升浪信号 ----------
    def main_bull_wave_signal(self, data: pd.DataFrame,
                              weekly_df: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
        """主升浪信号：年线滤网 + 周线锚定 + (价在MA5上 或 破五反五)。"""
        try:
            basic_passed, basic_errors = self.basic_filter(data)
            weekly = (self.weekly_anchor_check(weekly_df) if weekly_df is not None
                      else {'锚定': True, '原因': '无周线数据（跳过周线锚定）'})
            po5 = self.check_po5_fan5(data)
            price_above_ma5 = False
            if ('收盘价' in data.columns and 'MA5' in data.columns and len(data) > 0
                    and pd.notna(data.iloc[-1]['收盘价']) and pd.notna(data.iloc[-1]['MA5'])):
                price_above_ma5 = bool(data.iloc[-1]['收盘价'] > data.iloc[-1]['MA5'])
            is_main = basic_passed and weekly['锚定'] and (price_above_ma5 or po5['破五反五'])
            return {'主升浪信号': bool(is_main), '年线滤网': bool(basic_passed),
                    '周线锚定': bool(weekly['锚定']), '破五反五': bool(po5['破五反五']),
                    '详情': [*basic_errors[:3], weekly['原因'], po5['原因']],
                    '依据': {'年线滤网': basic_errors if not basic_passed
                            else ['年线多头滤网通过'], '周线': weekly, '破五反五': po5}}
        except Exception as e:
            self.logger.error(f'❌ 主升浪信号分析异常: {e}')
            return {'主升浪信号': False, '年线滤网': False, '周线锚定': False,
                    '破五反五': False, '详情': [f'分析异常: {e}']}

    # ---------- 综合信号（评分入口） ----------
    def comprehensive_signal_analysis(self, data: pd.DataFrame,
                                      weekly_data: Optional[pd.DataFrame] = None,
                                      market_data: Dict = None,
                                      industry_data: Dict = None,
                                      industry_trend: bool = None) -> Dict[str, Any]:
        """三大心法综合信号 → 综合评分/操作建议。评分 = 共振分*0.6 + 主升浪40*0.4。"""
        try:
            basic = self.basic_filter(data)
            if not basic[0]:
                return {'综合评分': 0.0, '操作建议': '观望（未通过年线滤网）',
                        '主升浪信号': False, '真三振': False, '年线滤网': False,
                        '周线锚定': False, '破五反五': False, '共振评分': 0.0,
                        '共振级别': '无共振', '详情': [f'年线滤网未通过: {basic[1][:3]}']}
            main_wave = self.main_bull_wave_signal(data, weekly_data)
            resonance = self.three_resonance_analysis(data, market_data, industry_trend,
                                                      industry_data=industry_data)
            r_score = float(resonance.get('共振评分', 0) or 0)
            score = r_score * 0.6 + (40 if main_wave['主升浪信号'] else 0) * 0.4
            if resonance.get('真三振') and main_wave['主升浪信号']:
                advice = '强烈关注（真三振 + 主升浪）'
            elif resonance.get('真三振'):
                advice = '重点关注（真三振）'
            elif main_wave['主升浪信号']:
                advice = '可关注（主升浪持股期）'
            else:
                advice = resonance.get('共振建议', '观望为主')
            return {'综合评分': round(score, 1), '操作建议': advice,
                    '主升浪信号': main_wave['主升浪信号'], '年线滤网': main_wave['年线滤网'],
                    '周线锚定': main_wave['周线锚定'], '破五反五': main_wave['破五反五'],
                    '真三振': resonance.get('真三振', False),
                    '共振评分': r_score, '共振级别': resonance.get('共振级别', '无共振'),
                    '资金活跃': resonance.get('资金活跃', False),
                    '最强板块': resonance.get('最强板块', []),
                    '详情': main_wave['详情'] + resonance.get('详情', [])}
        except Exception as e:
            self.logger.error(f'❌ 综合信号分析异常: {e}')
            return {'综合评分': 0.0, '操作建议': '分析异常', '主升浪信号': False,
                    '真三振': False, '详情': [f'分析异常: {e}']}

    # ---------- 主升浪状态 ----------
    def main_bull_wave_analysis(self, data: pd.DataFrame) -> Dict[str, Any]:
        """主升浪状态：持股期/空中加油/强势上升/观望。"""
        try:
            result = {'主升浪状态': '未知', '持股状态': False, '空中加油': False,
                      'MA5斜率': 0, '判定依据': [], '详情': []}
            required_cols = ['收盘价', 'MA5', 'MA20', '量比', '换手率']
            missing_cols = [col for col in required_cols if col not in data.columns]
            if missing_cols:
                result['详情'].append(f'缺少必要列: {missing_cols}')
                return result
            latest_data = data.iloc[-1]
            if len(data) >= 5:
                ma5_values = data['MA5'].tail(5).dropna()
                if len(ma5_values) >= 2:
                    slope = (ma5_values.iloc[-1] - ma5_values.iloc[0]) / 4
                    result['MA5斜率'] = slope
                    if slope > 0.5:
                        result['详情'].append('MA5斜率强劲，处于加速段')
                        result['判定依据'].append(f'MA5斜率{slope:.2f}>0.5，加速上行')
                    elif slope > 0:
                        result['详情'].append('MA5斜率温和，处于上升段')
                        result['判定依据'].append(f'MA5斜率{slope:.2f}>0，温和上行')
                    else:
                        result['详情'].append('MA5斜率平缓或下降')
                        result['判定依据'].append(f'MA5斜率{slope:.2f}<=0，未上行')
            above_ma5_count = 0
            if (pd.notna(latest_data['收盘价']) and pd.notna(latest_data['MA5'])
                    and latest_data['收盘价'] > latest_data['MA5']):
                recent_5_days = data.tail(5)
                above_ma5_count = sum(
                    1 for i in range(len(recent_5_days))
                    if (pd.notna(recent_5_days.iloc[i]['收盘价'])
                        and pd.notna(recent_5_days.iloc[i]['MA5'])
                        and recent_5_days.iloc[i]['收盘价'] > recent_5_days.iloc[i]['MA5']))
                if above_ma5_count >= 3:
                    result['持股状态'] = True
                    result['主升浪状态'] = '主升持股期'
                    result['详情'].append('✅ 主升持股期：股价沿MA5上涨')
                    result['判定依据'].append(
                        f'近5日{above_ma5_count}日收盘价>MA5，站稳MA5')
            amplitude = None
            if (pd.notna(latest_data['收盘价']) and pd.notna(latest_data['MA20'])
                    and latest_data['收盘价'] > latest_data['MA20']):
                recent_20_days = data.tail(20)
                if len(recent_20_days) >= 10:
                    high_20 = recent_20_days['最高价'].max()
                    low_20 = recent_20_days['最低价'].min()
                    avg_20 = recent_20_days['收盘价'].mean()
                    if (pd.notna(high_20) and pd.notna(low_20) and pd.notna(avg_20)
                            and avg_20 > 0):
                        amplitude = (high_20 - low_20) / avg_20 * 100
                    if amplitude is not None and amplitude < 15:
                        if '量比' in data.columns and pd.notna(latest_data['量比']):
                            if latest_data['量比'] < 1.0:
                                result['空中加油'] = True
                                result['主升浪状态'] = '空中加油'
                                result['详情'].append('✅ 空中加油形态：缩量横盘整理')
                                result['判定依据'].append(
                                    f"20日振幅{amplitude:.1f}%<15%且量比"
                                    f"{latest_data['量比']:.2f}<1，缩量横盘")
            if result['持股状态']:
                result['主升浪状态'] = '主升持股期'
            elif result['空中加油']:
                result['主升浪状态'] = '空中加油'
            elif result['MA5斜率'] > 0:
                result['主升浪状态'] = '强势上升'
                if not result['判定依据']:
                    result['判定依据'].append('MA5斜率>0，处于上升趋势')
            else:
                result['主升浪状态'] = '观望'
                result['判定依据'].append('MA5斜率<=0且未站稳MA5，趋势不明')
            result['判定依据'].insert(0, f"判定结果: {result['主升浪状态']}")
            return result
        except Exception as e:
            self.logger.error(f'❌ 主升浪分析异常: {e}')
            return {'主升浪状态': '异常', '详情': [f'分析异常: {e}']}

    # ---------- 多周期箱体 ----------
    def _analyze_cycle_box(self, cycle_data: pd.DataFrame, cycle_name: str,
                           lookback: int = 20, adaptive_n: int = None,
                           avg_turnover: float = None) -> Dict[str, Any]:
        result = {'周期': cycle_name, '上沿': None, '下沿': None, '当前价': None,
                  '位置': '未知', '状态': '未知', '距上沿': None, '距下沿': None,
                  '自适应周期': None, '详情': []}
        try:
            if cycle_data is None or cycle_data.empty or len(cycle_data) < 5:
                result['详情'].append(f'{cycle_name}数据不足，无法分析箱体')
                return result
            col_high = '最高价' if '最高价' in cycle_data.columns else 'high'
            col_low = '最低价' if '最低价' in cycle_data.columns else 'low'
            col_close = '收盘价' if '收盘价' in cycle_data.columns else 'close'
            recent = cycle_data.tail(lookback)
            box_high = recent[col_high].max()
            box_low = recent[col_low].min()
            current = cycle_data.iloc[-1][col_close]
            if pd.isna(box_high) or pd.isna(box_low) or pd.isna(current):
                result['详情'].append(f'{cycle_name}箱体数据缺失')
                return result
            box_high = float(box_high)
            box_low = float(box_low)
            current = float(current)
            result['上沿'] = round(box_high, 2)
            result['下沿'] = round(box_low, 2)
            result['当前价'] = round(current, 2)
            result['距上沿'] = round((current - box_high) / box_high * 100, 2) if box_high > 0 else None
            result['距下沿'] = round((current - box_low) / box_low * 100, 2) if box_low > 0 else None
            tolerance = 0.02
            if current > box_high * (1 + tolerance):
                result['位置'] = '上沿上方'
            elif current >= box_high * (1 - tolerance):
                result['位置'] = '上沿附近'
            elif current < box_low * (1 - tolerance):
                result['位置'] = '下沿下方'
            elif current <= box_low * (1 + tolerance):
                result['位置'] = '下沿附近'
            else:
                result['位置'] = '箱体内'
            if result['位置'] == '上沿上方':
                prev = cycle_data.iloc[-2][col_close]
                if not pd.isna(prev) and float(prev) <= box_high:
                    result['状态'] = '突破上沿'
                    result['详情'].append(
                        f'{cycle_name}突破上沿: 收盘{current:.2f} > 箱体上沿{box_high:.2f}'
                        f'（前值{float(prev):.2f} ≤ 上沿）')
                else:
                    result['状态'] = '上沿上方'
                    result['详情'].append(
                        f'{cycle_name}运行于上沿上方: 收盘{current:.2f} > 上沿{box_high:.2f}')
            elif result['位置'] == '上沿附近':
                result['状态'] = '回踩上沿'
                result['详情'].append(
                    f"{cycle_name}回踩上沿: 收盘{current:.2f} 贴近箱体上沿{box_high:.2f}"
                    f"（回踩不破，距上沿{result['距上沿']:.1f}%）")
            elif result['位置'] == '下沿附近':
                result['状态'] = '跌到下沿'
                result['详情'].append(
                    f"{cycle_name}跌到下沿: 收盘{current:.2f} 贴近箱体下沿{box_low:.2f}"
                    f"（距下沿{result['距下沿']:.1f}%）")
            elif result['位置'] == '下沿下方':
                result['状态'] = '跌破下沿'
                result['详情'].append(
                    f'{cycle_name}跌破下沿: 收盘{current:.2f} < 箱体下沿{box_low:.2f}')
            else:
                result['状态'] = '箱体内震荡'
                result['详情'].append(
                    f'{cycle_name}箱体内震荡: 收盘{current:.2f} 处于箱体'
                    f'[{box_low:.2f}, {box_high:.2f}]内')
            result['详情'].append(
                f'{cycle_name}箱体(近{lookback}期): 下沿{box_low:.2f} ~ 上沿{box_high:.2f}')
            if adaptive_n is not None:
                result['自适应周期'] = {'adaptive_n': adaptive_n, 'avg_turnover': avg_turnover}
                result['详情'].append(
                    f'{cycle_name}自适应周期: N={adaptive_n}日（近20日均换手{avg_turnover}%）'
                    if avg_turnover is not None else f'{cycle_name}自适应周期: N={adaptive_n}日')
        except Exception as e:
            result['详情'].append(f'{cycle_name}箱体分析异常: {e}')
        return result

    # ---------- 平台突破 ----------
    def platform_breakthrough_analysis(self, data: pd.DataFrame, stock_code: str = '',
                                       weekly_data: pd.DataFrame = None,
                                       monthly_data: pd.DataFrame = None) -> Dict[str, Any]:
        """平台突破：自适应 VAP-ATR + 周/月箱体 + 固定20日箱体 + 突破/买横信号。"""
        try:
            result = {'平台状态': '未知', '突破信号': False, '买横信号': False,
                      '平台范围': None, '固定箱体': None, '自适应平台': None,
                      '周线箱体': None, '月线箱体': None, '详情': []}
            required_cols = ['收盘价', 'MA20', '成交量', '量比', 'MACD', 'MACD_Signal']
            missing_cols = [col for col in required_cols if col not in data.columns]
            if missing_cols:
                result['详情'].append(f'缺少必要列: {missing_cols}')
                return result
            try:
                adaptive = analyze_adaptive_platform(data, stock_code=stock_code)
                if adaptive.get('平台范围'):
                    result['自适应平台'] = adaptive
                    result['平台范围'] = adaptive.get('平台范围')
                    if adaptive.get('突破信号'):
                        result['突破信号'] = True
                        result['平台状态'] = '突破确认'
                        result['详情'].append('✅ 自适应突破：收盘价>ATR上轨且阳线且重心>0.5')
                    elif adaptive.get('POC') is not None:
                        result['平台状态'] = '横盘整理'
                        result['详情'].append(
                            f"自适应平台: POC={adaptive['POC']}, "
                            f"上轨={adaptive['自适应上轨']}, 下轨={adaptive['自适应下轨']}")
            except Exception as e:
                result['详情'].append(f'自适应平台分析异常(降级固定箱体): {e}')
            adaptive_n = None
            avg_turnover = None
            if (result.get('自适应平台') and result['自适应平台'].get('自适应周期')):
                ap_cycle = result['自适应平台']['自适应周期']
                adaptive_n = ap_cycle.get('adaptive_n')
                avg_turnover = ap_cycle.get('avg_turnover')
            if weekly_data is not None:
                result['周线箱体'] = self._analyze_cycle_box(
                    weekly_data, '周线', lookback=20, adaptive_n=adaptive_n,
                    avg_turnover=avg_turnover)
            if monthly_data is not None:
                result['月线箱体'] = self._analyze_cycle_box(
                    monthly_data, '月线', lookback=20, adaptive_n=adaptive_n,
                    avg_turnover=avg_turnover)
            cycle_states = []
            for key, name in [('周线箱体', '周线'), ('月线箱体', '月线')]:
                box = result.get(key)
                if box and box.get('状态') not in ('未知',):
                    cycle_states.append(f"{name}:{box.get('状态')}({box.get('上沿')})")
            if cycle_states:
                result['多周期箱体状态'] = ' | '.join(cycle_states)
                result['详情'].append(f"📊 多周期箱体: {' | '.join(cycle_states)}")
            recent_20_days = data.tail(20)
            platform_high = recent_20_days['最高价'].max()
            platform_low = recent_20_days['最低价'].min()
            if pd.notna(platform_high) and pd.notna(platform_low):
                fixed_box = {'上沿': round(float(platform_high), 2),
                             '下沿': round(float(platform_low), 2),
                             '周期': 20, '方式': '固定箱体'}
                result['固定箱体'] = fixed_box
                if not result.get('平台范围'):
                    result['平台范围'] = fixed_box
                result['详情'].append(f'平台箱体(近20日): {platform_low:.2f} ~ {platform_high:.2f}')
            if len(recent_20_days) >= 10:
                high_price = platform_high
                low_price = platform_low
                avg_price = recent_20_days['收盘价'].mean()
                if (pd.notna(high_price) and pd.notna(low_price) and pd.notna(avg_price)
                        and avg_price > 0):
                    amplitude = (high_price - low_price) / avg_price * 100
                    if amplitude < 15:
                        result['平台状态'] = '横盘整理'
                        result['详情'].append(f'横盘整理：振幅{amplitude:.1f}%')
                        latest_data = data.iloc[-1]
                        if (pd.notna(latest_data['收盘价']) and pd.notna(latest_data['MA20'])
                                and latest_data['收盘价'] > latest_data['MA20']):
                            if '量比' in data.columns and pd.notna(latest_data['量比']):
                                if latest_data['量比'] > 1.5:
                                    result['详情'].append(
                                        f"放量突破：量比{latest_data['量比']:.2f}>1.5, "
                                        f"突破箱体上沿{platform_high:.2f}")
                                    if ('MACD_信号' in data.columns
                                            and pd.notna(latest_data['MACD_信号'])
                                            and latest_data['MACD_信号'] == 1):
                                        result['突破信号'] = True
                                        result['平台状态'] = '突破确认'
                                        result['详情'].append('✅ MACD零轴上金叉，突破确认')
                            if not result['突破信号']:
                                current_price = latest_data['收盘价']
                                if pd.notna(platform_low) and pd.notna(current_price):
                                    distance_from_low = (
                                        (current_price - platform_low) / platform_low * 100)
                                    if distance_from_low < 5:
                                        result['买横信号'] = True
                                        result['平台状态'] = '买横机会'
                                        result['详情'].append(
                                            f'✅ 买横信号：距平台下沿{platform_low:.2f}仅'
                                            f'{distance_from_low:.1f}%')
            return result
        except Exception as e:
            self.logger.error(f'❌ 平台突破分析异常: {e}')
            return {'平台状态': '异常', '详情': [f'分析异常: {e}']}

    # ---------- 主升浪8项 ----------
    def main_bull_wave_checklist(self, data: pd.DataFrame,
                                 industry_trend: bool = None) -> Dict[str, Any]:
        """主升浪8项指标对比表。"""
        try:
            checklist = {'长期横盘3个月以上': False, '60日均线开始向上': False,
                         '股价突破平台': False, '放量超20日均量2倍': False,
                         '回踩不破+MACD零轴金叉': False, 'RSI>50继续走强': False,
                         '主力资金连续流入': False, '行业板块同步走强': False,
                         '平台范围': None, '详情': []}
            if data is None or data.empty:
                checklist['详情'].append('数据为空')
                checklist['满足数量'] = 0
                return checklist
            latest = data.iloc[-1]
            if len(data) >= 60:
                recent_60 = data.tail(60)
                high_60 = recent_60['最高价'].max()
                low_60 = recent_60['最低价'].min()
                avg_60 = recent_60['收盘价'].mean()
                if (pd.notna(high_60) and pd.notna(low_60) and pd.notna(avg_60)
                        and avg_60 > 0):
                    amplitude_60 = (high_60 - low_60) / avg_60 * 100
                    if amplitude_60 < 25:
                        checklist['长期横盘3个月以上'] = True
                        checklist['详情'].append(f'60日振幅{amplitude_60:.1f}%（<25%）')
                    else:
                        checklist['详情'].append(f'60日振幅{amplitude_60:.1f}%（>=25%，未横盘）')
            else:
                checklist['详情'].append('数据不足60日，无法判断横盘')
            if 'MA60' in data.columns and len(data) >= 65:
                ma60_series = data['MA60'].dropna()
                if len(ma60_series) >= 6:
                    ma60_slope = ma60_series.iloc[-1] - ma60_series.iloc[-6]
                    if ma60_slope > 0:
                        checklist['60日均线开始向上'] = True
                        checklist['详情'].append(f'MA60近5日上行{ma60_slope:.2f}')
                    else:
                        checklist['详情'].append(f'MA60近5日{ma60_slope:.2f}（未向上）')
            else:
                checklist['详情'].append('MA60数据不足')
            if len(data) >= 21:
                recent_20_high = data['最高价'].tail(20).max()
                recent_20_low = data['最低价'].tail(20).min()
                if pd.notna(recent_20_high) and pd.notna(recent_20_low):
                    checklist['平台范围'] = {'上沿': round(float(recent_20_high), 2),
                                            '下沿': round(float(recent_20_low), 2),
                                            '周期': 20}
                if pd.notna(recent_20_high) and pd.notna(latest['收盘价']):
                    if latest['收盘价'] >= recent_20_high:
                        checklist['股价突破平台'] = True
                        checklist['详情'].append(
                            f"收盘价{latest['收盘价']:.2f}突破近20日箱体上沿"
                            f"{recent_20_high:.2f}(箱体{recent_20_low:.2f}-"
                            f"{recent_20_high:.2f})")
                    else:
                        checklist['详情'].append(
                            f'未突破近20日箱体上沿{recent_20_high:.2f}'
                            f'(箱体{recent_20_low:.2f}-{recent_20_high:.2f})')
            else:
                checklist['详情'].append('数据不足20日')
            if '成交量' in data.columns and len(data) >= 21:
                avg_vol_20 = data['成交量'].tail(20).mean()
                if (pd.notna(avg_vol_20) and avg_vol_20 > 0
                        and pd.notna(latest['成交量'])):
                    vol_ratio_20 = latest['成交量'] / avg_vol_20
                    if vol_ratio_20 >= 2.0:
                        checklist['放量超20日均量2倍'] = True
                        checklist['详情'].append(f'当日量/20日均量={vol_ratio_20:.2f}（>=2倍）')
                    else:
                        checklist['详情'].append(f'当日量/20日均量={vol_ratio_20:.2f}（<2倍）')
            else:
                checklist['详情'].append('成交量数据不足')
            if len(data) >= 2:
                yesterday = data.iloc[-2]
                platform_low = (data['最低价'].tail(20).min() if len(data) >= 20
                                else data['最低价'].min())
                platform_high = (data['最高价'].tail(20).max() if len(data) >= 20
                                 else data['最高价'].max())
                pullback_ok = False
                if (pd.notna(yesterday['最低价']) and pd.notna(platform_low)
                        and platform_low > 0):
                    pullback_ok = yesterday['最低价'] >= platform_low * 0.98
                macd_golden = False
                if all(c in data.columns for c in ['MACD', 'MACD_Signal']):
                    macd_now = latest.get('MACD', 0)
                    signal_now = latest.get('MACD_Signal', 0)
                    macd_prev = yesterday.get('MACD', 0)
                    signal_prev = yesterday.get('MACD_Signal', 0)
                    near_zero = (abs(macd_now) < latest['收盘价'] * 0.01
                                 if pd.notna(latest['收盘价']) else False)
                    golden_cross = macd_now > signal_now and macd_prev <= signal_prev
                    if golden_cross or (macd_now > signal_now and near_zero):
                        macd_golden = True
                if pullback_ok and macd_golden:
                    checklist['回踩不破+MACD零轴金叉'] = True
                    checklist['详情'].append(
                        f'回踩平台支撑{platform_low:.2f}未破(箱体{platform_low:.2f}-'
                        f'{platform_high:.2f})，MACD零轴附近金叉')
                else:
                    checklist['详情'].append(
                        f"回踩{('未破' if pullback_ok else '破位')}平台支撑{platform_low:.2f}"
                        f"(箱体{platform_low:.2f}-{platform_high:.2f})/"
                        f"MACD{('金叉' if macd_golden else '未金叉')}")
            else:
                checklist['详情'].append('数据不足2日')
            if 'RSI' in data.columns and len(data) >= 6:
                rsi_now = latest.get('RSI', 0)
                rsi_5ago = data['RSI'].iloc[-6] if len(data) >= 6 else rsi_now
                if pd.notna(rsi_now) and pd.notna(rsi_5ago):
                    if rsi_now > 50 and rsi_now >= rsi_5ago:
                        checklist['RSI>50继续走强'] = True
                        checklist['详情'].append(f'RSI={rsi_now:.1f}（>50且上行）')
                    else:
                        checklist['详情'].append(
                            f"RSI={rsi_now:.1f}（{('<50' if rsi_now <= 50 else '走弱')}）")
            else:
                checklist['详情'].append('RSI数据不足')
            if len(data) >= 4 and '成交量' in data.columns and '涨跌幅' in data.columns:
                recent_3 = data.tail(3)
                inflows = 0
                for i in range(len(recent_3)):
                    row = recent_3.iloc[i]
                    pct_chg = row.get('涨跌幅', 0)
                    if pd.notna(pct_chg) and pct_chg > 0:
                        inflows += 1
                if inflows >= 2:
                    checklist['主力资金连续流入'] = True
                    checklist['详情'].append(f'近3日{inflows}日上涨')
                else:
                    checklist['详情'].append(f'近3日仅{inflows}日上涨')
            else:
                checklist['详情'].append('涨跌幅数据不足')
            if industry_trend is True:
                checklist['行业板块同步走强'] = True
                checklist['详情'].append('行业板块走强')
            elif industry_trend is False:
                checklist['详情'].append('行业板块走弱')
            else:
                checklist['详情'].append('行业趋势数据缺失')
            # 只统计 8 项布尔指标（排除 详情/平台范围 dict/汇总字段），
            # 平台范围是 dict（truthy），旧实现被误计为满足项 → 满足数量+1
            bool_items = [k for k, v in checklist.items() if isinstance(v, bool)]
            satisfied = sum(1 for k in bool_items if checklist[k])
            checklist['满足数量'] = satisfied
            checklist['满足占比'] = satisfied / len(bool_items) if bool_items else 0
            if satisfied >= 6:
                checklist['综合判断'] = '主升浪高概率'
            elif satisfied >= 4:
                checklist['综合判断'] = '主升浪中概率'
            elif satisfied >= 2:
                checklist['综合判断'] = '关注观察'
            else:
                checklist['综合判断'] = '暂不参与'
            return checklist
        except Exception as e:
            self.logger.error(f'❌ 主升浪指标对比异常: {e}')
            return {'满足数量': 0, '综合判断': '异常', '详情': [f'分析异常: {e}']}

    # ---------- 技术细节 ----------
    def technical_detail_capture(self, data: pd.DataFrame) -> Dict[str, Any]:
        """破五反五 + 筹码集中度。"""
        try:
            result = {'破五反五': False, '筹码集中度': '未知', '筹码集中度数值': None,
                      '筹码趋势': '未知', '详情': []}
            required_cols = ['收盘价', 'MA5', '成交量', '换手率']
            missing_cols = [col for col in required_cols if col not in data.columns]
            if missing_cols:
                result['详情'].append(f'缺少必要列: {missing_cols}')
                return result
            if len(data) >= 2:
                yesterday = data.iloc[-2]
                today = data.iloc[-1]
                if (pd.notna(yesterday['收盘价']) and pd.notna(yesterday['MA5'])
                        and yesterday['收盘价'] < yesterday['MA5']):
                    if (pd.notna(today['收盘价']) and pd.notna(today['MA5'])
                            and today['收盘价'] > today['MA5']
                            and pd.notna(today['量比']) and today['量比'] > 1.5):
                        result['破五反五'] = True
                        result['详情'].append('✅ 破五反五：洗盘信号确认')
            if '换手率' in data.columns:
                recent_20_days = data.tail(20)
                avg_turnover = recent_20_days['换手率'].mean()
                if pd.notna(avg_turnover) and avg_turnover > 0:
                    result['筹码集中度数值'] = round(float(avg_turnover), 2)
                    if avg_turnover < 2:
                        result['筹码集中度'] = '高度集中'
                        result['详情'].append(
                            f'筹码高度集中（近20日平均换手率{avg_turnover:.2f}%<2%）')
                    elif avg_turnover < 5:
                        result['筹码集中度'] = '相对集中'
                        result['详情'].append(
                            f'筹码相对集中（近20日平均换手率{avg_turnover:.2f}%，2-5%）')
                    elif avg_turnover < 10:
                        result['筹码集中度'] = '分散'
                        result['详情'].append(
                            f'筹码分散（近20日平均换手率{avg_turnover:.2f}%，5-10%）')
                    else:
                        result['筹码集中度'] = '高度分散'
                        result['详情'].append(
                            f'筹码高度分散（近20日平均换手率{avg_turnover:.2f}%>10%）')
                else:
                    # 换手率缺失（ths/tdx 常为 None → 0.0）：不伪造"高度集中"
                    result['筹码集中度'] = '数据缺失'
                    result['筹码集中度数值'] = None
                    result['详情'].append('换手率数据缺失，筹码集中度无法判定（不伪造）')
                if (pd.notna(avg_turnover) and avg_turnover > 0
                        and len(recent_20_days) >= 10):
                    recent_10_turnover = recent_20_days.tail(10)['换手率'].mean()
                    early_10_turnover = recent_20_days.head(10)['换手率'].mean()
                    if (pd.notna(recent_10_turnover) and pd.notna(early_10_turnover)):
                        if recent_10_turnover < early_10_turnover:
                            result['筹码趋势'] = '趋于集中'
                            result['详情'].append(
                                f'筹码呈集中趋势（近10日换手{recent_10_turnover:.2f}%'
                                f'<前10日{early_10_turnover:.2f}%）')
                        else:
                            result['筹码趋势'] = '趋于分散'
                            result['详情'].append(
                                f'筹码呈分散趋势（近10日换手{recent_10_turnover:.2f}%'
                                f'>=前10日{early_10_turnover:.2f}%）')
                elif pd.notna(avg_turnover) and avg_turnover <= 0:
                    result['筹码趋势'] = '未知（数据缺失）'
            return result
        except Exception as e:
            self.logger.error(f'❌ 技术细节捕捉异常: {e}')
            return {'破五反五': False, '筹码集中度': '异常', '详情': [f'分析异常: {e}']}
