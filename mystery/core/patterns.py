"""mystery.core.patterns — 形态识别（迁自 pattern_recognition.py）。纯函数，零 IO。"""
from __future__ import annotations

from typing import Any, Dict

import pandas as pd


def recognize_patterns(daily: pd.DataFrame, **kwargs) -> Dict[str, Any]:
    """形态识别。P1 迁入。"""
    raise NotImplementedError("P1")
