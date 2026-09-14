"""mystery.services.scan — 全市场扫描（只调 analyze_one_stock）。

与个股页 / daily 同源同分（同一 Service）。单票失败不中断（大规模扫描容错）。
W1-B：每条结果带三类信号（core.scan_signals.classify），末尾写
scan_jobs / scan_results 落库；``no_persist=True`` 只打印不写库。
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..core.scan_signals import classify
from ..store.db import MysteryDB
from .analyze import AnalysisService

logger = logging.getLogger(__name__)


def _write_scan_batch(db, results: List[Dict[str, Any]], failed: int,
                      trade_date: Optional[str] = None,
                      scan_type: str = "market") -> Optional[int]:
    """写一笔 scan_jobs + 全量 scan_results，返回 job_id。

    W18：同类型扫描只保留最新一份——先删该 scan_type 的旧 job 及其
    scan_results，再插新（全市场/自选/板块各一份，历史不无限累积）。
    """
    import sqlite3
    conn = db._connect()
    try:
        started = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            "DELETE FROM scan_results WHERE job_id IN "
            "(SELECT id FROM scan_jobs WHERE scan_type=?)", (scan_type,))
        conn.execute("DELETE FROM scan_jobs WHERE scan_type=?", (scan_type,))
        cur = conn.execute(
            "INSERT INTO scan_jobs (trade_date, started_at, finished_at, "
            "n_ok, n_fail, scan_type) VALUES (?,?,?,?,?,?)",
            (trade_date or "", started, started, len(results), failed, scan_type))
        job_id = cur.lastrowid
        for r in results:
            conn.execute(
                "INSERT INTO scan_results (job_id, symbol, trade_date, score, "
                "true_resonance, vap_atr_break, chip_low, payload_json) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (job_id,
                 str(r.get("symbol", "")),
                 str(r.get("trade_date", "")),
                 r.get("score"),
                 1 if r.get("true_resonance") else 0,
                 1 if r.get("vap_atr_break") else 0,
                 1 if r.get("chip_low") else 0,
                 json.dumps(r, ensure_ascii=False)))
        conn.commit()
        return job_id
    finally:
        conn.close()


def _scan_worker(codes_batch: List[str], cfg: Optional[Dict],
                 include_detail: bool, min_score: Optional[float]):
    """进程池 worker：独立 AnalysisService 分析一批股票（W15 进程级并行）。

    :return: (results, failed, filtered) — filtered=低于 min_score 被过滤的只数
              （进度统计用，主进程不重算）。
    """
    svc = AnalysisService(cfg)
    # W23：scan 链缓存开关（env MYSTERY_SCAN_CACHE，默认开）。
    from ..store.cache import scan_cache_enabled
    use_cache = scan_cache_enabled()
    out: List[Dict[str, Any]] = []
    failed = 0
    filtered = 0
    for code in codes_batch:
        try:
            r = svc.analyze_one_stock(code, include_detail=include_detail,
                                      use_cache=use_cache)
            d = r.to_dict()
            d.update(classify(d))
            if min_score is None or (d.get('score') is not None
                                     and float(d['score']) >= min_score):
                out.append(d)
            else:
                filtered += 1
        except Exception as e:
            failed += 1
            logger.warning(f"[scan] {code} 分析失败跳过: {str(e)[:80]}")
    return out, failed, filtered


def _scan_thread_worker(code: str, svc: AnalysisService,
                        include_detail: bool, min_score: Optional[float]):
    """线程池 worker（共享 svc）：返回 (dict or None, is_failed)。"""
    try:
        from ..store.cache import scan_cache_enabled
        r = svc.analyze_one_stock(code, include_detail=include_detail,
                                  use_cache=scan_cache_enabled())
        d = r.to_dict()
        d.update(classify(d))
        if min_score is not None and (d.get('score') is None
                                      or float(d['score']) < min_score):
            return None, False
        return d, False
    except Exception as e:
        logger.warning(f"[scan] {code} 分析失败跳过: {str(e)[:80]}")
        return None, True


def _scan_lock_path(db_path: str) -> str:
    return db_path + '.scanlock'


@contextlib.contextmanager
def _hold_scan_lock(db_path: str, enabled: bool = True):
    """W25（006.md 阶段5）：扫描写库职责单一——同一时刻只允许一个持久化
    扫描（cron 18:00 与 Web「点扫描」互斥，拒绝并发，不排队）。

    用 flock 文件锁：进程退出/崩溃自动释放，无残留 running 状态。
    锁文件记录持有者 pid+时间，便于冲突时报错定位。
    """
    if not enabled:
        yield
        return
    import fcntl
    import os as _os
    path = _scan_lock_path(db_path)
    fh = open(path, 'a+')
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            import errno
            if e.errno in (errno.EACCES, errno.EAGAIN):
                fh.seek(0)
                who = (fh.read() or '').strip() or '其他进程'
                raise RuntimeError(
                    f"已有扫描在运行（{who}）。W25 约定：每日 18:00 cron 为"
                    "自选扫描固定写手；并发扫描被拒绝，请等其结束后重试。"
                ) from e
            raise
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid={_os.getpid()} scan_started={datetime.now().isoformat(timespec='seconds')}")
        fh.flush()
        yield
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        except Exception:
            pass
        fh.close()


def scan_market(limit: Optional[int] = None,
                watchlist: Optional[List[str]] = None,
                include_detail: bool = False,
                cfg: Optional[Dict] = None,
                universe: Optional[List[str]] = None,
                min_score: Optional[float] = None,
                no_persist: bool = False,
                progress_cb=None,
                job_holder: Optional[list] = None,
                scan_type: Optional[str] = None,
                force: bool = False) -> List[Dict[str, Any]]:
    """扫描入口（W25：持久化扫描全程持互斥锁；--force/no_persist 不加锁）。

    实现见 _scan_market_impl。db_path 解析口径与 MarketDataClient 一致。
    """
    db_path = MysteryDB(db_path=(cfg or {}).get('db_path')).db_path
    with _hold_scan_lock(db_path, enabled=(not force and not no_persist)):
        return _scan_market_impl(
            limit=limit, watchlist=watchlist, include_detail=include_detail,
            cfg=cfg, universe=universe, min_score=min_score,
            no_persist=no_persist, progress_cb=progress_cb,
            job_holder=job_holder, scan_type=scan_type, force=force)


def _scan_market_impl(limit: Optional[int] = None,
                watchlist: Optional[List[str]] = None,
                include_detail: bool = False,
                cfg: Optional[Dict] = None,
                universe: Optional[List[str]] = None,
                min_score: Optional[float] = None,
                no_persist: bool = False,
                progress_cb=None,
                job_holder: Optional[list] = None,
                scan_type: Optional[str] = None,
                force: bool = False) -> List[Dict[str, Any]]:
    """扫描市场，返回 AnalysisResult.to_dict() 列表（按分数降序）。

    :param watchlist: 指定代码列表（优先）
    :param universe: 自定义股票池（watchlist 为空时用）
    :param limit: 最多分析 N 只
    :param min_score: 只保留 >= 该分的股票
    :param include_detail: 是否带 VAP-ATR/平台明细（三类信号需要）
    :param no_persist: True 只打印不写库
    :param progress_cb: 可选回调 progress_cb(done, total)，每处理一只调用一次（后台扫描进度）
    :param job_holder: 可选 list，落库后把 job_id append 进去（后台扫描捕获任务号）
    :param scan_type: 落库类型（W18 同类型只保留最新一份）；None 自动推断：
           watchlist→'watchlist'、universe→'sector'、否则 'market'
    :param force: 跳过 W25 扫描互斥锁（仅脚本调试用）
    """
    if scan_type is None:
        if watchlist:
            scan_type = "watchlist"
        elif universe is not None:
            scan_type = "sector"
        else:
            scan_type = "market"
    svc = AnalysisService(cfg)
    if watchlist:
        codes = list(watchlist)
    elif universe is not None:
        # 显式传入空股票池：不落回全市场（避免误扫全 A 卡死）
        codes = list(universe)
    else:
        codes = [s['code'] for s in svc.market.fetch_stock_list()]
    if limit:
        codes = codes[:limit]

    # W15：全市场扫描并行（对齐 daily W11/W13）。实测单只 ~0.9s，5222 只
    # 串行 ~80min；ThreadPool 受 GIL 限制几乎无收益（32 只 29.7s vs 串行 29.3s），
    # 进程池实测 2x（32 只 14.6s）——多进程各自独立 GIL。
    # 优先进程池；Pool 创建失败（受限环境）回退线程池；小列表直接串行。
    # workers 优先级：env MYSTERY_SCAN_WORKERS > cfg.scan.workers > 4。
    workers = max(1, int(os.environ.get('MYSTERY_SCAN_WORKERS')
                         or (cfg or {}).get('scan', {}).get('workers') or 4))
    total = len(codes)
    results: List[Dict[str, Any]] = []
    failed = 0
    done = 0

    if workers > 1 and total >= 16:
        import multiprocessing as mp
        from functools import partial
        try:
            ctx = mp.get_context('fork') if 'fork' in mp.get_all_start_methods() \
                else mp.get_context()
            # 切成小批（每批 16 只）再派发：任务数越多 worker 分配越均衡
            # （实测 128 只：batch=16 四进程 1.7x vs batch=64 仅 1.35x），
            # 进度条平滑推进，且每 worker 只建一次 AnalysisService。
            batch = 16
            chunks = [codes[i:i + batch] for i in range(0, len(codes), batch)]
            with ctx.Pool(processes=workers) as pool:
                for part, part_failed, part_filtered in pool.imap_unordered(
                        partial(_scan_worker, cfg=cfg,
                                include_detail=include_detail,
                                min_score=min_score),
                        chunks, chunksize=1):
                    results.extend(part)
                    failed += part_failed
                    done += len(part) + part_failed + part_filtered
                    if progress_cb is not None:
                        try:
                            progress_cb(done, total)
                        except Exception:
                            pass
            logger.info(f"[scan] 完成 {len(results)} 只（失败 {failed} 只，"
                        f"processes={workers}）")
            results.sort(key=lambda x: (x.get('score') is not None,
                                        float(x.get('score') or -1)), reverse=True)
            return _persist_or_return(results, failed, no_persist, svc,
                                      job_holder, scan_type)
        except Exception as e:
            logger.warning(f"[scan] 进程池不可用，回退线程池: {str(e)[:100]}")

    # 线程池回退 / workers==1 / 小列表：共享 svc 单实例（W13 线程安全）
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(_scan_thread_worker, code, svc,
                          include_detail, min_score): code for code in codes}
        for fut in as_completed(futs):
            done += 1
            d, is_fail = fut.result()
            if is_fail:
                failed += 1
            elif d is not None:
                results.append(d)
            if progress_cb is not None:
                try:
                    progress_cb(done, total)
                except Exception:
                    pass
    logger.info(f"[scan] 完成 {len(results)} 只（失败 {failed} 只，"
                f"threads={max(1, workers)}）")
    results.sort(key=lambda x: (x.get('score') is not None,
                                float(x.get('score') or -1)), reverse=True)
    return _persist_or_return(results, failed, no_persist, svc, job_holder,
                              scan_type)


def _persist_or_return(results: List[Dict[str, Any]], failed: int,
                       no_persist: bool, svc: AnalysisService,
                       job_holder: Optional[list],
                       scan_type: str = "market") -> List[Dict[str, Any]]:
    """扫描结果落库（scan_jobs/scan_results）或仅返回。与历史行为一致。"""

    if not no_persist and results:
        try:
            trade_date = max((str(r.get('trade_date', '')) for r in results),
                             default='')
            job_id = _write_scan_batch(svc.market.db, results, failed,
                                       trade_date, scan_type=scan_type)
            if job_holder is not None:
                try:
                    job_holder.append(job_id)
                except Exception:
                    pass
            logger.info(f"[scan] 已写库 job_id={job_id}（{len(results)} 只，失败 {failed}）")
        except Exception as e:  # 写库失败不阻断扫描结果
            logger.warning(f"[scan] 写库失败（不影响本次结果）: {str(e)[:100]}")
    elif no_persist:
        logger.info("[scan] no_persist：仅打印，未写库")
    logger.info(f"[scan] 完成 {len(results)} 只（失败 {failed} 只）")
    return results


def latest_scan_job(cfg: Optional[Dict] = None) -> Optional[int]:
    """最近一次成功写库的 job_id（Web 真三振池用）。"""
    svc = AnalysisService(cfg)
    conn = svc.market.db._connect()
    try:
        row = conn.execute(
            "SELECT id FROM scan_jobs ORDER BY id DESC LIMIT 1").fetchone()
        return int(row[0]) if row else None
    finally:
        conn.close()


def scan_results_of(job_id: int, signal: Optional[str] = None,
                    cfg: Optional[Dict] = None) -> List[Dict[str, Any]]:
    """读取某次 job 的结果（signal 过滤 true_resonance/vap_atr/chip_low）。"""
    svc = AnalysisService(cfg)
    conn = svc.market.db._connect()
    try:
        sql = "SELECT payload_json FROM scan_results WHERE job_id=?"
        params: List[Any] = [job_id]
        if signal == "true_resonance":
            sql += " AND true_resonance=1"
        elif signal == "vap_atr":
            sql += " AND vap_atr_break=1"
        elif signal == "chip_low":
            sql += " AND chip_low=1"
        rows = conn.execute(sql, params).fetchall()
        out = []
        for (payload,) in rows:
            try:
                out.append(json.loads(payload))
            except Exception:
                continue
        return out
    finally:
        conn.close()
