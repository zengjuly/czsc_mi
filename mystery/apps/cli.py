#!/usr/bin/env python3
"""mystery.apps.cli — czsc-mi 命令入口（P3 收口）。

用法：
  czsc-mi analyze --stock sh600519
  czsc-mi daily --limit 50
  czsc-mi scan --limit 100 --min-score 60
  czsc-mi sync --period daily --days 365 [--symbols sh600519 sz000001]
"""
from __future__ import annotations

import argparse
import sys
from typing import Optional


def _cmd_analyze(args: argparse.Namespace) -> int:
    import json as _json

    from ..services.analyze import analyze_one_stock

    result = analyze_one_stock(args.stock, include_detail=not args.quick)
    print(_json.dumps(result.to_dict(), ensure_ascii=False))
    return 0


def _cmd_daily(args: argparse.Namespace) -> int:
    from ..services.scan import scan_market

    results = scan_market(limit=args.limit, include_detail=False,
                          min_score=args.min_score)
    for r in results:
        print(f"{r.get('symbol')} {r.get('name', ''):　<6} "
              f"score={r.get('score')} {r.get('advice', '')}")
    print(f"\n共 {len(results)} 只")
    return 0


def _cmd_scan(args: argparse.Namespace) -> int:
    from ..services.scan import scan_market

    results = scan_market(limit=args.limit, include_detail=False,
                          min_score=args.min_score)
    for r in results:
        print(f"{r.get('symbol')} {r.get('name', ''):　<6} "
              f"score={r.get('score')} {r.get('advice', '')}")
    print(f"\n共 {len(results)} 只")
    return 0


def _cmd_sync(args: argparse.Namespace) -> int:
    from ..services.sync import sync_market

    out = sync_market(period=args.period, days=args.days, force=args.force,
                      symbols=args.symbols, limit=args.limit)
    print(out)
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="czsc-mi", description="Mistery 趋势交易分析")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("analyze", help="单票深度分析")
    p.add_argument("--stock", required=True, help="sh600519 / 600519.SH")
    p.add_argument("--quick", action="store_true", help="跳过明细（扫描模式）")
    p.set_defaults(func=_cmd_analyze)

    p = sub.add_parser("daily", help="日报（默认自选股/全列表前N）")
    p.add_argument("--limit", type=int, default=50, help="最多分析只数")
    p.add_argument("--min-score", type=float, default=None)
    p.set_defaults(func=_cmd_daily)

    p = sub.add_parser("scan", help="全市场扫描")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--min-score", type=float, default=None)
    p.set_defaults(func=_cmd_scan)

    p = sub.add_parser("sync", help="行情同步")
    p.add_argument("--period", default="daily", choices=["daily", "weekly", "monthly"])
    p.add_argument("--days", type=int, default=365)
    p.add_argument("--force", action="store_true", help="强制全量（勿轻易使用）")
    p.add_argument("--symbols", nargs="*", default=None)
    p.add_argument("--limit", type=int, default=None)
    p.set_defaults(func=_cmd_sync)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except NotImplementedError as e:
        print(f"[{args.cmd}] 尚未实现（P{e}）", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"[{args.cmd}] 失败: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
