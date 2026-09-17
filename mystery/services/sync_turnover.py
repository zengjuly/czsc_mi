"""mystery.services.sync_turnover — 每日空 turn 回算（W22，006.md 阶段 2.5）。

18:00 管线在行情 sync 之后调用（纯本地派生，不打 HTTP）：
1. 读本地最新 float_shares 快照（as_of <= 当日）。
2. 仅对当日且 turn IS NULL 且 volume>0 的日 K 行写 calc_float。
3. 0 < turn < 80 才写，否则丢弃并记 QA。
4. legacy/official 已有的行绝不覆盖（UPDATE ... AND turn IS NULL）。
5. 近 20 日窗口内连续缺失 ≤5 日的中间洞用前值 ffill（标 ffill）；
   更长的洞保持 NULL；禁止用当前股本回填快照日之前很久的历史。

W26b（007.md）：
- 默认策略 A 不变：只填当日 + 近端 ffill，进 18:00 管线。
- 可选策略 B（仅 CLI，`backfill_days`）：回填 [max(快照 as_of, 当日-N自然日),
  当日] 内的空 turn；边界由 get_float_share(as_of_max=row_date) 天然守住
  （行日期 < 唯一快照 as_of 时查不到快照 → skipped_before_asof，不写入）。
- 结束打一行结构化 QA（覆盖率、来源分布、unknown 面），供日报/排障读取。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from ..core.turnover import derived_turn, ffill_gaps
from ..store.db import MysteryDB
from .sync_shares import _to_thscode

logger = logging.getLogger(__name__)

FFILL_WINDOW_DAYS = 35   # 自然日窗口 ≈ 20 交易日
FFILL_MAX_GAP = 5


def sync_turnover(trade_date: str,
                  codes: Optional[List[str]] = None,
                  db: Optional[MysteryDB] = None,
                  backfill_days: Optional[int] = None) -> Dict:
    """派生指定交易日及各票近端洞的 turn。返回 QA 摘要。

    backfill_days=None → 策略 A（只填当日）。给了 N>0 → 策略 B：额外回填
    [trade_date - N 自然日, trade_date] 内快照 as_of 之后的空 turn（仅 CLI）。
    """
    db = db or MysteryDB()
    qa = {'trade_date': trade_date, 'null_rows': 0, 'calc_float': 0,
          'dropped': 0, 'no_shares': 0, 'ffill_filled': 0,
          'backfill_range': 0, 'skipped_before_asof': 0}

    shares_cache: Dict[str, Optional[Dict]] = {}

    def _snap(thscode: str, max_date: str) -> Optional[Dict]:
        # 缓存键 = (票, 上界日期)：当日与回填各查各的 as_of
        key = f"{thscode}|{max_date}"
        if key not in shares_cache:
            shares_cache[key] = db.get_float_share(thscode, as_of_max=max_date)
        return shares_cache[key]

    # ---- 1. 当日空 turn 行 → calc_float ----
    rows = db.null_turn_rows(trade_date, codes=codes)
    qa['null_rows'] = len(rows)
    for r in rows:
        thscode = _to_thscode(r['code'])
        snap = _snap(thscode, trade_date)
        if not snap:
            qa['no_shares'] += 1      # 无股本快照：保持 unknown，不伪造
            continue
        turn = derived_turn(r['volume'], snap['float_shares'], unit='share')
        if turn is None:
            qa['dropped'] += 1        # 越界/无效：丢弃并记 QA
            continue
        db.set_turn(r['code'], r['date'], turn, 'calc_float')
        qa['calc_float'] += 1

    # ---- 2. 策略 B：回填窗口内空 turn（不越过各票快照 as_of） ----
    if backfill_days and backfill_days > 0:
        start = (datetime.strptime(trade_date, '%Y-%m-%d')
                 - timedelta(days=int(backfill_days))).strftime('%Y-%m-%d')
        bf = db.null_turn_rows_between(start, trade_date, codes=codes)
        qa['backfill_range'] = len(bf)
        for r in bf:
            thscode = _to_thscode(r['code'])
            snap = _snap(thscode, r['date'])
            if not snap:
                # 该行日期早于唯一快照 as_of（或确无快照）→ 禁止回填
                qa['skipped_before_asof'] += 1
                continue
            turn = derived_turn(r['volume'], snap['float_shares'],
                                unit='share')
            if turn is None:
                qa['dropped'] += 1
                continue
            db.set_turn(r['code'], r['date'], turn, 'calc_float')
            qa['calc_float'] += 1
        # 回填后新边界仍查不到快照的，才计 skipped；已在窗口内逐行处理

    # ---- 3. 近端窗口 ≤5 日连续洞 → ffill（每票独立） ----
    ff = _ffill_recent_gaps(db, trade_date, codes)
    qa['ffill_filled'] = ff

    # ---- 4. 覆盖率 QA 日志（W26b：让「看不见」变「看得见」） ----
    cov = db.turnover_coverage(days=20, codes=codes)
    qa['coverage'] = cov
    stats = db.turnover_qa_stats(trade_date, codes=codes)
    qa['turn_20_coverage'] = stats['coverage']
    qa['n_symbols'] = stats['n_symbols']
    qa['n_with_shares'] = stats['n_with_shares']
    unknown_rows = stats['by_source'].get('null', 0)
    qa['turn_null_rows'] = unknown_rows
    qa['chip_low_unknown_rate'] = (round(unknown_rows / stats['rows'], 4)
                                   if stats['rows'] else None)
    qa['turn_source'] = stats['by_source']
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
    # W43：锚点/前值口径与治理同源（0 < t < 80 才有效）——旧版把假 0 当
    # "有值"锚点向前填充（实测 15864 行 ffill 传播了假 0），一并阻断。
    def _ok(t):
        return t is not None and 0 < t < 80
    for code, series in by_code.items():
        vals = [t for _, t in series if _ok(t)]
        if not vals:
            continue                      # 全空 = 无锚，不填
        _, dates = ffill_gaps(
            [(d, t if _ok(t) else None) for d, t in series],
            max_gap=FFILL_MAX_GAP)
        filled_dates = set(dates)
        prev = None
        for d, t in series:
            if _ok(t):
                prev = t
            elif d in filled_dates and prev is not None:
                db.set_turn(code, d, prev, 'ffill')
                filled += 1
    return filled
