"""mystery.services.sync_turnover — 每日空 turn 回算（W22，006.md 阶段 2.5）。

18:00 管线在行情 sync 之后调用（纯本地派生，不打 HTTP）：
1. 读本地最新 float_shares 快照（as_of <= 当日）。
2. 仅对当日且 turn IS NULL 且 volume>0 的日 K 行写 calc_float。
3. 0 < turn < 80 才写，否则丢弃并记 QA。
4. legacy/official 已有的行绝不覆盖（UPDATE ... AND turn IS NULL）。
5. 近 20 日窗口内连续缺失 ≤5 日的中间洞用前值 ffill（标 ffill）；
   更长的洞保持 NULL；禁止用当前股本回填快照日之前很久的历史。
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from ..core.turnover import derived_turn, ffill_gaps
from ..store.db import MysteryDB
from .sync_shares import _to_thscode

logger = logging.getLogger(__name__)

FFILL_WINDOW_DAYS = 35   # 自然日窗口 ≈ 20 交易日
FFILL_MAX_GAP = 5


def sync_turnover(trade_date: str,
                  codes: Optional[List[str]] = None,
                  db: Optional[MysteryDB] = None) -> Dict:
    """派生指定交易日及各票近端洞的 turn。返回 QA 摘要。"""
    db = db or MysteryDB()
    qa = {'trade_date': trade_date, 'null_rows': 0, 'calc_float': 0,
          'dropped': 0, 'no_shares': 0, 'ffill_filled': 0}

    # ---- 1. 当日空 turn 行 → calc_float ----
    rows = db.null_turn_rows(trade_date, codes=codes)
    qa['null_rows'] = len(rows)
    shares_cache: Dict[str, Optional[float]] = {}
    for r in rows:
        thscode = _to_thscode(r['code'])
        if thscode not in shares_cache:
            snap = db.get_float_share(thscode, as_of_max=trade_date)
            shares_cache[thscode] = snap['float_shares'] if snap else None
        shares = shares_cache[thscode]
        if not shares:
            qa['no_shares'] += 1      # 无股本快照：保持 unknown，不伪造
            continue
        turn = derived_turn(r['volume'], shares, unit='share')
        if turn is None:
            qa['dropped'] += 1        # 越界/无效：丢弃并记 QA
            continue
        db.set_turn(r['code'], r['date'], turn, 'calc_float')
        qa['calc_float'] += 1

    # ---- 2. 近端窗口 ≤5 日连续洞 → ffill（每票独立） ----
    ff = _ffill_recent_gaps(db, trade_date, codes)
    qa['ffill_filled'] = ff

    # ---- 3. 覆盖率 QA 日志 ----
    cov = db.turnover_coverage(days=20, codes=codes)
    qa['coverage'] = cov
    logger.info(f"[sync_turnover] {qa}")
    return qa


def _ffill_recent_gaps(db: MysteryDB, trade_date: str,
                       codes: Optional[List[str]]) -> int:
    """近 20 交易日窗口内，连续缺失 ≤5 日的中间洞前值填充。"""
    with db._lock:
        conn = db._connect()
        try:
            where = "period='daily' AND substr(date,1,10)<=? " \
                    "AND substr(date,1,10)>=date(?, '-{} day')".format(FFILL_WINDOW_DAYS)
            args: List = [trade_date, trade_date]
            if codes:
                where += f" AND code IN ({','.join('?' * len(codes))})"
                args += list(codes)
            data = conn.execute(
                f"SELECT code, substr(date,1,10), turn FROM stock_kline_data "
                f"WHERE {where} ORDER BY code, date", args).fetchall()
        finally:
            conn.close()

    by_code: Dict[str, List] = {}
    for code, d, turn in data:
        by_code.setdefault(code, []).append((d, turn))

    filled = 0
    for code, series in by_code.items():
        vals = [t for _, t in series if t is not None]
        if not vals:
            continue                      # 全空 = 无锚，不填
        _, dates = ffill_gaps(series, max_gap=FFILL_MAX_GAP)
        filled_dates = set(dates)
        prev = None
        for d, t in series:
            if t is not None:
                prev = t
            elif d in filled_dates and prev is not None:
                db.set_turn(code, d, prev, 'ffill')
                filled += 1
    return filled
