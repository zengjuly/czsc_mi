"""W45 存量剪除：每票日K超 2000 条的删最旧（配合 db._prune_daily 写端钩子）。

用户 2026-09-17 指令：每个股票日K最多保存 2000 条，超限循环替换，控制磁盘。
本脚本处理钩子上线前的存量（实测 3284 票 / 约 125 万多余行）。

用法（czsc_mi 仓根，先 source ~/.stockrc）：
  python scripts/prune_daily_kline.py            # dry-run 报数
  python scripts/prune_daily_kline.py --apply    # 真删
  python scripts/prune_daily_kline.py --apply --vacuum  # 删后 VACUUM 回收磁盘
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sqlite3  # noqa: E402

from mystery.store.db import _DEFAULT_DB, DAILY_KLINE_MAX_ROWS  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真删（默认 dry-run）")
    ap.add_argument("--vacuum", action="store_true", help="删除后 VACUUM 回收")
    ap.add_argument("--db", default=_DEFAULT_DB)
    args = ap.parse_args()
    cap = DAILY_KLINE_MAX_ROWS
    assert cap > 0, "MYSTERY_DAILY_K_MAX_ROWS=0 已关闭上限，无需剪除"

    conn = sqlite3.connect(args.db)
    over = conn.execute(
        "SELECT code, COUNT(*)-? FROM stock_kline_data WHERE period='daily' "
        "GROUP BY code HAVING COUNT(*) > ?", (cap, cap)).fetchall()
    total = sum(n for _, n in over)
    print(f"{time.strftime('%T')} 超{cap}条票数={len(over)} 待删行={total}")
    if not args.apply or not over:
        return 0

    t0 = time.time()
    deleted = 0
    for i, (code, n) in enumerate(sorted(over)):
        cur = conn.execute(
            "DELETE FROM stock_kline_data WHERE code=? AND period='daily' "
            "AND date <= (SELECT date FROM stock_kline_data WHERE code=? "
            "AND period='daily' ORDER BY date DESC LIMIT 1 OFFSET ?)",
            (code, code, cap))
        deleted += cur.rowcount
        if (i + 1) % 500 == 0:
            conn.commit()
            print(f"{time.strftime('%T')} {i+1}/{len(over)} 票已删 {deleted} 行"
                  f"（{(time.time()-t0)/60:.1f}min）", flush=True)
    conn.commit()
    print(f"{time.strftime('%T')} 完成：删 {deleted} 行 / {len(over)} 票"
          f"，用时 {(time.time()-t0)/60:.1f}min")
    chk = conn.execute(
        "SELECT COUNT(*) FROM (SELECT code FROM stock_kline_data "
        "WHERE period='daily' GROUP BY code HAVING COUNT(*)>?)", (cap,)).fetchone()[0]
    print(f"验收：仍超{cap}条的票数 = {chk}（应为 0）")
    if args.vacuum:
        print(f"{time.strftime('%T')} VACUUM 开始…", flush=True)
        t0 = time.time()
        conn.execute("VACUUM")
        print(f"VACUUM 完成，用时 {(time.time()-t0)/60:.1f}min，"
              f"库大小 {os.path.getsize(args.db)/2**30:.2f} GiB")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
