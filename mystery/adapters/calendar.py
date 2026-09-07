"""mystery.adapters.calendar — 交易日历（迁自 trade_calendar.py，akshare 在线 + DB 兜底）。

三层缓存：进程内存（TTL 600s）→ 磁盘 data/calendar-cache.json（TTL 24h）→ akshare 在线。
磁盘层消除"每次进程冷启动都打一次在线日历"的卡顿（实测 4~36s）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import List, Optional

logger = logging.getLogger(__name__)

_CALENDAR_TTL = 600
_DISK_TTL = 24 * 3600
_calendar_cache: Optional[List[str]] = None
_calendar_ts: float = 0.0
_calendar_lock = threading.Lock()

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DISK_CACHE_PATH = os.environ.get(
    "MYSTERY_CALENDAR_CACHE",
    os.path.join(_REPO_ROOT, "data", "calendar-cache.json"),
)

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


def _load_disk_calendar() -> List[str]:
    """磁盘日历缓存（24h 内有效），损坏/过期返回空列表。"""
    try:
        if os.path.exists(_DISK_CACHE_PATH) and \
                time.time() - os.path.getmtime(_DISK_CACHE_PATH) < _DISK_TTL:
            with open(_DISK_CACHE_PATH, encoding='utf-8') as f:
                dates = json.load(f)
            if isinstance(dates, list) and dates:
                return [str(d) for d in dates]
    except Exception as e:
        logger.debug(f"日历磁盘缓存读取失败: {str(e)[:60]}")
    return []


def _save_disk_calendar(dates: List[str]) -> None:
    try:
        os.makedirs(os.path.dirname(_DISK_CACHE_PATH), exist_ok=True)
        tmp = _DISK_CACHE_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(dates, f, ensure_ascii=False)
        os.replace(tmp, _DISK_CACHE_PATH)
    except Exception as e:
        logger.debug(f"日历磁盘缓存写入失败: {str(e)[:60]}")


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
            # 1) 磁盘缓存（24h）：进程冷启动不再打在线
            dates = _load_disk_calendar()
            if not dates:
                # 2) akshare 在线；成功则落盘供下次冷启动使用
                dates = _fetch_online_calendar()
                if dates:
                    _save_disk_calendar(dates)
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
