#!/usr/bin/env python3
"""W45: 用 baostock 历史换手率（turn 字段，按当日真实流通股本计算）回填存量 NULL turn。

背景：库里 1991~2026 有 ~1020 万行 turn 为 NULL；float_share_snapshot 只有当前
快照，sync_turnover 护栏禁止用今天的股本回填历史（skipped_before_asof）。
baostock 官方 turn 是按当时股本算的真值，是历史换手唯一可靠本地可得来源。

规则：
- 只回填 period='daily'；值域过 W43 同一护栏 0 < t < 80，假 0 拒收。
- 只写 turn IS NULL 或来源 calc_float/ffill（当前股本近似）的行，
  legacy/official 真值不覆盖。幂等：重跑自动跳过已达标行。
- 股票名单以 baostock query_stock_basic type='1' 为准，指数/ETF 排除。
- 双代码格式：bs 格式（sh.600000）直查；ths 后缀格式（600000.SH）映射回
  baostock code 查询后写回原行。
- 并发：baostock 服务端约支持 2-3 并发会话（第 4 个报 10001001 用户未登录），
  多 worker 错峰登录 + 失败重试；写库用 date 精确匹配（substr 无法走索引，
  实测慢 190 倍）。

用法：
  source ~/.stockrc
  python scripts/backfill_turn_baostock.py --start 1991-01-01 [--workers 3]
      [--limit N] [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

STATE = os.path.expanduser("~/.local/state/czsc_mi")
CKPT = os.path.join(STATE, "baostock_turn_ckpt.txt")


def _valid(t):
    return t is not None and 0 < t < 80


def ths_to_bs(code: str) -> str | None:
    """'600000.SH' -> 'sh.600000'；已是 bs 格式返回自身；其他返回 None。"""
    if code[:3] in ("sh.", "sz.", "bj."):
        return code
    if "." in code:
        num, suf = code.rsplit(".", 1)
        if suf.upper() in ("SH", "SZ", "BJ"):
            return f"{suf.lower()}.{num}"
    return None


def login_with_retry(bs, tries: int = 12) -> bool:
    for _ in range(tries):
        if bs.login().error_code == "0":
            return True
        time.sleep(1.0 + random.random() * 2.0)
    return False


def _worker(args):
    wid, todo, start, end = args
    import baostock as bs
    from mystery.store.db import MysteryDB

    time.sleep(wid * (1.5 + random.random()))
    if not login_with_retry(bs):
        return wid, 0, 0, ["LOGIN_FAIL"]
    db = MysteryDB()
    conn = db._connect()
    cur = conn.cursor()
    ck = open(CKPT, "a", buffering=1)   # O_APPEND 行级原子，多进程共写安全
    n_rows = n_codes = 0
    errs = []
    for i, code in enumerate(todo):
        bs_code = ths_to_bs(code)
        if not bs_code:
            errs.append(f"{code}: 无法映射")
            continue
        # 查询偶发掉会话（10001001）：重登一次再试
        rs = None
        for attempt in range(2):
            rs = bs.query_history_k_data_plus(
                bs_code, "date,turn", start_date=start, end_date=end,
                frequency="d", adjustflag="3")
            if rs.error_code == "0":
                break
            login_with_retry(bs, tries=3)
        if rs is None or rs.error_code != "0":
            errs.append(f"{code}: query {rs.error_code}")
            continue
        rows = []
        while rs.next():
            d, t = rs.get_row_data()
            try:
                tv = float(t)
            except (TypeError, ValueError):
                continue
            if _valid(tv):
                rows.append((round(tv, 4), code, d))
        if rows:
            cur.executemany(
                "UPDATE stock_kline_data SET turn=?, turn_source='legacy' "
                "WHERE code=? AND period='daily' AND date=? "
                "AND (turn IS NULL OR turn_source IN ('calc_float','ffill'))",
                rows)
            conn.commit()
            n_rows += len(rows)
        n_codes += 1
        ck.write(code + "\n")
        if (i + 1) % 100 == 0:
            print(f"[w{wid} {i+1}/{len(todo)}] rows={n_rows}", flush=True)
    ck.close()
    conn.close()
    try:
        bs.logout()
    except Exception:
        pass
    return wid, n_codes, n_rows, errs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="1991-01-01")
    ap.add_argument("--end", default=time.strftime("%Y-%m-%d"))
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    os.makedirs(STATE, exist_ok=True)
    import baostock as bs
    if not login_with_retry(bs):
        print("baostock login 失败")
        sys.exit(1)
    rs = bs.query_stock_basic()
    ok_codes = set()
    while rs.error_code == '0' and rs.next():
        r = rs.get_row_data()          # code, code_name, ipoDate, outDate, type, status
        if r[4] == '1':
            ok_codes.add(r[0])
    print(f"baostock 股票名单: {len(ok_codes)}", flush=True)

    from mystery.store.db import MysteryDB
    db = MysteryDB()
    conn = db._connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT DISTINCT code FROM stock_kline_data "
        "WHERE period='daily' AND date>=? AND date<=? "
        "AND (turn IS NULL OR turn_source IN ('calc_float','ffill')) "
        "AND volume>0 ORDER BY code", (args.start, args.end))
    universe = [r[0] for r in cur.fetchall() if ths_to_bs(r[0]) in ok_codes]
    conn.close()

    done = set()
    if os.path.exists(CKPT):
        with open(CKPT) as f:
            done = {ln.strip() for ln in f if ln.strip()}
    todo = [c for c in universe if c not in done]
    if args.limit:
        todo = todo[:args.limit]
    print(f"universe={len(universe)} done={len(done)} todo={len(todo)}", flush=True)
    bs.logout()
    if args.dry_run or not todo:
        return

    shards = [todo[i::args.workers] for i in range(args.workers)]
    t0 = time.time()
    from multiprocessing import Pool
    with Pool(args.workers) as p:
        results = p.map(_worker, [(w, sh, args.start, args.end)
                                  for w, sh in enumerate(shards)])
    tot_c = sum(r[1] for r in results)
    tot_r = sum(r[2] for r in results)
    for wid, nc, nr, errs in results:
        if errs:
            print(f"w{wid} errs({len(errs)}):", errs[:5])
    print(f"DONE codes={tot_c} rows={tot_r} ({(time.time()-t0)/60:.1f}min)",
          flush=True)


if __name__ == "__main__":
    main()
