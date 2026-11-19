"""mystery.adapters.minute_source — 分钟K线数据源（W103）。

数据源：本地 tdx-stock-web 容器 REST（http://localhost:8080），
      用 /api/kline-history?type=minute30|hour&limit=N 拉最近N根（默认200）：
      - 相比 /api/kline 返回全历史(~700KB/只)，history+limit 仅~35KB/只，提速~20倍；
      - 30m 200根≈25天、60m 200根≈50天，均满足 MACD(12,26,9) 所需~80根。
      注意：mootdx 的 tdx 二进制协议在本环境所有服务器握手失败（2026-11-19
      实测），故不走 mootdx，统一用 tdx-web REST。
价格：tdx-web 返回放大1000倍的整数，统一 /1000 还原为实际价。
代码：入参 600519.SH → tdx-web 用 6 位数字 600519。
"""
from __future__ import annotations

import logging
import os
import re
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

_TDX_WEB = os.environ.get("TDX_WEB_URL", "http://localhost:8080")
_TIMEOUT = float(os.environ.get("MYSTERY_MIN30_TIMEOUT", "15"))
_SCALE = 1000.0
_BAR_LIMIT = int(os.environ.get("MYSTERY_MIN30_BAR_LIMIT", "200"))


def _six(code: str) -> str:
    """600519.SH / sh600519 / 600519 → 600519。"""
    m = re.search(r"(\d{6})", str(code))
    return m.group(1) if m else str(code).strip()


def _num(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return round(float(v) / _SCALE, 4)
    except (TypeError, ValueError):
        return None


def fetch_minute_bars(symbol: str, freq: str = "minute30") -> List[Dict]:
    """拉某票分钟K线，返回 [{dt,open,high,low,close,volume,amount}] 升序。

    freq: 'minute30'（30分钟）或 'hour'（60分钟）。当前只用 minute30，
    60min 由 30min 聚合（见 service 层），避免重复存储。
    """
    code = _six(symbol)
    url = f"{_TDX_WEB}/api/kline-history"
    try:
        r = requests.get(url, params={"code": code, "type": freq,
                                      "limit": _BAR_LIMIT}, timeout=_TIMEOUT)
        r.raise_for_status()
        payload = r.json()
    except Exception as e:
        logger.debug(f"[minute] tdx-web 请求失败 {symbol}/{freq}: {str(e)[:100]}")
        return []
    if payload.get("code") != 0:
        logger.debug(f"[minute] tdx-web 业务错误 {symbol}: {payload.get('message')}")
        return []
    data = payload.get("data") or {}
    lst = data.get("List") or [] if isinstance(data, dict) else []
    bars = []
    for it in lst:
        # tdx-web 返回 '2026-11-19T14:00:00+08:00'，统一成 'YYYY-MM-DD HH:MM'
        raw_dt = str(it.get("Time", ""))
        dt = raw_dt[:16].replace("T", " ")
        if not dt or " " not in dt:
            continue
        bars.append({
            "dt": dt,
            "open": _num(it.get("Open")),
            "high": _num(it.get("High")),
            "low": _num(it.get("Low")),
            "close": _num(it.get("Close")),
            "volume": it.get("Volume"),
            "amount": it.get("Amount"),
        })
    return bars


def aggregate_60min(bars_30: List[Dict]) -> List[Dict]:
    """由 30min K线聚合成 60min（每相邻2根 → 1根）。

    交易时段 30min 分时点：09:30,10:00,...,11:30,13:30,...,15:00 共8根/日。
    60min 聚合：把它们按 (日期, 时段序) 两两合并。
    简化实现：按日期分组，每2根合并（首开/最高/最低/末收/量额求和）。
    """
    from collections import OrderedDict
    by_day: Dict[str, List[Dict]] = OrderedDict()
    for b in bars_30:
        day = b["dt"][:10]
        by_day.setdefault(day, []).append(b)
    out = []
    for _day, day_bars in by_day.items():
        day_bars.sort(key=lambda x: x["dt"])
        for i in range(0, len(day_bars), 2):
            chunk = day_bars[i:i + 2]
            closes = [c["close"] for c in chunk if c.get("close") is not None]
            highs = [c["high"] for c in chunk if c.get("high") is not None]
            lows = [c["low"] for c in chunk if c.get("low") is not None]
            if not closes:
                continue
            out.append({
                "dt": chunk[-1]["dt"],
                "open": chunk[0].get("open"),
                "high": max(highs) if highs else None,
                "low": min(lows) if lows else None,
                "close": closes[-1],
                "volume": sum((c.get("volume") or 0) for c in chunk),
                "amount": sum((c.get("amount") or 0) for c in chunk),
            })
    return out
