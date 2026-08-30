"""mystery.services.scan — 全市场扫描（只调 analyze_one_stock）。

与个股页 / daily 同源同分（同一 Service）。单票失败不中断（大规模扫描容错）。
W1-B：每条结果带三类信号（core.scan_signals.classify），末尾写
scan_jobs / scan_results 落库；``no_persist=True`` 只打印不写库。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..core.scan_signals import classify
from .analyze import AnalysisService

logger = logging.getLogger(__name__)


def _write_scan_batch(db, results: List[Dict[str, Any]], failed: int,
                      trade_date: Optional[str] = None) -> Optional[int]:
    """写一笔 scan_jobs + 全量 scan_results，返回 job_id。"""
    import sqlite3
    conn = db._connect()
    try:
        started = datetime.now().isoformat(timespec="seconds")
        cur = conn.execute(
            "INSERT INTO scan_jobs (trade_date, started_at, finished_at, "
            "n_ok, n_fail) VALUES (?,?,?,?,?)",
            (trade_date or "", started, started, len(results), failed))
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


def scan_market(limit: Optional[int] = None,
                watchlist: Optional[List[str]] = None,
                include_detail: bool = False,
                cfg: Optional[Dict] = None,
                universe: Optional[List[str]] = None,
                min_score: Optional[float] = None,
                no_persist: bool = False,
                progress_cb=None,
                job_holder: Optional[list] = None) -> List[Dict[str, Any]]:
    """扫描市场，返回 AnalysisResult.to_dict() 列表（按分数降序）。

    :param watchlist: 指定代码列表（优先）
    :param universe: 自定义股票池（watchlist 为空时用）
    :param limit: 最多分析 N 只
    :param min_score: 只保留 >= 该分的股票
    :param include_detail: 是否带 VAP-ATR/平台明细（三类信号需要）
    :param no_persist: True 只打印不写库
    :param progress_cb: 可选回调 progress_cb(done, total)，每处理一只调用一次（后台扫描进度）
    :param job_holder: 可选 list，落库后把 job_id append 进去（后台扫描捕获任务号）
    """
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

    results: List[Dict[str, Any]] = []
    failed = 0
    total = len(codes)
    for i, code in enumerate(codes, 1):
        try:
            r = svc.analyze_one_stock(code, include_detail=include_detail)
            d = r.to_dict()
            d.update(classify(d))
            if min_score is None or (d.get('score') is not None
                                     and float(d['score']) >= min_score):
                results.append(d)
        except Exception as e:
            failed += 1
            logger.warning(f"[scan] {code} 分析失败跳过: {str(e)[:80]}")
        if progress_cb is not None:
            try:
                progress_cb(i, total)
            except Exception:
                pass
    results.sort(key=lambda x: (x.get('score') is not None,
                                float(x.get('score') or -1)), reverse=True)

    if not no_persist and results:
        try:
            trade_date = max((str(r.get('trade_date', '')) for r in results),
                             default='')
            job_id = _write_scan_batch(svc.market.db, results, failed, trade_date)
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
