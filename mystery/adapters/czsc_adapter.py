"""mystery.adapters.czsc_adapter — 唯一允许 import czsc 的分析适配。

把 BarSeries → CZSC 对象 → ChanStructure（core 模型），不向 core/web 泄漏
CZSC / BI / ZS / RawBar。读 CZSC_MIN_BI_LEN 等环境变量，不改 czsc 源码。
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional

from ..core.models import Bar, BarSeries, ChanBi, ChanStructure, ChanZs

_CZSC_MIN_BI_LEN = int(os.environ.get("CZSC_MIN_BI_LEN", "7"))


class CzscAdapter:
    """缠论结构适配器。"""

    def __init__(self, min_bi_len: Optional[int] = None):
        self.min_bi_len = min_bi_len or _CZSC_MIN_BI_LEN

    def analyze(self, series: BarSeries) -> ChanStructure:
        """单周期分析：BarSeries → ChanStructure。"""
        raise NotImplementedError("P2")

    def analyze_multi(self, daily: BarSeries, freqs: List[str]) -> Dict[str, ChanStructure]:
        """多周期分析：返回 {freq: ChanStructure}。"""
        raise NotImplementedError("P2")
