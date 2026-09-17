#!/usr/bin/env python
"""W43：一次性清洗库内假 0 换手（turn<=0 或 >=80 → NULL）。

背景：baostock/DuckDB 上游缺数以 0 填充，历史行 96.9% 为假 0
（2026-09-17 实测全库 turn=0 且 volume>0 共 1095 万行）。写端口径
已封（db._t），本脚本清存量：
- UPDATE turn=0 → NULL 与读端口径等价（_turn_opt 本就把 0 当 None），
  无信息损失，不做整库备份（4.9GB 盘紧）；
- 同时把被假 0 污染的 turn_source='ffill' 行清空（等下轮 sync_turnover
  用真锚点重填）；
- 真停牌行（volume<=0 且 turn=0）一并归 NULL：治理口径 0 永远无效。

用法：venv/bin/python scripts/clean_turn_zeroes.py [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

DB = os.environ.get(
    "MYSTERY_DB_PATH",
    "/home/ai/ai_runner/stock/data/db/mystery_cache.db")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not os.path.isfile(DB):
        print(f"库不存在: {DB}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(DB)
    try:
        t0 = time.time()
        n0 = conn.execute(
            "SELECT COUNT(*) FROM stock_kline_data "
            "WHERE turn IS NOT NULL AND (turn <= 0 OR turn >= 80)").fetchone()[0]
        n_ffill = conn.execute(
            "SELECT COUNT(*) FROM stock_kline_data "
            "WHERE turn_source='ffill' AND (turn <= 0 OR turn >= 80)").fetchone()[0]
        print(f"待清洗: turn<=0 或 >=80 共 {n0} 行（其中 ffill 传播 {n_ffill} 行）")
        if args.dry_run:
            return 0
        conn.execute(
            "UPDATE stock_kline_data SET turn=NULL, turn_source=NULL "
            "WHERE turn IS NOT NULL AND (turn <= 0 OR turn >= 80)")
        conn.commit()
        left = conn.execute(
            "SELECT COUNT(*) FROM stock_kline_data "
            "WHERE turn IS NOT NULL AND (turn <= 0 OR turn >= 80)").fetchone()[0]
        print(f"完成: 残留 {left} 行，耗时 {time.time() - t0:.0f}s")
        return 0 if left == 0 else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
