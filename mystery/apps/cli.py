#!/usr/bin/env python3
"""mystery.apps.cli — czsc-mi 命令入口（P3 收口 + 002.md W1-W2）。

用法：
  czsc-mi analyze --stock sh600519
  czsc-mi daily --watchlist --limit 3
  czsc-mi scan --limit 100 --signal true_resonance
  czsc-mi sync --period daily --period weekly --days 365
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from typing import Optional

from ..config import load_config, output_dir


def _cmd_analyze(args: argparse.Namespace) -> int:
    from ..services.analyze import analyze_one_stock

    result = analyze_one_stock(args.stock, include_detail=not args.quick,
                               cfg=args.cfg)
    print(json.dumps(result.to_dict(), ensure_ascii=False))
    return 0


def _cmd_daily(args: argparse.Namespace) -> int:
    from ..apps.reports.excel_report import write_excel
    from ..apps.reports.html_report import write_html
    from ..services import watchlist as _wl
    from ..services.analyze import analyze_one_stock

    if args.watchlist:
        codes = _wl.load_watchlist()
    elif args.symbols:
        codes = list(args.symbols)
    else:
        codes = _wl.load_watchlist()
    if args.limit:
        codes = codes[:args.limit]
    if not codes:
        print("标的为空（--watchlist 读 data/watchlist.json，或用 --symbols 指定）")
        return 1
    results = []
    failed = 0
    for code in codes:
        try:
            r = analyze_one_stock(code, include_detail=True, cfg=args.cfg)
            d = r.to_dict()
            if args.min_score is None or (d.get('score') is not None
                                          and float(d['score']) >= args.min_score):
                results.append(d)
        except Exception as e:
            failed += 1
            print(f"[daily] {code} 分析失败跳过: {str(e)[:80]}", file=sys.stderr)
    if not results:
        print("全部失败或低于最低分，未生成报告")
        return 1
    results.sort(key=lambda x: (x.get('score') is not None,
                                float(x.get('score') or -1)), reverse=True)
    out = output_dir(args.cfg)
    date_str = datetime.now().strftime("%Y%m%d")
    xlsx = f"{out}/每日股票分析报告_{date_str}.xlsx"
    html = f"{out}/每日股票分析报告_{date_str}.html"
    write_excel(results, xlsx)
    write_html(results, html)
    print(f"共 {len(results)} 只（失败 {failed}）")
    print(xlsx)
    print(html)
    return 0


def _cmd_scan(args: argparse.Namespace) -> int:
    from ..core.scan_signals import filter_by_signal
    from ..services import watchlist as _wl
    from ..services.scan import scan_market

    watchlist = _wl.load_watchlist() if args.watchlist else None
    results = scan_market(limit=args.limit, include_detail=True,
                          min_score=args.min_score, cfg=args.cfg,
                          no_persist=args.no_persist,
                          watchlist=watchlist)
    if args.signal:
        results = filter_by_signal(results, args.signal)
    for r in results:
        if r.get("chip_low"):
            chip_s = "是"
        elif r.get("chip_low_unknown"):
            chip_s = "未知"
        elif r.get("chip_quiet"):
            chip_s = "缩量高位"
        else:
            chip_s = "否"
        trn = r.get("turnover_20")
        trn_s = f"{float(trn):.2f}%" if trn is not None else "-"
        pos = r.get("price_pos")
        pos_s = f"{float(pos) * 100:.1f}%" if pos is not None else "-"
        print(f"{r.get('symbol')} {(r.get('name') or '未知'):　<6} "
              f"score={r.get('score')} chip_low={chip_s} "
              f"20日换手={trn_s} 回撤={pos_s} {r.get('advice', '')}")
    n_tr = sum(1 for r in results if r.get('true_resonance'))
    n_vap = sum(1 for r in results if r.get('vap_atr_break'))
    n_chip = sum(1 for r in results if r.get('chip_low'))
    n_unknown = sum(1 for r in results if r.get('chip_low_unknown'))
    n_quiet = sum(1 for r in results if r.get('chip_quiet'))
    print(f"\n共 {len(results)} 只 | 真三振 {n_tr} | VAP-ATR突破 {n_vap} | "
          f"筹码低位 {n_chip}（换手未知 {n_unknown} / 高位缩量 {n_quiet}）")
    return 0


def _cmd_sync(args: argparse.Namespace) -> int:
    from ..services.sync import sync_market

    periods = list(dict.fromkeys(args.period or ['daily']))
    out = sync_market(days=args.days, force=args.force,
                      symbols=args.symbols, limit=args.limit,
                      cfg=args.cfg, periods=periods)
    print(json.dumps(out, ensure_ascii=False))
    return 0


def _cmd_watchlist_import_tdx(args: argparse.Namespace) -> int:
    from ..services import watchlist as _wl

    r = _wl.import_from_tdx(args.cfg)
    if not r.get('path'):
        print("未找到通达信自选文件 zxg.blk（设 TDX_VIPDOC_DIR / TDX_BLOCKNEW_DIR）",
              file=sys.stderr)
        return 1
    print(json.dumps(r, ensure_ascii=False))
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="czsc-mi", description="Mistery 趋势交易分析")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("analyze", help="单票深度分析")
    p.add_argument("--stock", required=True, help="sh600519 / 600519.SH")
    p.add_argument("--quick", action="store_true", help="跳过明细（扫描模式）")
    p.set_defaults(func=_cmd_analyze)

    p = sub.add_parser("daily", help="日报：AnalysisResult → Excel/HTML 落盘")
    p.add_argument("--watchlist", action="store_true",
                   help="读自选股 data/watchlist.json（默认）")
    p.add_argument("--symbols", nargs="*", default=None, help="临时指定代码")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--min-score", type=float, default=None)
    p.set_defaults(func=_cmd_daily)

    p = sub.add_parser("scan", help="全市场扫描（写 scan_jobs/scan_results）")
    p.add_argument("--watchlist", action="store_true",
                   help="只扫自选股（避免全市场，daily 流程默认）")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--min-score", type=float, default=None)
    p.add_argument("--signal", default=None,
                   choices=["vap_atr", "chip_low", "true_resonance"],
                   help="只保留该信号的结果")
    p.add_argument("--no-persist", action="store_true", help="只打印不写库")
    p.set_defaults(func=_cmd_scan)

    p = sub.add_parser("sync", help="行情同步（断点续跑，支持多周期）")
    p.add_argument("--period", action="append",
                   choices=["daily", "weekly", "monthly"],
                   help="可重复：--period daily --period weekly")
    p.add_argument("--days", type=int, default=365)
    p.add_argument("--force", action="store_true", help="强制全量（勿轻易使用）")
    p.add_argument("--symbols", nargs="*", default=None)
    p.add_argument("--limit", type=int, default=None)
    p.set_defaults(func=_cmd_sync)

    p = sub.add_parser("watchlist", help="自选股管理")
    wsub = p.add_subparsers(dest="wl_cmd", required=True)
    wp = wsub.add_parser("import-tdx", help="从通达信本地自选 zxg.blk 导入")
    wp.set_defaults(func=_cmd_watchlist_import_tdx)

    args = parser.parse_args(argv)
    args.cfg = load_config()
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
