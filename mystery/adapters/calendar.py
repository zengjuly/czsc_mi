"""mystery.adapters.calendar — 交易日历（迁自 trade_calendar.py，akshare 在线 + DB 兜底）。"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import List, Optional

logger = logging.getLogger(__name__)

_CALENDAR_TTL = 600
_calendar_cache: Optional[List[str]] = None
_calendar_ts: float = 0.0
_calendar_lock = threading.Lock()

# 收盘时间（当日 15:30 前视为盘中，最新交易日回退上一交易日）
_CLOSE_HOUR, _CLOSE_MINUTE = 15, 30


def _fetch_online_calendar() -> List[str]:
    """akshare 全市场交易日历（含过去+未来），失败返回空列表。"""
    try:
        import akshare as ak
        df = ak.tool_trade_date_hist_sina()
        dates = sorted(str(d) for d in df['trade_date'])
        if dates:
            logger.debug(f"📅 在线交易日历获取成功: {len(dates)} 条 "
                         f"({dates[0]} ~ {dates[-1]})")
        return dates
    except Exception as e:
        logger.warning(f"⚠️ 在线交易日历获取失败({str(e)[:80]})，回退主库")
        return []


def _get_db_max_date() -> Optional[str]:
    """本地库最新交易日（回退用）。"""
    try:
        from ..store.db import MysteryDB
        db = MysteryDB()
        conn = db._connect()
        try:
            row = conn.execute(
                "SELECT MAX(date) FROM stock_kline_data WHERE period='daily'"
            ).fetchone()
            return str(row[0]) if row and row[0] else None
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"⚠️ 本地库最新交易日获取失败: {e}")
        return None


def get_latest_trade_date(now: Optional[datetime] = None) -> Optional[str]:
    """真实最新交易日：

    1. 在线交易日历（TTL 缓存）≤ 今天的最大交易日；盘中(15:30 前)回退上一交易日。
    2. 在线失败 → 本地库 MAX(date)。
    """
    global _calendar_cache, _calendar_ts
    now = now or datetime.now()
    today = now.date()

    with _calendar_lock:
        if _calendar_cache is None or time.time() - _calendar_ts > _CALENDAR_TTL:
            dates = _fetch_online_calendar()
            if dates:
                _calendar_cache = dates
                _calendar_ts = time.time()
        else:
            dates = _calendar_cache

    if dates:
        if now.hour < _CLOSE_HOUR or \
                (now.hour == _CLOSE_HOUR and now.minute < _CLOSE_MINUTE):
            cutoff = (today - timedelta(days=1)).isoformat()
        else:
            cutoff = today.isoformat()
        valid = [d for d in dates if d <= cutoff]
        if valid:
            return valid[-1]
        if dates:
            return dates[0]
    return _get_db_max_date()
