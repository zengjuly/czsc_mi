#!/usr/bin/env python3
"""mystery.apps.cli — czsc-mi 命令入口（P3 收口 + 002.md W1-W2）。

用法：
  czsc-mi analyze --stock sh600519
  czsc-mi daily --watchlist --limit 3
  czsc-mi scan --limit 100 --signal true_resonance
  czsc-mi sync --period daily --period weekly --days 365
  czsc-mi sector-sync --sector ths_886015
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from typing import Optional

from ..config import load_config, output_dir

logger = logging.getLogger(__name__)


def _ensure_default_ths_env() -> None:
    """未设置 THS_FUYAO_SCRIPT / THS_MARKETDB_DIR 时，从仓库 sibling 推导默认。

    czsc_mi 与 Financial-API 同级部署（../Financial-API）：默认
    fuyao.py 与 market.duckdb 路径。仅当环境变量未设且路径真实存在时
    注入（不存在则不设，交给降级链 tdx_api/tdx_local）。
    必须在 load_config() 展开 ${THS_...} 之前调用。
    """
    import os
    if os.environ.get("THS_FUYAO_SCRIPT") and os.environ.get("THS_MARKETDB_DIR"):
        return
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))      # .../czsc_mi
    fa_root = os.path.join(os.path.dirname(repo_root), "Financial-API")
    if not os.path.isdir(fa_root):
        return
    if not os.environ.get("THS_FUYAO_SCRIPT"):
        cand = os.path.join(fa_root, "python", "toolkit", "fuyao", "scripts",
                            "fuyao.py")
        if os.path.isfile(cand):
            os.environ["THS_FUYAO_SCRIPT"] = cand
    if not os.environ.get("THS_MARKETDB_DIR"):
        cand = os.path.join(fa_root, "data")
        if os.path.isdir(cand):
            os.environ["THS_MARKETDB_DIR"] = cand


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
    from ..services.analyze import AnalysisService

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
    from concurrent.futures import ThreadPoolExecutor, as_completed
    max_workers = max(1, int(getattr(args, 'workers', 4) or 4))

    # W13: 共享 AnalysisService 实例（模块级 analyze_one_stock 每只 new service，
    # 重复初始化 SQLite 连接/指数/日历缓存，4 线程实测慢 3 倍+，
    # 是 daily 86 只 6.5min 的根因之一；实例方法共享缓存且线程安全）
    svc = AnalysisService(args.cfg)

    def _one(code: str):
        """单只分析（线程内，共享 svc）：返回 (dict, None) 或 (None, 错误串)。"""
        try:
            # W36 P1-1：批量路径与扫描链同口径（库内 ROE 覆盖 5510/5518，
            # 缺则 unknown，不逐只外呼财务 API）
            r = svc.analyze_one_stock(code, include_detail=True,
                                      fill_financial=False)
            return r.to_dict(), None
        except Exception as e:
            return None, f"{code} 分析失败跳过: {str(e)[:80]}"

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(_one, c): c for c in codes}
        for done_n, fut in enumerate(as_completed(futs), 1):
            d, err = fut.result()
            if err:
                failed += 1
                print(f"[daily] {err}", file=sys.stderr)
            elif d is not None and (args.min_score is None
                                    or (d.get('score') is not None
                                        and float(d['score']) >= args.min_score)):
                results.append(d)
            if done_n % 20 == 0 or done_n == len(codes):
                print(f"[daily] 进度 {done_n}/{len(codes)}（workers={max_workers}）",
                      flush=True)
    if not results:
        print("全部失败或低于最低分，未生成报告")
        return 1
    results.sort(key=lambda x: (x.get('score') is not None,
                                float(x.get('score') or -1)), reverse=True)
    out = output_dir(args.cfg)
    date_str = datetime.now().strftime("%Y%m%d")
    xlsx = f"{out}/每日股票分析报告_{date_str}.xlsx"
    html = f"{out}/每日股票分析报告_{date_str}.html"
    try:  # W26b QA 行（只读观测）
        from ..store.db import MysteryDB
        qa_line = _turnover_qa_line(
            MysteryDB(), results,
            (results[0].get('trade_date') or '')[:10] or date_str)
    except Exception:  # noqa: BLE001
        qa_line = ""
    # W37 大盘滤网（只读展示，现算不进个股缓存；会话缓存=每进程一次）
    try:
        from ..services.analyze import market_env_line, market_env_summary
        qa_line = (qa_line + "\n" if qa_line else "") + market_env_line(
            market_env_summary(svc.market))
    except Exception:  # noqa: BLE001
        pass
    write_excel(results, xlsx, qa_line=qa_line or None)
    write_html(results, html, qa_line=qa_line or None)
    print(f"共 {len(results)} 只（失败 {failed}）")
    print(xlsx)
    print(html)
    return 0


def _write_scan_report(results, args) -> None:
    """扫描结果生成 Excel/HTML 日报（W17：定时任务 `scan --watchlist --report` 用）。

    文件名与 daily 一致（每日股票分析报告_YYYYMMDD.xlsx/.html），
    供飞书 xlsx 链接与 git push 复用，无需改 feishu_notify / 管线 git 段。
    W26b：附换手覆盖率 QA 行（只读观测，不改排序、不改 2%/低位门）。
    """
    from ..apps.reports.excel_report import write_excel
    from ..apps.reports.html_report import write_html
    from ..store.db import MysteryDB

    out = output_dir(args.cfg)
    date_str = datetime.now().strftime("%Y%m%d")
    xlsx = f"{out}/每日股票分析报告_{date_str}.xlsx"
    html = f"{out}/每日股票分析报告_{date_str}.html"
    try:
        trade_date = (results[0].get('trade_date') or '')[:10] \
            if results else ''
        qa_line = _turnover_qa_line(MysteryDB(), results,
                                     trade_date or date_str) \
            if results else ""
    except Exception:  # noqa: BLE001 QA 失败不阻塞报告
        qa_line = ""
    # W37 大盘滤网（只读展示；新建 service 仅为取数通道，会话缓存一次）
    try:
        from ..services.analyze import (AnalysisService, market_env_line,
                                        market_env_summary)
        qa_line = (qa_line + "\n" if qa_line else "") + market_env_line(
            market_env_summary(AnalysisService(args.cfg).market))
    except Exception:  # noqa: BLE001
        pass
    write_excel(results, xlsx, qa_line=qa_line or None)
    write_html(results, html, qa_line=qa_line or None)
    print(f"报告已生成: {xlsx}")
    print(f"报告已生成: {html}")
    if qa_line:
        print(f"[换手QA] {qa_line}")


def _print_db_fingerprint() -> None:
    """W31b（012.md）：scan/sync-turnover 启动先打一行数据指纹，
    防止忘 export MYSTERY_DB_PATH 打到仓内旧库还当成治理回退。"""
    try:
        from ..store.db import MysteryDB
        fp = MysteryDB().data_fingerprint()
        print(f"db={fp.get('db_path')} kline_max={fp.get('kline_max')}"
              f" as_of_max={fp.get('as_of_max')}", flush=True)
    except Exception as e:  # noqa: BLE001 指纹失败不阻塞主流程
        print(f"[warn] db 指纹读取失败: {e}", file=sys.stderr)


def _cmd_scan(args: argparse.Namespace) -> int:
    from ..core.scan_signals import filter_by_signal
    from ..services import watchlist as _wl
    from ..services.scan import scan_market

    _print_db_fingerprint()
    # --watchlist 自选扫描默认不设 limit（全自选）；全市场防呆默认 100
    if args.limit is None:
        args.limit = None if args.watchlist else 100
    watchlist = _wl.load_watchlist() if args.watchlist else None
    results = scan_market(limit=args.limit, include_detail=True,
                          min_score=args.min_score, cfg=args.cfg,
                          no_persist=args.no_persist,
                          watchlist=watchlist,
                          force=getattr(args, 'force', False))
    if args.report and results:
        _write_scan_report(results, args)
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


def _cmd_sync_shares(args: argparse.Namespace) -> int:
    from ..services.sync_shares import sync_shares

    if getattr(args, 'from_adjustments', False):
        from ..services.sync_shares import sync_shares_from_adjustments
        out = sync_shares_from_adjustments(since=args.since)
        print(json.dumps(out, ensure_ascii=False))
        return 0    # 无事件/表缺失均为正常路径（周对账兜底），不阻塞管线

    codes = None
    if args.watchlist:
        from ..services.watchlist import load_watchlist
        codes = load_watchlist()
    out = sync_shares(codes=codes, force=args.force,
                      fresh_skip_days=getattr(args, 'fresh_skip_days', None))
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get('fetched') else 1


def _cmd_sync_turnover(args: argparse.Namespace) -> int:
    from datetime import datetime

    from ..services.sync_turnover import sync_turnover
    from ..services.watchlist import load_watchlist
    from ..store.db import MysteryDB

    db = MysteryDB()
    _print_db_fingerprint()  # W31b：换手派生同样先亮库
    trade_date = args.date or datetime.now().strftime('%Y-%m-%d')
    codes = load_watchlist() if args.watchlist else None
    out = sync_turnover(trade_date, codes=codes, db=db,
                        backfill_days=args.backfill_days)
    print(json.dumps(out, ensure_ascii=False))
    # 观测补丁：可填的根存在却一根没写 → 派生环节故障（as_of/量单位/
    # code 映射问题），stderr 打 ERROR 且非零退出，让 pipeline_status
    # 的 turnover_derive=fail 可见。回填模式（backfill_days）跳过此判：
    # 回填合法地可能一根都不填（全部 date<as_of 属预期 skipped）。
    if not args.backfill_days and out.get('calc_float', 0) == 0:
        try:
            from ..adapters.codes import db_code_of
            db_codes = ([db_code_of(c) for c in codes]
                        if codes is not None else None)
            stats = db.turnover_qa_stats(trade_date, codes=db_codes)
            unfilled = ((stats.get('fillable_rows') or 0)
                        - (stats.get('filled_rows') or 0))
            if unfilled > 0:
                print(f"ERROR: sync-turnover {trade_date} "
                      f"calc_float=0 但可填未写 {unfilled} 根 —— "
                      f"查 as_of/量单位/code 映射", file=sys.stderr)
                return 1
        except Exception:
            pass  # 自检本身故障不打断正常派生结果
    return 0


def _turnover_qa_line(db, results, trade_date: str) -> str:
    """日报 QA 行（W26b/W27a）：换手20日覆盖率【自选】与【全市场】双口径
    分列 + chip_low 低位未知只数（只读，不改判、不改阈值）。

    W27a（008.md P0）：全市场口径被 ~5500 只无快照票拉低（当前 44.5%），
    会误导「派生失败」；自选口径才是 18:00 验收指标。两行以 \\n 分隔，
    Excel 分行写、HTML 转 <br>。
    """
    try:
        import math

        from ..adapters.codes import db_code_of
        from ..services.watchlist import load_watchlist

        def _cov_str(stats) -> str:
            cov = stats.get('coverage')
            cov_s = f"{cov * 100:.1f}%" if cov is not None else "无数据"
            fc = stats.get('fillable_coverage')
            fc_s = (f"{fc * 100:.1f}%" if fc is not None else "—")
            seg = (f"{cov_s}（{stats.get('n_symbols')} 只中 "
                   f"{stats.get('n_with_shares')} 只有股本快照，"
                   f"窗口 {stats.get('window_start')}~{trade_date}）")
            # W28a：可填口径 —— as_of 落在窗口中后段时覆盖率被「依法不填」
            # 的前半段拉低；可填覆盖率才反映派生健康度（目标自选 ≥95%）
            if stats.get('fillable_rows'):
                extra = (f" | 可填覆盖率 {fc_s}"
                         f"（{stats.get('filled_rows')}/"
                         f"{stats.get('fillable_rows')} 根）")
                if stats.get('as_of_min'):
                    extra += (f" | 快照 as_of {stats['as_of_min']}"
                              f"~{stats['as_of_max']}")
                # 观测补丁：把「窗口只有 N 根可填」说成人话，防止把
                # 覆盖率 46% 当成派生故障（可填日从 as_of 起才数得着）
                if stats.get('shortfall_to_20'):
                    extra += (f" | 可填日从 as_of 起还差 "
                              f"{stats['shortfall_to_20']} 个交易日才满 20 根")
                if stats.get('skipped_before_asof'):
                    extra += f" | as_of前空turn {stats['skipped_before_asof']} 根"
                seg += "\n  " + extra.strip(" |")
            return seg

        n_unknown = sum(1 for r in results if r.get('chip_low_unknown'))
        n_turn = sum(1 for r in results
                     if r.get('turnover_20') is not None
                     or (isinstance(r.get('turnover_20'), float)
                         and not math.isnan(r['turnover_20'])))
        # W31b（012.md）：第一行固定标明数据指纹——手动 scan 打到旧库/仓内
        # 空库时，「0 只有股本快照」是库不对，不是快照丢了。
        fp = db.data_fingerprint()
        head = (f"db={fp.get('db_path')} kline_max={fp.get('kline_max')}"
                f" as_of_max={fp.get('as_of_max')}")
        warns = []
        if fp.get('kline_max') is None:
            warns.append("WARN: 库内无日K——空库/库不对（检查 MYSTERY_DB_PATH）")
        elif trade_date and fp['kline_max'] < trade_date:
            warns.append(f"WARN: K线止于 {fp['kline_max']}，早于分析日 "
                         f"{trade_date}（未跑当日 sync？）")
        if fp.get('as_of_max') is None:
            warns.append("WARN: 股本快照表为空——可能未用 MYSTERY_DB_PATH "
                         "指向生产库")
        if warns:
            head += "\n  " + "；".join(warns)
        chip_seg = (f" · 本次报告 chip_low 未知 {n_unknown}/{len(results)} 只"
                    f"（近20日均换手可得 {n_turn} 只）") if results else ""
        lines = [head]
        wl_db = []
        try:
            wl_db = [db_code_of(c) for c in load_watchlist()]
        except Exception:  # noqa: BLE001 自选清单缺失不算 QA 失败
            wl_db = []
        report_symbols = {r.get('symbol') for r in results if r.get('symbol')}
        chip_seg_used = False
        if wl_db:
            seg = chip_seg if report_symbols and \
                {db_code_of(s) for s in report_symbols} == set(wl_db) else ""
            chip_seg_used = bool(seg)
            lines.append("自选 换手20日覆盖率 "
                         + _cov_str(db.turnover_qa_stats(trade_date,
                                                         codes=wl_db)) + seg)
        lines.append("全市场 换手20日覆盖率 "
                     + _cov_str(db.turnover_qa_stats(trade_date))
                     + ("" if chip_seg_used else chip_seg))
        # W32c（013.md）：三振行业腿 sector_kline 覆盖率（只读；缺失保持
        # 未知，禁止成分股抽样）。口径：主行业归一 ths_ 后关联 sector_kline。
        sc = db.sector_coverage_stats(codes=(wl_db or None))
        if sc.get('n_stocks'):
            lines.append(
                f"行业覆盖 watchlist_n={sc['n_stocks']} "
                f"sector_kline_n={sc['n_covered']} "
                f"sector_coverage={sc['coverage']}% "
                f"（行业 {sc['n_sectors_covered']}/{sc['n_sectors']} 个，"
                f"板块K线止于 {sc.get('kline_max')}）")
        # W27b（008.md P3）：管线 2/3 步状态行（daily_pipeline 写 status json；
        # 手动跑报告时文件不存在/过期 → 跳过，不误导）
        try:
            import os

            from ..config import output_dir
            sp = os.path.join(output_dir(), 'pipeline_status.json')
            if os.path.exists(sp):
                with open(sp, encoding='utf-8') as f:
                    st = json.load(f)
                if st.get('date') == datetime.now().strftime('%Y-%m-%d'):
                    lines.append(f"管线状态：股本刷新={st.get('shares_refresh')}"
                                 f"；换手派生={st.get('turnover_derive')}")
            # W28b（009.md）：周日铺盘验收行（7 日内有效）
            wp = os.path.join(output_dir(), 'weekly_shares_status.json')
            if os.path.exists(wp):
                import time as _t
                with open(wp, encoding='utf-8') as f:
                    ws = json.load(f)
                age = (_t.time() - os.path.getmtime(wp)) / 86400
                if age <= 7:
                    mark = "✅" if ws.get('exit') == 0 else "❌"
                    lines.append(
                        f"上周铺盘 {mark}：market_with_shares="
                        f"{ws.get('market_with_shares')}/"
                        f"{ws.get('market_symbols')} "
                        f"(watchlist {ws.get('watchlist_with_shares')}/"
                        f"{ws.get('watchlist_symbols')})")
        except Exception:  # noqa: BLE001 状态缺失不影响 QA
            pass
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001 QA 失败不阻塞日报
        logger.debug(f"turnover QA 行生成失败: {e}")
        return ""


def _cmd_sector_sync(args: argparse.Namespace) -> int:
    from ..services.sync import sync_sector_constituents

    out = sync_sector_constituents(args.sector, cfg=args.cfg, limit=args.limit)
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get('written') else 1


def _cmd_sector_sync_kline(args: argparse.Namespace) -> int:
    from ..services.sync import sync_sector_kline

    out = sync_sector_kline(cfg=args.cfg, days=args.days,
                            sectors=args.sector, limit=args.limit,
                            full_since=args.full_since)
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get('synced') else 1


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
    # 未设 THS 路径时自动补 sibling Financial-API 默认（须在 load_config 前）
    _ensure_default_ths_env()
    parser = argparse.ArgumentParser(prog="czsc-mi", description="Mistery 趋势交易分析")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("analyze", help="单票深度分析")
    p.add_argument("--stock", required=True, help="sh600519 / 600519.SH")
    p.add_argument("--quick", action="store_true", help="跳过明细（扫描模式）")
    p.set_defaults(func=_cmd_analyze)

    p = sub.add_parser("daily", help="日报：AnalysisResult → Excel/HTML 落盘")
    p.add_argument("--watchlist", action="store_true",
                   help="读自选股 data/watchlist.json（默认）")
    p.add_argument("--workers", type=int, default=4,
                   help="并发分析线程数（默认 4）")
    p.add_argument("--symbols", nargs="*", default=None, help="临时指定代码")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--min-score", type=float, default=None)
    p.set_defaults(func=_cmd_daily)

    p = sub.add_parser("scan", help="全市场扫描（写 scan_jobs/scan_results）")
    p.add_argument("--watchlist", action="store_true",
                   help="只扫自选股（避免全市场，daily 流程默认）")
    p.add_argument("--limit", type=int, default=None,
                   help="最多扫 N 只（--watchlist 时默认全自选；全市场默认 100）")
    p.add_argument("--min-score", type=float, default=None)
    p.add_argument("--signal", default=None,
                   choices=["vap_atr", "chip_low", "true_resonance"],
                   help="只保留该信号的结果")
    p.add_argument("--report", action="store_true",
                   help="扫描后生成 Excel/HTML 日报（定时任务用，文件名同 daily）")
    p.add_argument("--no-persist", action="store_true", help="只打印不写库")
    p.add_argument("--force", action="store_true",
                   help="跳过 W25 扫描互斥锁（并发危险，仅调试用）")
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

    p = sub.add_parser("sync-shares",
                       help="流通股本快照刷新（低频：初始化/每周对账/除权事件，W22/W26c）")
    p.add_argument("--force", action="store_true",
                   help="强制全量重拉并写快照（跳过 3%% 跳变过滤）")
    p.add_argument("--watchlist", action="store_true",
                   help="只刷自选股（每周对账必跑）")
    p.add_argument("--from-adjustments", action="store_true",
                   help="W26c：只重拉 MarketDB 除权事件（送转/增发）命中的票；"
                        "无事件零 HTTP 秒回，可安全进每日管线")
    p.add_argument("--since", default=None,
                   help="事件窗口起点 YYYY-MM-DD（默认上次成功对账日，否则近 7 天）")
    p.add_argument("--fresh-skip-days", type=int, default=None,
                   help="W27d：距上次快照 as_of 不足 N 天的票跳过 HTTP"
                        "（周日全市场控批次；--force 时忽略）")
    p.set_defaults(func=_cmd_sync_shares)

    p = sub.add_parser("sync-turnover",
                       help="每日空 turn 回算（纯本地派生，不打 HTTP，W22）")
    p.add_argument("--date", default=None, help="交易日 YYYY-MM-DD（默认今天）")
    p.add_argument("--watchlist", action="store_true",
                   help="只处理自选股（默认全市场）")
    p.add_argument("--backfill-days", type=int, default=None,
                   help="W26b 策略 B：额外回填近 N 自然日内的空 turn"
                        "（不越过各票快照 as_of；仅手动，勿入每日管线）")
    p.set_defaults(func=_cmd_sync_turnover)

    p = sub.add_parser("sector-sync", help="同步板块成分股 → stock_sector_rel")
    p.add_argument("--sector", required=True,
                   help="板块代码（ths_886015 / 886015 / 886015.TI 均可）")
    p.add_argument("--limit", type=int, default=None,
                   help="最多写 N 只成分（调试用）")
    p.set_defaults(func=_cmd_sector_sync)

    p = sub.add_parser("sector-sync-kline",
                       help="增量同步板块指数日K → sector_kline（W39，三振行业腿）")
    p.add_argument("--sector", action="append", default=None,
                   help="指定板块（可重复；缺省 = sector_kline 全部存量板块）")
    p.add_argument("--days", type=int, default=15,
                   help="每板块从库内最新日期前扩 N 天重拉（默认 15）")
    p.add_argument("--limit", type=int, default=None,
                   help="最多同步 N 个板块（调试用）")
    p.add_argument("--full-since", default=None, metavar="YYYY-MM-DD",
                   help="清空 sector_kline 后自该日全量重灌（W39 date_ms 错位"
                        "重建专用；常规增量勿用）")
    p.set_defaults(func=_cmd_sector_sync_kline)

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
