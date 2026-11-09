"""mystery.store.audit — 非破坏性数据库数据质量审计（只读，不改变任何写入行为）。

用途：定期/CI 巡检本地库 stock_kline_data 的数据健康问题，输出统计与样本，
供人工或流水线判定。所有查询均为 SELECT，不执行任何 INSERT/UPDATE/DELETE。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, Dict, List


def audit_kline(db_path: str, period: str = "daily",
                sample_limit: int = 50) -> Dict[str, Any]:
    """扫描 stock_kline_data，返回质量问题统计与样本（只读）。

    检查项：
      1. 重复主键 (code, date, period)
      2. 空/非正收盘价
      3. OHLC 逻辑错误（high<low / high<open / high<close / low>open /
         low>close / 任一非正）
      4. 成交量>0 但成交额缺失（amount IS NULL 或 <=0）
      5. 交易日跳空（同票相邻交易日差 > 7 自然日）
      7. 换手缺失（W81：仅查最新一个交易日——turn 缺失/<=0 且 volume>0；
         当日缺失占比 >50% 时判「数据源缺失 likely」，避免全表扫描）

    参数：
      db_path: SQLite 文件路径
      period: 检查的周期（daily/weekly/monthly）
      sample_limit: 每类问题最多保留的样本条数

    返回结构：{'db_path','period','row_count','checks':{...},'samples':{...}}
    """
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        report: Dict[str, Any] = {
            "db_path": db_path,
            "period": period,
            "row_count": 0,
            "checks": {},
            "samples": {},
        }

        # 1. 重复主键
        dup = cur.execute(
            "SELECT code, substr(date,1,10) d, period, COUNT(*) c "
            "FROM stock_kline_data WHERE period=? "
            "GROUP BY code, d, period HAVING c>1", (period,)).fetchall()
        report["checks"]["duplicate_pk"] = len(dup)
        report["samples"]["duplicate_pk"] = [list(r) for r in dup[:sample_limit]]

        # 2. 空/非正收盘价
        bad_close = cur.execute(
            "SELECT code, substr(date,1,10) d, close FROM stock_kline_data "
            "WHERE period=? AND (close IS NULL OR close<=0)", (period,)).fetchall()
        report["checks"]["null_or_nonpos_close"] = len(bad_close)
        report["samples"]["null_or_nonpos_close"] = \
            [list(r) for r in bad_close[:sample_limit]]

        # 3. OHLC 逻辑错误
        bad_ohlc = cur.execute(
            "SELECT code, substr(date,1,10) d, open, high, low, close "
            "FROM stock_kline_data WHERE period=? AND ("
            "high < low OR high < open OR high < close OR low > open "
            "OR low > close OR open<=0 OR low<=0 OR high<=0)",
            (period,)).fetchall()
        report["checks"]["invalid_ohlc"] = len(bad_ohlc)
        report["samples"]["invalid_ohlc"] = \
            [list(r) for r in bad_ohlc[:sample_limit]]

        # 4. 量>0 但额缺失
        bad_amt = cur.execute(
            "SELECT code, substr(date,1,10) d, volume, amount "
            "FROM stock_kline_data WHERE period=? AND volume>0 "
            "AND (amount IS NULL OR amount<=0)", (period,)).fetchall()
        report["checks"]["volume_no_amount"] = len(bad_amt)
        report["samples"]["volume_no_amount"] = \
            [list(r) for r in bad_amt[:sample_limit]]

        # 5. 总行数（上下文）
        total = cur.execute(
            "SELECT COUNT(*) FROM stock_kline_data WHERE period=?",
            (period,)).fetchone()[0]
        report["row_count"] = total

        # 6. 交易日跳空（同票相邻日期差 > 7 天）
        rows = cur.execute(
            "SELECT code, substr(date,1,10) d FROM stock_kline_data "
            "WHERE period=? ORDER BY code, d", (period,)).fetchall()
        gaps: List[List[Any]] = []
        prev = None
        for code, d in rows:
            if prev and prev[0] == code:
                try:
                    delta = (datetime.strptime(d, "%Y-%m-%d")
                             - datetime.strptime(prev[1], "%Y-%m-%d")).days
                except Exception:
                    delta = 0
                if delta > 7:
                    gaps.append([code, prev[1], d, delta])
            prev = (code, d)
        report["checks"]["date_gaps_gt_7d"] = len(gaps)
        report["samples"]["date_gaps_gt_7d"] = gaps[:sample_limit]

        # 7. 换手缺失（W81，轻量：只查最新一个交易日，不全表扫）
        #    turn 缺失/<=0 且 volume>0 → 该票当日「换手缺失」；若整日占比
        #    >50%，大概率是数据源/股本快照缺失而非个股行为（避免 0 换手被
        #    当成真实缩量参与换手分档）。
        latest = cur.execute(
            "SELECT MAX(substr(date,1,10)) FROM stock_kline_data "
            "WHERE period=?", (period,)).fetchone()[0]
        report["checks"]["latest_trade_date"] = latest or ""
        if latest:
            n_day, n_zero = cur.execute(
                "SELECT COUNT(*), SUM(CASE WHEN (turn IS NULL OR turn<=0) "
                "AND volume>0 THEN 1 ELSE 0 END) FROM stock_kline_data "
                "WHERE period=? AND substr(date,1,10)=?",
                (period, latest)).fetchone()
            n_zero = int(n_zero or 0)
            n_day = int(n_day or 0)
            ratio = (n_zero / n_day) if n_day else 0.0
            report["checks"]["turnover_zero_recent"] = n_zero
            report["checks"]["turnover_zero_ratio"] = round(ratio, 4)
            report["checks"]["turnover_missing_likely"] = \
                bool(n_day and ratio > 0.5)
            zero_rows = cur.execute(
                "SELECT code, turn FROM stock_kline_data "
                "WHERE period=? AND substr(date,1,10)=? "
                "AND (turn IS NULL OR turn<=0) AND volume>0 "
                "ORDER BY code LIMIT ?",
                (period, latest, sample_limit)).fetchall()
            report["samples"]["turnover_zero_recent"] = \
                [list(r) for r in zero_rows]

        return report
    finally:
        conn.close()
