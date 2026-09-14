"""W24 验收：200 只样本 ① resample 口径 vs fetch_bars 口径分差 ② 单票耗时倍率。

跑法: MYSTERY_DB_PATH=... venv/python scripts/w24_bench.py [数量, 默认200]
"""
import os
import sys
import time

os.environ.setdefault('MYSTERY_DB_PATH',
                      '/home/ai/ai_runner/stock/data/db/mystery_cache.db')
os.environ.setdefault('MYSTERY_CHAN_ENABLED', '1')

from mystery.services.analyze import AnalysisService  # noqa: E402
from mystery.store.db import MysteryDB  # noqa: E402


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    db = MysteryDB()
    rows = db._connect().execute(
        "SELECT DISTINCT code FROM stock_kline_data "
        "WHERE date >= '2026-09-01' LIMIT ?", (n,)).fetchall()
    codes = [r[0] for r in rows]
    print(f"样本 {len(codes)} 只")

    svc = AnalysisService()
    diff_max = 0.0
    t_resample = t_fetch = 0.0
    fail = 0
    for i, code in enumerate(codes):
        try:
            # 口径 A（W24 现路）：周月由日 K resample 派生
            t0 = time.perf_counter()
            r_a = svc.analyze_one_stock(code, use_cache=False)
            t_resample += time.perf_counter() - t0
            # 口径 B（旧路等价）：显式 fetch_bars('1w'/'1M') 再 run_rules
            t0 = time.perf_counter()
            daily = svc.market.fetch_bars(code, '1d')
            weekly = svc.market.fetch_bars(code, '1w')
            monthly = svc.market.fetch_bars(code, '1M')
            ctx = svc.build_market_context(daily.symbol, daily)
            bd = svc.run_rules(daily, weekly, monthly, ctx,
                               include_detail=False)
            t_fetch += time.perf_counter() - t0
            s_a = r_a.score if r_a.score is not None else 0.0
            s_b = getattr(bd, 'score', None)
            s_b = s_b if s_b is not None else 0.0
            diff_max = max(diff_max, abs(float(s_a) - float(s_b)))
        except Exception as e:
            fail += 1
            if fail <= 3:
                print(f"  [{code}] 失败: {str(e)[:100]}")
        if (i + 1) % 50 == 0:
            print(f"  进度 {i+1}/{len(codes)} "
                  f"resample累计{t_resample:.1f}s fetch累计{t_fetch:.1f}s "
                  f"分差max={diff_max:.2f}")

    print(f"\n失败 {fail}")
    print(f"分差 max = {diff_max:.3f} (验收 ≤1)")
    print(f"resample 路: {t_resample/len(codes):.3f}s/只")
    print(f"fetch  路: {t_fetch/len(codes):.3f}s/只")
    print(f"倍率: {t_fetch/t_resample:.2f}x" if t_resample else '')


if __name__ == '__main__':
    main()
