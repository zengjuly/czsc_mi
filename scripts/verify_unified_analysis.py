#!/usr/bin/env python3
"""verify_unified_analysis — 同股三路径评分一致性验证（P3）。

三路径：个股页(Service 直调) / 扫描(scan_market) / CLI(czsc-mi analyze)，
底层同一 analyze_one_stock → score 差 ≤ 1。
chan 关闭时同时与 tests/fixtures/gold_*.json 金标对比。

用法：
    python scripts/verify_unified_analysis.py [--chan]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mystery.services.analyze import analyze_one_stock  # noqa: E402
from mystery.services.scan import scan_market  # noqa: E402

_CODES = ['sh600519', 'sz000001', 'sh600150']


def _cli_analyze(code: str) -> dict:
    out = subprocess.run(
        [sys.executable, '-m', 'mystery.apps.cli', 'analyze', '--stock', code,
         '--quick'],
        capture_output=True, text=True, timeout=180, cwd=str(Path(__file__).resolve().parents[1]))
    if out.returncode != 0:
        raise RuntimeError(f"CLI analyze {code} 失败: {out.stderr[-200:]}")
    return json.loads(out.stdout.strip().splitlines()[-1])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--chan', action='store_true', help='开启缠论（默认关）')
    args = ap.parse_args()

    ok = True
    for code in _CODES:
        r1 = analyze_one_stock(code, include_detail=False)          # 个股页路径
        d1 = r1.to_dict()
        d2 = scan_market(watchlist=[code], include_detail=False)[0]  # 扫描路径
        d3 = _cli_analyze(code)                                      # CLI 路径
        s1, s2, s3 = d1.get('score'), d2.get('score'), d3.get('score')
        diffs = [abs(float(a or 0) - float(b or 0))
                 for a, b in ((s1, s2), (s2, s3), (s1, s3))]
        line = f"{code}: 个股={s1} 扫描={s2} CLI={s3} | max_diff={max(diffs):.1f}"
        if max(diffs) <= 1:
            print(f"✓ {line}")
        else:
            ok = False
            print(f"✗ {line}")
        # 金标对比（chan 关闭时）
        if not args.chan:
            gold_path = Path(__file__).resolve().parents[1] / 'tests' / 'fixtures' \
                / f'gold_{code}.json'
            if gold_path.exists():
                gold = json.loads(gold_path.read_text(encoding='utf-8'))
                g = gold.get('综合评分')
                diff = abs(float(s1 or 0) - float(g or 0))
                if diff <= 1:
                    print(f"  ✓ 金标一致 (mine={s1} gold={g})")
                else:
                    ok = False
                    print(f"  ✗ 金标不一致 (mine={s1} gold={g})")
    print('\nPASS' if ok else '\nFAIL')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
