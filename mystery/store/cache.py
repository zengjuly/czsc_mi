"""mystery.store.cache — 分析结果缓存（006.md 阶段 3 / W23）。

键 = symbol + trade_date + cache_key，其中 cache_key 由
adjust / rule_ver / chan_enabled / chan_score / fill_financial / czsc_ver
/ include_detail + bars_fingerprint（首根 dt + 中间 close + 末根
dt/close/volume + volume 校验和 + 根数）归一哈希。

语义：
- 收盘后日级有效；盘中数据在变动时指纹不同 → 自然 miss，不会读到脏分。
- sync 更新某票 K 线 → 必须调 invalidate_symbol 删除该票全部缓存。
- 任何读写异常都降级为 miss / no-op（缓存永远不阻断分析主链）。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from typing import Any, Dict, List, Optional

from .db import MysteryDB


def scan_cache_enabled() -> bool:
    """扫描链是否启用 analysis_cache（006：全市场扫描用环境变量打开）。

    默认开：缓存键含 K 线指纹，数据没变命中即同一结果；scan 与个股页
    二次进入都省一次全量规则计算。MYSTERY_SCAN_CACHE=0 可关。
    """
    return os.environ.get('MYSTERY_SCAN_CACHE', '1').strip() not in (
        '0', 'false', 'off', 'no', '')


def bars_fingerprint(bars: List[Any]) -> str:
    """日 K 指纹（W36 P1-3 加重）：首根 dt + 中间根 close + 末根
    dt/close/volume + 根数 + volume 校验和。

    旧实现只看末根 3 值 + 根数：换源重刷历史、中段补洞都可能指纹不变
    → 脏缓存命中。校验和用「根数×位置加权」取样（首/中/末 + volume 求和
    保留 2 位精度）覆盖全序列，成本 O(n) 字符串拼接只在必要时可接受。
    close/volume 用 repr 保精度；None 组件统一字面量，避免歧义。
    """
    if not bars:
        return 'empty'
    n = len(bars)
    last = bars[-1]
    mid = bars[n // 2]
    first = bars[0]
    vol_sum = 0.0
    for b in bars:
        v = getattr(b, 'volume', None)
        if v is not None:
            vol_sum += float(v)
    parts = [
        str(first.dt)[:19],
        repr(float(mid.close)) if mid.close is not None else 'None',
        str(last.dt)[:19],
        repr(float(last.close)) if last.close is not None else 'None',
        repr(float(last.volume)) if getattr(last, 'volume', None) is not None else 'None',
        repr(round(vol_sum, 2)),
        str(n),
    ]
    return hashlib.sha1('|'.join(parts).encode('utf-8')).hexdigest()[:16]


def make_cache_key(adjust: Optional[str], rule_ver: str,
                   analysis_flags: str, czsc_ver: str,
                   fingerprint: str) -> str:
    """版本 + 归因开关串 + 指纹 → 归一 cache_key（顺序固定，| 分隔）。

    analysis_flags = 'D{0|1}|C{0|1}|S{0|1}|F{0|1}'：
    D=include_detail，C=chan_enabled（结构），S=chan_score（混合分），
    F=fill_financial（W36：扫描链禁在线补财务，与详情互不命中）。
    （epoch 曾入键，会导致 analyze 在线降级自动落库后立即自失效抖动，
    已移除；sync 写 K 线的失效由 upsert_kline* 显式删对应日缓存实现。）
    """
    raw = '|'.join([
        adjust or '', rule_ver, analysis_flags, czsc_ver or '', fingerprint,
    ])
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()


class AnalysisCache:
    """analysis_cache 表读写。db 复用调用方连接对象（同一进程单例语义）。"""

    def __init__(self, db: Optional[MysteryDB] = None):
        self.db = db or MysteryDB()

    def get(self, symbol: str, trade_date: str,
            cache_key: str) -> Optional[Dict[str, Any]]:
        try:
            # W41 #6：与 MysteryDB 同锁（RLock，外层持锁不死锁）——扫描线程池
            # 回退路径不再与 upsert_kline 抢同一库。
            with self.db._lock:
                conn = self.db._connect()
                try:
                    row = conn.execute(
                        "SELECT payload_json FROM analysis_cache "
                        "WHERE symbol=? AND trade_date=? AND cache_key=?",
                        (symbol, trade_date, cache_key)).fetchone()
                finally:
                    conn.close()
            if row:
                return json.loads(row[0])
        except Exception:
            pass
        return None

    def put(self, symbol: str, trade_date: str, cache_key: str,
            payload: Dict[str, Any], *, adjust: Optional[str] = None,
            rule_ver: str = '', chan_enabled: bool = False,
            chan_score: bool = False, czsc_ver: str = '',
            include_detail: bool = False,
            fingerprint: str = '') -> None:
        try:
            with self.db._lock:
                conn = self.db._connect()
                try:
                    conn.execute(
                        "INSERT OR REPLACE INTO analysis_cache "
                        "(symbol, trade_date, cache_key, adjust, rule_ver, "
                        " chan_enabled, chan_score, czsc_ver, include_detail, "
                        " bars_fingerprint, payload_json, created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?, CURRENT_TIMESTAMP)",
                        (symbol, trade_date, cache_key, adjust, rule_ver,
                         int(chan_enabled), int(chan_score), czsc_ver,
                         int(include_detail), fingerprint,
                         json.dumps(payload, ensure_ascii=False)))
                    conn.commit()
                finally:
                    conn.close()
        except Exception:
            pass

    def invalidate_symbol(self, symbol: str,
                          trade_date: Optional[str] = None) -> int:
        """K 线更新后删该票缓存（006：sync 更新则删对应日）。

        trade_date 为 None 时删该票全部（sync 无法精确定位旧交易日）。
        """
        try:
            with self.db._lock:
                conn = self.db._connect()
                try:
                    if trade_date:
                        cur = conn.execute(
                            "DELETE FROM analysis_cache "
                            "WHERE symbol=? AND trade_date=?",
                            (symbol, trade_date))
                    else:
                        cur = conn.execute(
                            "DELETE FROM analysis_cache WHERE symbol=?",
                            (symbol,))
                    conn.commit()
                    return cur.rowcount
                finally:
                    conn.close()
        except Exception:
            return 0


def dot_symbol(code: str) -> str:
    """任意格式代码 → 内部点号格式（600519.SH），与 AnalysisResult.symbol 一致。

    复用 adapters.codes.normalize_symbol，保证缓存键与结果 symbol 同源。
    """
    from ..adapters.codes import normalize_symbol
    return normalize_symbol(code)
