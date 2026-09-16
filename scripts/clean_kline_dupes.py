#!/usr/bin/env python
"""一次性清洗 stock_kline_data 同日双行 + 日期格式混存（2026-09-16 P0-1）。

背景：历史上 DuckDB 预同步写入的 date 带 ' 00:00:00' 后缀，在线回写为
'YYYY-MM-DD'，主键 (code,date,period) 视其为不同行 → 同日双行 1000 万+。
扫描取「近 N 根」时双行吃掉窗口预算（实测 002522 60 根窗口只剩 33 个
自然日），结果随去重顺序抖动。双行副本 OHLCV 实测完全一致，删任意一行
不丢数据；仅 turn 可能只在其中一行（sync-turnover 写 NULL 优先），故先回填。

规则：
1. 回填：冗余长行的正 turn 并入同日期短行（短行为 NULL/0 时）。
2. 删除：存在同 (code, date[:10], period) 短行的长行。
3. 长-vs-长同日互撞：保留 rowid 最大（最后写入，等价 upsert 语义）。
4. 归一：剩余长行 date → substr(date,1,10)（OR IGNORE + 残留删除兜底）。
5. 校验后清空 analysis_cache / chan_cache（K 线序列变化，指纹全变）。

分批 rowid 提交、幂等可重跑。
用法： MYSTERY_DB_PATH=... python scripts/clean_kline_dupes.py [--dry-run]
"""
import os
import sqlite3
import sys
import time

DB = os.environ.get('MYSTERY_DB_PATH') or \
    '/home/ai/ai_runner/stock/data/db/mystery_cache.db'
BATCH = 2_000_000


def main(dry: bool) -> None:
    t0 = time.time()
    con = sqlite3.connect(DB, timeout=120)
    con.execute('PRAGMA journal_mode=WAL')
    cur = con.cursor()

    def n(sql, args=()):
        return cur.execute(sql, args).fetchone()[0]

    print(f"[0] db={DB}")
    print(f"[0] rows total={n('SELECT COUNT(*) FROM stock_kline_data'):,} "
          f"long_format={n('SELECT COUNT(*) FROM stock_kline_data WHERE length(date)>10'):,}")
    mn, mx = cur.execute(
        "SELECT MIN(rowid), MAX(rowid) FROM stock_kline_data").fetchone()
    mn = mn or 0

    # ---- 1. 回填 turn（驱动集 = 全库正 turn 行，千级；PK 点更新）----
    print("[1] backfilling turn from long rows into plain-date siblings...")
    pos = cur.execute(
        "SELECT code, period, substr(date,1,10), turn, turn_source "
        "FROM stock_kline_data WHERE turn>0").fetchall()
    hit = 0
    for code, period, d, turn, tsrc in pos:
        s = cur.execute("SELECT turn FROM stock_kline_data "
                        "WHERE code=? AND date=? AND period=?",
                        (code, d, period)).fetchone()
        if s is not None and (s[0] is None or s[0] == 0):
            hit += 1
            if not dry:
                cur.execute("UPDATE stock_kline_data SET turn=?, turn_source=? "
                            "WHERE code=? AND date=? AND period=?",
                            (turn, tsrc, code, d, period))
    print(f"    patched siblings: {hit:,} (driver {len(pos):,})")
    if not dry:
        con.commit()

    # ---- 2. 删除「有同日短行」的冗余长行（按 (code,period) 组装短日期集合）----
    print("[2] deleting redundant long rows shadowed by plain-date rows...")
    deleted = 0
    lo = mn
    while lo <= mx:
        hi = lo + BATCH - 1
        rows = cur.execute(
            "SELECT rowid, code, period, date FROM stock_kline_data "
            "WHERE rowid BETWEEN ? AND ? AND length(date)>10",
            (lo, hi)).fetchall()
        if rows and not dry:
            short_dates = {}
            for c, p in {(c, p) for _, c, p, _d in rows}:
                short_dates[(c, p)] = {
                    r[0] for r in cur.execute(
                        "SELECT date FROM stock_kline_data "
                        "WHERE code=? AND period=? AND length(date)=10",
                        (c, p))}
            dels = [rid for rid, c, p, d_ in rows
                    if d_[:10] in short_dates.get((c, p), ())]
            if dels:
                cur.executemany("DELETE FROM stock_kline_data WHERE rowid=?",
                                [(r,) for r in dels])
                deleted += len(dels)
                con.commit()
        lo = hi + 1
        print(f"    …rowid≤{min(hi, mx):,} deleted={deleted:,} "
              f"({time.time()-t0:.0f}s)", flush=True)
    print(f"[2] deleted {deleted:,}")

    # ---- 3. 剩余长行：先归一（OR IGNORE），再删除撞 PK 的残留 ----
    print("[3] normalizing remaining long dates; dropping PK collisions...")
    if not dry:
        upd = 0
        lo = mn
        while lo <= mx:
            hi = lo + BATCH - 1
            cur.execute(
                "UPDATE OR IGNORE stock_kline_data SET date=substr(date,1,10) "
                "WHERE rowid BETWEEN ? AND ? AND length(date)>10",
                (lo, hi))
            upd += cur.rowcount
            con.commit()
            lo = hi + 1
        left = n("SELECT COUNT(*) FROM stock_kline_data WHERE length(date)>10")
        cur.execute("DELETE FROM stock_kline_data WHERE length(date)>10")
        dropped = cur.rowcount
        con.commit()
        print(f"[3] normalized {upd:,}, PK-collide dropped {dropped:,}")

    # ---- 4. 校验 ----
    dup = n("""SELECT COUNT(*) FROM (SELECT 1 FROM stock_kline_data
               GROUP BY code, date, period HAVING COUNT(*)>1)""")
    bad = n("SELECT COUNT(*) FROM stock_kline_data WHERE length(date)>10")
    final = n("SELECT COUNT(*) FROM stock_kline_data")
    print(f"[4] residual dup groups={dup} non-plain dates={bad} rows={final:,}")
    if dup or bad:
        print("!! cleaning incomplete — NOT clearing caches")
        con.close()
        sys.exit(1)

    # ---- 5. 失效分析/缠论缓存 ----
    if not dry:
        for t in ('analysis_cache', 'chan_cache'):
            try:
                cur.execute(f"DELETE FROM {t}")
                print(f"[5] cleared {t}: {cur.rowcount} rows")
            except sqlite3.Error as e:
                print(f"[5] {t} skip: {e}")
        con.commit()
        # 注：不 VACUUM——5GB 库需 ~10G 临时空间，本机磁盘仅剩 6G。
        # 删除的 1000 万行留在 freelist，后续 sync 自然复用。
    con.close()
    print(f"done in {time.time()-t0:.0f}s")


if __name__ == '__main__':
    main('--dry-run' in sys.argv)
