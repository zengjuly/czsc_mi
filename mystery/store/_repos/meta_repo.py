"""mystery.store._repos.meta_repo - MetaRepoMixin（按职责从 MysteryDB 抽取，公开 API 不变）。"""
from __future__ import annotations
import logging
import os
import sqlite3
import threading
from typing import Any, Dict, List, Optional
import pandas as pd
from ._base import BaseRepoMixin
from ._helpers import *

class MetaRepoMixin(BaseRepoMixin):
    def get_stock_list(self, stock_only: bool = True) -> List[Dict]:
            """证券列表 [{code, name}]，code 形如 sh.600519。"""
            with self._lock:
                conn = self._connect()
                try:
                    sql = ("SELECT code, code_name FROM stock_industry_info "
                           "WHERE code_name IS NOT NULL AND code_name != ''")
                    if stock_only:
                        sql += " AND (type='1' OR type IS NULL)"
                    rows = conn.execute(sql).fetchall()
                    out = []
                    for c, n in rows:
                        if n and str(n) != 'nan':
                            out.append({'code': str(c), 'name': str(n)})
                    return out
                finally:
                    conn.close()
    def get_stock_name(self, code: str) -> str:
            """查股票名称（sh600519 / sh.600519 / 600519.SH 均可）。"""
            from mystery.adapters.codes import db_code_of
            try:
                db_code = db_code_of(code) if not code.startswith(('sh.', 'sz.', 'bj.')) else code
                with self._lock:
                    conn = self._connect()
                    try:
                        row = conn.execute(
                            "SELECT code_name FROM stock_industry_info WHERE code=? LIMIT 1",
                            (db_code,)).fetchone()
                        return str(row[0]) if row and row[0] else ""
                    finally:
                        conn.close()
            except Exception as e:
                logger.debug(f"行业名读取失败: {e}")
                return ""
    def get_primary_industry(self, stock_code: str) -> Optional[tuple]:
            """股票主行业 → (sector_code, sector_name) 或 None。"""
            with self._lock:
                conn = self._connect()
                try:
                    row = conn.execute(
                        "SELECT r.sector_code, m.sector_name "
                        "FROM stock_sector_rel r LEFT JOIN sector_meta m "
                        "ON r.sector_code=m.sector_code "
                        "WHERE r.stock_code=? AND r.is_primary=1 LIMIT 1",
                        (stock_code,)).fetchone()
                    return row
                finally:
                    conn.close()
    def data_fingerprint(self) -> Dict:
            """W31b（012.md）：QA/报表第一行标明「打的是哪套库」。

            手动 scan 若忘了 export MYSTERY_DB_PATH 会打到仓内空库/旧库，
            页脚「0 只有股本快照」被误判成治理回退。本方法只读三条指纹：
            db 路径 / 日K 末日 / 股本快照最新 as_of。
            """
            out = {'db_path': self.db_path, 'kline_max': None, 'as_of_max': None}
            try:
                with self._lock:
                    conn = self._connect()
                    try:
                        r = conn.execute(
                            "SELECT MAX(substr(date,1,10)) FROM stock_kline_data "
                            "WHERE period='daily'").fetchone()
                        out['kline_max'] = r[0] if r and r[0] else None
                        r = conn.execute(
                            "SELECT MAX(as_of) FROM float_share_snapshot").fetchone()
                        out['as_of_max'] = r[0] if r and r[0] else None
                        # W46 哨兵：K线表双格式回潮检测（恒为 0，非 0 即
                        # upsert 归一失效或外部直写，需人工介入）
                        r = conn.execute(
                            "SELECT COUNT(*) FROM stock_kline_data "
                            "WHERE code GLOB '*.[A-Z][A-Z]'").fetchone()
                        out['suffix_orphans'] = r[0] if r else None
                    finally:
                        conn.close()  # W41：与其他读方法同构，防指纹泄漏连接
            except Exception:  # noqa: BLE001 指纹失败不阻塞 QA
                pass
            return out
    def sector_coverage_stats(self, codes: Optional[List[str]] = None) -> Dict:
            """W32c（013.md）：三振行业腿所需 sector_kline 覆盖率（只读）。

            口径与 get_sector_kline 一致：stock_sector_rel.is_primary=1 的主行业，
            sector_code 归一为 ths_ 前缀后与 sector_kline 关联。行业缺失保持未知，
            禁止成分股抽样。
            """
            out = {'n_stocks': 0, 'n_covered': 0, 'coverage': None,
                   'n_sectors': 0, 'n_sectors_covered': 0, 'kline_max': None}
            try:
                with self._lock:
                    conn = self._connect()
                    norm = "'ths_' || replace(r.sector_code, '.TI', '')"
                    where = "r.is_primary=1"
                    args: List = []
                    if codes:
                        where += f" AND r.stock_code IN ({','.join('?' * len(codes))})"
                        args += list(codes)
                    rows = conn.execute(
                        f"SELECT r.stock_code, {norm}, "
                        f"  MAX((SELECT 1 FROM sector_kline k "
                        f"        WHERE k.sector_code = {norm})) "
                        f"FROM stock_sector_rel r WHERE {where} "
                        f"GROUP BY r.stock_code, 2", args).fetchall()
                    out['n_stocks'] = len(rows)
                    out['n_covered'] = sum(1 for *_x, c in rows if c)
                    out['coverage'] = (round(out['n_covered'] * 100.0 / out['n_stocks'], 1)
                                       if out['n_stocks'] else None)
                    out['n_sectors'] = len({s for _c, s, _x in rows})
                    out['n_sectors_covered'] = len({s for _c, s, x in rows if x})
                    r = conn.execute(
                        "SELECT MAX(trade_date) FROM sector_kline").fetchone()
                    out['kline_max'] = r[0] if r and r[0] else None
            except Exception:  # noqa: BLE001 只读统计不阻塞
                pass
            return out
    def turnover_qa_stats(self, trade_date: str,
                              codes: Optional[List[str]] = None,
                              window_days: int = 33) -> Dict:
            """W26b QA：窗口 [trade_date-window_days, trade_date] 内按票统计。

            universe 口径：窗口内有日 K 行的票才算分母（无 K 不摊薄覆盖率）。
            n_with_shares：存在 as_of <= trade_date 股本快照的票（可派生票）。
            by_source 含 null 键 = 窗口内仍无 turn 的行数（chip_low_unknown 根源）。
            """
            from datetime import datetime, timedelta
            start = (datetime.strptime(trade_date, '%Y-%m-%d')
                     - timedelta(days=window_days)).strftime('%Y-%m-%d')
            with self._lock:
                conn = self._connect()
                try:
                    where = ("period='daily' AND substr(date,1,10)>=? "
                             "AND substr(date,1,10)<=?")
                    args: List = [start, trade_date]
                    if codes:
                        where += f" AND code IN ({','.join('?' * len(codes))})"
                        args += list(codes)
                    uni = ('' if codes else
                           " AND code IN (SELECT code FROM stock_industry_info"
                           " WHERE type='1' OR type IS NULL)")
                    rows = conn.execute(
                        f"SELECT code, COALESCE(turn_source,'null'), COUNT(*), "
                        f"SUM(CASE WHEN turn > 0 AND turn < 80 THEN 1 ELSE 0 END) "
                        f"FROM stock_kline_data WHERE {where}{uni} "
                        f"GROUP BY code, 2", args).fetchall()  # W43: 假0不算覆盖
                    stats = {'as_of': trade_date, 'window_start': start,
                             'n_symbols': 0, 'n_with_shares': 0,
                             'rows': 0, 'with_turn': 0, 'coverage': None,
                             'by_source': {}, 'n_symbols_full': 0}
                    by_code: Dict[str, tuple] = {}
                    for code, src, n, w in rows:
                        prev = by_code.get(code, (0, 0))
                        by_code[code] = (prev[0] + n, prev[1] + w)
                        stats['by_source'][src] = stats['by_source'].get(src, 0) + n
                    if codes:
                        stats['n_symbols'] = len(set(codes))
                    else:
                        stats['n_symbols'] = len(by_code)
                    for code, (n, w) in by_code.items():
                        stats['rows'] += n
                        stats['with_turn'] += w
                        if n and w == n:
                            stats['n_symbols_full'] += 1
                    snaps = conn.execute(
                        "SELECT thscode, MAX(as_of) FROM float_share_snapshot "
                        "WHERE as_of<=? GROUP BY thscode",
                        (trade_date,)).fetchall()
                    snap_set = {r[0] for r in snaps}
                    # W28a：票级 as_of 表（thscode '600519.SH' → db code 'sh600519'）
                    as_of_by_db = {}
                    for t, a in snaps:
                        if '.' in t:
                            num, ex = t.rsplit('.', 1)
                            # db code 形如 sh.600001（带点）
                            as_of_by_db[f"{ex.lower()}.{num}"] = a
                    targets = set(codes) if codes else set(by_code)
                    stats['n_with_shares'] = sum(
                        1 for c in targets if _dot(c) in snap_set)
                    stats['no_shares'] = stats['n_symbols'] - stats['n_with_shares']
                    if stats['rows']:
                        stats['coverage'] = round(
                            stats['with_turn'] / stats['rows'], 4)
                    # W28a（009.md）：fillable 口径 —— 只统计「有快照且
                    # date >= as_of」且 volume 有效的 K 根（这些行才是派生
                    # 「有资格填」的根）；覆盖率低但可填覆盖率高 = 快照新、
                    # 窗口前半段依法不填，不是派生失败。只读，不改任何写入。
                    dated = conn.execute(
                        f"SELECT code, substr(date,1,10), turn, volume "
                        f"FROM stock_kline_data WHERE {where}{uni}",
                        args).fetchall()
                    fillable = filled = skipped = 0
                    all_dates = {d for _, d, _, _ in dated}
                    as_of_list = sorted(v for k, v in as_of_by_db.items()
                                        if k in targets)
                    for code, d, turn, vol in dated:
                        a = as_of_by_db.get(code)
                        if not a:
                            continue
                        if d < a:
                            if turn is None:
                                skipped += 1
                            continue
                        if vol is not None and vol > 0:
                            fillable += 1
                            if turn is not None and 0 < turn < 80:
                                # W43：假 0 不算已填（与 coverage 口径同源）
                                filled += 1
                    stats['fillable_rows'] = fillable
                    stats['filled_rows'] = filled
                    stats['fillable_coverage'] = (round(filled / fillable, 4)
                                                   if fillable else None)
                    stats['skipped_before_asof'] = skipped
                    # 人话口径（W28+1 观测补丁）：从最晚 as_of 起到 trade_date
                    # 窗口内实际有几个交易日根 = 可填根的最大长度；距满 20 根
                    # 还差多少，用来解释「覆盖率低但可填率高」不是故障。
                    if as_of_list:
                        stats['as_of_min'] = as_of_list[0]
                        stats['as_of_max'] = as_of_list[-1]
                        stats['as_of_p50'] = as_of_list[len(as_of_list) // 2]
                        dates_since = sum(1 for d in all_dates
                                          if d >= as_of_list[-1])
                        stats['fillable_days'] = dates_since
                        stats['shortfall_to_20'] = max(0, 20 - dates_since)
                    return stats
                finally:
                    conn.close()
