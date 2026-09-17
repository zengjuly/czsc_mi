"""W46 存量合并：600000.SH 后缀孤儿行 → sh.600000 点格式（配合写端修复）。

来源：upsert_kline_many 旧版裸 str(code) 把 DuckDB thscode 原样写库
（W12b 起潜伏），2317 只 .SH + 2898 只 .SZ 共约 2.6 万行。这些后缀行是
sync 反复重拉的**最新数据**（点格式行停在 09-11），必须合并保新，
不可一删了之。

合并规则（同 code+date+period 冲突时）：
  价格量额列以 suffix 行（更新鲜）覆盖；turn/pctChg 用 COALESCE(suffix, 现值)。
幂等：可重复执行；备份表只追补当前仍在主库的后缀行。

用法（先 source ~/.stockrc）：
  python scripts/merge_suffix_codes.py            # dry-run 报数
  python scripts/merge_suffix_codes.py --apply    # 备份→合并→删→验收
"""
import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mystery.store.db import _DEFAULT_DB  # noqa: E402

DOT_FROM_SUFFIX = "lower(substr(code,8)) || '.' || substr(code,1,6)"
SUFFIX_WHERE = "(code GLOB '*.SH' OR code GLOB '*.SZ')"


def counts(c):
    s = c.execute(f"SELECT COUNT(*) FROM stock_kline_data "
                  f"WHERE {SUFFIX_WHERE}").fetchone()[0]
    overlap = c.execute(
        f"SELECT COUNT(*) FROM stock_kline_data s WHERE {SUFFIX_WHERE} "
        "AND EXISTS (SELECT 1 FROM stock_kline_data d "
        "WHERE d.code = lower(substr(s.code,8))||'.'||substr(s.code,1,6) "
        "AND d.date = s.date AND d.period = s.period)").fetchone()[0]
    return s, overlap


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真合并（默认 dry-run）")
    ap.add_argument("--db", default=_DEFAULT_DB)
    args = ap.parse_args()
    c = sqlite3.connect(args.db)
    s, overlap = counts(c)
    print(f"{time.strftime('%H:%M:%S')} 后缀行={s} 与点格式同日冲突={overlap}")
    if not args.apply:
        return 0
    if s == 0:
        print("无后缀行，跳过")
        return 0
    # 1) 备份到独立库（幂等追补；验证后用户可自删）
    bak = args.db + ".suffix_bak.db"
    b = sqlite3.connect(bak)
    b.execute("ATTACH ? AS src", (args.db,))
    b.execute("CREATE TABLE IF NOT EXISTS suffix_rows AS "
              "SELECT * FROM src.stock_kline_data WHERE 0")
    suf_w = SUFFIX_WHERE.replace("code", "k.code")
    b.execute(f"INSERT INTO suffix_rows SELECT k.* FROM src.stock_kline_data k "
              f"WHERE {suf_w} AND NOT EXISTS (SELECT 1 FROM suffix_rows sr "
              f"WHERE sr.code=k.code AND sr.date=k.date AND sr.period=k.period)")
    n_bak = b.execute("SELECT COUNT(*) FROM suffix_rows").fetchone()[0]
    b.commit()
    b.close()
    print(f"备份库现有 {n_bak} 行 → {bak}")
    # 2) 冲突行：点格式行以 suffix 覆盖价格量额、补 turn/pctChg。
    #    先落临时映射表并索引，避免 8 个相关子查询全表扫。
    c.execute("BEGIN")
    c.execute(f"CREATE TEMP TABLE suf AS SELECT "
              f"{DOT_FROM_SUFFIX} AS dot_code, date, period, "
              f"open, high, low, close, volume, amount, turn, pctChg "
              f"FROM stock_kline_data WHERE {SUFFIX_WHERE}")
    c.execute("CREATE INDEX suf_ix ON suf(dot_code, date, period)")
    sel = ("(SELECT s.{col} FROM suf s WHERE s.dot_code=d.code "
           "AND s.date=d.date AND s.period=d.period)")
    sets = ", ".join(
        f"{col}=" + (f"COALESCE({sel.format(col=col)}, {col})"
                     if col in ("turn", "pctChg") else sel.format(col=col))
        for col in ("open", "high", "low", "close", "volume", "amount",
                    "turn", "pctChg"))
    exists = ("EXISTS (SELECT 1 FROM suf s WHERE s.dot_code=d.code "
              "AND s.date=d.date AND s.period=d.period)")
    not_suffix = SUFFIX_WHERE.replace("code", "d.code")
    c.execute(f"UPDATE stock_kline_data AS d SET {sets} "
              f"WHERE NOT {not_suffix} AND {exists}")
    # 3) 无冲突的直接改码迁入（INSERT OR IGNORE：冲突行已在第2步更新过）
    c.execute(f"INSERT OR IGNORE INTO stock_kline_data "
              f"(code, date, period, open, high, low, close, volume, amount, turn, pctChg) "
              f"SELECT {DOT_FROM_SUFFIX}, date, period, open, high, low, close, "
              f"volume, amount, turn, pctChg FROM stock_kline_data WHERE {SUFFIX_WHERE}")
    # 4) 删后缀孤儿
    c.execute(f"DELETE FROM stock_kline_data WHERE {SUFFIX_WHERE}")
    s2, _ = counts(c)
    assert s2 == 0, f"残留后缀行={s2}"
    c.commit()
    print(f"{time.strftime('%H:%M:%S')} 合并完成：残留后缀=0（备份保留于 {bak}）")
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
