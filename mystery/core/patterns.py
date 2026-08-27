"""mystery.core.patterns — 形态识别（迁自 pattern_recognition.py）。纯函数，零 IO。"""
from __future__ import annotations

import logging
from typing import Any, Dict

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class PatternRecognition:
    """头肩 / 双重 / 三角形 / 楔形 形态识别。"""

    def __init__(self):
        self.logger = logger

    def recognize_head_and_shoulders(self, data: pd.DataFrame) -> Dict[str, Any]:
        try:
            result = {'形态类型': '无', '形态状态': '未知', '左肩位置': None, '头部位置': None,
                      '右肩位置': None, '颈线位置': None, '可靠性': 0, '目标价位': None,
                      '详情': []}
            if len(data) < 60:
                result['详情'].append('数据不足，无法识别头肩形态')
                return result
            recent_data = data.tail(60)
            highs, lows = [], []
            for i in range(2, len(recent_data) - 2):
                if (recent_data.iloc[i]['最高价'] > recent_data.iloc[i - 1]['最高价']
                        and recent_data.iloc[i]['最高价'] > recent_data.iloc[i + 1]['最高价']
                        and recent_data.iloc[i]['最高价'] > recent_data.iloc[i - 2]['最高价']
                        and recent_data.iloc[i]['最高价'] > recent_data.iloc[i + 2]['最高价']):
                    highs.append(i)
                if (recent_data.iloc[i]['最低价'] < recent_data.iloc[i - 1]['最低价']
                        and recent_data.iloc[i]['最低价'] < recent_data.iloc[i + 1]['最低价']
                        and recent_data.iloc[i]['最低价'] < recent_data.iloc[i - 2]['最低价']
                        and recent_data.iloc[i]['最低价'] < recent_data.iloc[i + 2]['最低价']):
                    lows.append(i)
            if len(highs) >= 3:
                highs_sorted = sorted(highs)
                for i in range(len(highs_sorted) - 2):
                    left_shoulder, head, right_shoulder = highs_sorted[i:i + 3]
                    if head - left_shoulder > 5 and right_shoulder - head > 5:
                        lsh = recent_data.iloc[left_shoulder]['最高价']
                        hh = recent_data.iloc[head]['最高价']
                        rsh = recent_data.iloc[right_shoulder]['最高价']
                        if hh > lsh and hh > rsh and i + 1 < len(lows):
                            neck_line_price = recent_data.iloc[lows[i + 1]]['最低价']
                            result.update({'形态类型': '头肩顶', '形态状态': '形成中',
                                           '左肩位置': left_shoulder, '头部位置': head,
                                           '右肩位置': right_shoulder,
                                           '颈线位置': neck_line_price, '可靠性': 70,
                                           '目标价位': neck_line_price - (hh - neck_line_price)})
                            result['详情'].append(
                                f'头肩顶形态形成: 左肩={left_shoulder}, 头部={head}, '
                                f'右肩={right_shoulder}')
                            result['详情'].append(
                                f'颈线位置: {neck_line_price:.2f}, '
                                f"目标价位: {result['目标价位']:.2f}")
                            break
            if len(lows) >= 3 and result['形态类型'] == '无':
                lows_sorted = sorted(lows)
                for i in range(len(lows_sorted) - 2):
                    left_shoulder, head, right_shoulder = lows_sorted[i:i + 3]
                    if head - left_shoulder > 5 and right_shoulder - head > 5:
                        lsl = recent_data.iloc[left_shoulder]['最低价']
                        hl = recent_data.iloc[head]['最低价']
                        rsl = recent_data.iloc[right_shoulder]['最低价']
                        if hl < lsl and hl < rsl and i + 1 < len(highs):
                            neck_line_price = recent_data.iloc[highs[i + 1]]['最高价']
                            result.update({'形态类型': '头肩底', '形态状态': '形成中',
                                           '左肩位置': left_shoulder, '头部位置': head,
                                           '右肩位置': right_shoulder,
                                           '颈线位置': neck_line_price, '可靠性': 70,
                                           '目标价位': neck_line_price + (neck_line_price - hl)})
                            result['详情'].append(
                                f'头肩底形态形成: 左肩={left_shoulder}, 头部={head}, '
                                f'右肩={right_shoulder}')
                            result['详情'].append(
                                f'颈线位置: {neck_line_price:.2f}, '
                                f"目标价位: {result['目标价位']:.2f}")
                            break
            return result
        except Exception as e:
            self.logger.error(f'❌ 识别头肩形态异常: {e}')
            return {'形态类型': '异常', '详情': [f'识别异常: {e}']}

    def recognize_double_top_bottom(self, data: pd.DataFrame) -> Dict[str, Any]:
        try:
            result = {'形态类型': '无', '形态状态': '未知', '第一个顶/底位置': None,
                      '第二个顶/底位置': None, '颈线位置': None, '可靠性': 0,
                      '目标价位': None, '详情': []}
            if len(data) < 40:
                result['详情'].append('数据不足，无法识别双重形态')
                return result
            recent_data = data.tail(40)
            highs = [i for i in range(2, len(recent_data) - 2)
                     if (recent_data.iloc[i]['最高价'] > recent_data.iloc[i - 1]['最高价']
                         and recent_data.iloc[i]['最高价'] > recent_data.iloc[i + 1]['最高价'])]
            if len(highs) >= 2:
                for i in range(len(highs) - 1):
                    first_high, second_high = highs[i], highs[i + 1]
                    if 5 <= second_high - first_high <= 15:
                        first_price = recent_data.iloc[first_high]['最高价']
                        second_price = recent_data.iloc[second_high]['最高价']
                        if abs(first_price - second_price) / first_price * 100 < 5:
                            low_idx = (first_high + second_high) // 2
                            if low_idx < len(recent_data):
                                neck_price = recent_data.iloc[low_idx]['最低价']
                                result.update({'形态类型': '双重顶', '形态状态': '形成中',
                                               '第一个顶/底位置': first_high,
                                               '第二个顶/底位置': second_high,
                                               '颈线位置': neck_price, '可靠性': 75,
                                               '目标价位': neck_price - (first_price - neck_price)})
                                result['详情'].append(
                                    f'双重顶形态形成: 第一个顶={first_high}, '
                                    f'第二个顶={second_high}')
                                result['详情'].append(
                                    f'颈线位置: {neck_price:.2f}, '
                                    f"目标价位: {result['目标价位']:.2f}")
                                break
            lows = [i for i in range(2, len(recent_data) - 2)
                    if (recent_data.iloc[i]['最低价'] < recent_data.iloc[i - 1]['最低价']
                        and recent_data.iloc[i]['最低价'] < recent_data.iloc[i + 1]['最低价'])]
            if len(lows) >= 2 and result['形态类型'] == '无':
                for i in range(len(lows) - 1):
                    first_low, second_low = lows[i], lows[i + 1]
                    if 5 <= second_low - first_low <= 15:
                        first_price = recent_data.iloc[first_low]['最低价']
                        second_price = recent_data.iloc[second_low]['最低价']
                        if abs(first_price - second_price) / first_price * 100 < 5:
                            high_idx = (first_low + second_low) // 2
                            if high_idx < len(recent_data):
                                neck_price = recent_data.iloc[high_idx]['最高价']
                                result.update({'形态类型': '双重底', '形态状态': '形成中',
                                               '第一个顶/底位置': first_low,
                                               '第二个顶/底位置': second_low,
                                               '颈线位置': neck_price, '可靠性': 75,
                                               '目标价位': neck_price + (neck_price - first_price)})
                                result['详情'].append(
                                    f'双重底形态形成: 第一个底={first_low}, 第二个底={second_low}')
                                result['详情'].append(
                                    f'颈线位置: {neck_price:.2f}, '
                                    f"目标价位: {result['目标价位']:.2f}")
                                break
            return result
        except Exception as e:
            self.logger.error(f'❌ 识别双重形态异常: {e}')
            return {'形态类型': '异常', '详情': [f'识别异常: {e}']}

    def recognize_triangle_pattern(self, data: pd.DataFrame) -> Dict[str, Any]:
        try:
            result = {'形态类型': '无', '形态状态': '未知', '收敛程度': 0, '突破方向': '未知',
                      '可靠性': 0, '目标价位': None, '详情': []}
            if len(data) < 30:
                result['详情'].append('数据不足，无法识别三角形形态')
                return result
            recent_data = data.tail(30)
            price_range = recent_data['最高价'].max() - recent_data['最低价'].min()
            avg_price = recent_data['收盘价'].mean()
            volatility = price_range / avg_price * 100
            if volatility < 15:
                first_half_volatility = (recent_data.head(15)['最高价'].max()
                                         - recent_data.head(15)['最低价'].min())
                second_half_volatility = (recent_data.tail(15)['最高价'].max()
                                          - recent_data.tail(15)['最低价'].min())
                if second_half_volatility < first_half_volatility:
                    first_high = recent_data.iloc[0]['最高价']
                    last_high = recent_data.iloc[-1]['最高价']
                    first_low = recent_data.iloc[0]['最低价']
                    last_low = recent_data.iloc[-1]['最低价']
                    if last_high < first_high and last_low > first_low:
                        result['形态类型'] = '对称三角形'
                    elif last_high < first_high and last_low < first_low:
                        result['形态类型'] = '下降三角形'
                    elif last_high > first_high and last_low > first_low:
                        result['形态类型'] = '上升三角形'
                    else:
                        result['形态类型'] = '三角形整理'
                    result['形态状态'] = '整理中'
                    result['收敛程度'] = ((first_half_volatility - second_half_volatility)
                                          / first_half_volatility * 100)
                    result['可靠性'] = 60
                    if result['形态类型'] == '上升三角形':
                        result['突破方向'] = '向上'
                    elif result['形态类型'] == '下降三角形':
                        result['突破方向'] = '向下'
                    elif recent_data.iloc[-1]['收盘价'] > recent_data.iloc[-5]['收盘价']:
                        result['突破方向'] = '向上'
                    else:
                        result['突破方向'] = '向下'
                    result['详情'].append(f"三角形整理形态: {result['形态类型']}")
                    result['详情'].append(f"收敛程度: {result['收敛程度']:.1f}%")
                    result['详情'].append(f"预期突破方向: {result['突破方向']}")
            return result
        except Exception as e:
            self.logger.error(f'❌ 识别三角形形态异常: {e}')
            return {'形态类型': '异常', '详情': [f'识别异常: {e}']}

    def recognize_wedge_pattern(self, data: pd.DataFrame) -> Dict[str, Any]:
        try:
            result = {'形态类型': '无', '形态状态': '未知', '倾斜方向': '未知',
                      '可靠性': 0, '目标价位': None, '详情': []}
            if len(data) < 25:
                result['详情'].append('数据不足，无法识别楔形形态')
                return result
            recent_data = data.tail(25)
            highs = recent_data['最高价'].tolist()
            lows = recent_data['最低价'].tolist()
            x = np.arange(len(recent_data))
            high_slope = np.polyfit(x, highs, 1)[0]
            low_slope = np.polyfit(x, lows, 1)[0]
            if abs(high_slope) > 0.01 or abs(low_slope) > 0.01:
                if high_slope > 0 and low_slope > 0:
                    result['形态类型'] = '上升楔形'
                    result['倾斜方向'] = '向上'
                    result['形态状态'] = '看跌'
                elif high_slope < 0 and low_slope < 0:
                    result['形态类型'] = '下降楔形'
                    result['倾斜方向'] = '向下'
                    result['形态状态'] = '看涨'
                else:
                    result['形态类型'] = '混合楔形'
                    result['倾斜方向'] = '混合'
                    result['形态状态'] = '观望'
                convergence = abs(high_slope - low_slope)
                result['可靠性'] = min(convergence * 1000, 80)
                result['详情'].append(f"楔形形态识别: {result['形态类型']}")
                result['详情'].append(f'高点斜率: {high_slope:.4f}, 低点斜率: {low_slope:.4f}')
                result['详情'].append(f"可靠性: {result['可靠性']:.1f}%")
            return result
        except Exception as e:
            self.logger.error(f'❌ 识别楔形形态异常: {e}')
            return {'形态类型': '异常', '详情': [f'识别异常: {e}']}

    def recognize_all_patterns(self, data: pd.DataFrame) -> Dict[str, Any]:
        try:
            result = {'头肩形态': self.recognize_head_and_shoulders(data),
                      '双重形态': self.recognize_double_top_bottom(data),
                      '三角形形态': self.recognize_triangle_pattern(data),
                      '楔形形态': self.recognize_wedge_pattern(data),
                      '主要形态': '无', '形态置信度': 0, '详情': []}
            patterns = ['头肩形态', '双重形态', '三角形形态', '楔形形态']
            max_confidence = 0
            main_pattern = '无'
            for pattern in patterns:
                pattern_result = result[pattern]
                if pattern_result['可靠性'] > max_confidence:
                    max_confidence = pattern_result['可靠性']
                    main_pattern = pattern_result['形态类型']
            result['主要形态'] = main_pattern
            result['形态置信度'] = max_confidence
            if max_confidence > 50:
                result['详情'].append(f'主要形态: {main_pattern} (置信度: {max_confidence:.1f}%)')
            else:
                result['详情'].append('未发现明确的形态')
            return result
        except Exception as e:
            self.logger.error(f'❌ 识别所有形态异常: {e}')
            return {'主要形态': '异常', '详情': [f'识别异常: {e}']}


def recognize_patterns(daily: pd.DataFrame, **kwargs) -> Dict[str, Any]:
    return PatternRecognition().recognize_all_patterns(daily)
