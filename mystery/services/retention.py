"""mystery.services.retention - 历史数据定时清理（W101）。

背景：生产库 3GB 中，真正分析输入仅日K 1.05GB + 板块 70MB；
scan_results 历史扫描结果 826MB、analysis_cache/chan_cache 陈旧缓存、
以及"重写端钩子前"的历史 K 线冗余，都不是分析输入，随日期无限累积。

本模块提供统一的、可 dry-run、可定时调度的清理能力，类型：
  1. scan_jobs/scan_results：按 started_at 保留最近 N 天（含 replay，
     用户 2026-11-18 拍板「回放 job 也按天数清理」）；孤儿结果兜底。
  2. analysis_cache/chan_cache：按 trade_date 保留最近 N 天；
     sync 更新 K 线本会使缓存失效，但早期残留不会被触达，需按天兜底。
  3. stock_kline_data.daily：每票最多保留 cap 根（复用 W45 口径），
     处理写端 _prune_daily 钩子上线前的存量/被外部回写绕过的票。
  4. 可选 VACUUM：仅当空闲页占比超阈值才执行（auto_vacuum=0，
     不回收则文件不缩小）；避开每次清理都锁库数分钟。

安全约束（不可协商）：
  - 默认 dry_run=True，必须显式 apply 才真删；
  - 每种清理都用"保留边界"查询，绝不无条件 DELETE；
  - replay 与其它 scan_type 一视同仁按天数（可配 replay_days 单独放宽）；
  - 全部在单事务内完成，失败回滚；
  - 返回结构化统计，便于日志/审计。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ---- 保留策略（全部可用 env 覆盖，唯一事实源仍是调用方注入的 env） ----
DEFAULT_SCAN_DAYS = int(os.environ.get("MYSTERY_CLEAN_SCAN_DAYS", "30"))
DEFAULT_REPLAY_DAYS = int(os.environ.get("MYSTERY_CLEAN_REPLAY_DAYS", "180"))
DEFAULT_CACHE_DAYS = int(os.environ.get("MYSTERY_CLEAN_CACHE_DAYS", "30"))
DEFAULT_KLINE_CAP = int(os.environ.get("MYSTERY_CLEAN_KLINE_CAP", "2000"))
# VACUUM 触发阈值：空闲页占比超过此值才回收（0=每次清理都回收；>=1=禁用）
VACUUM_FREELIST_RATIO = float(os.environ.get("MYSTERY_CLEAN_VACUUM_RATIO", "0.15"))


def _cutoff(days: int) -> str:
    """返回 YYYY-MM-DD 的保留边界：早于该日期的视为过期。"""
    return (datetime.now() - timedelta(days=int(days))).strftime("%Y-%m-%d")


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def _date_expr(trade_date_col: str = "trade_date") -> str:
    """把可能为 'YYYY-MM-DD HH:MM:SS' / 'YYYY-MM-DD' 的列截成日期。"""
    return f"substr(CAST({trade_date_col} AS TEXT),1,10)"


def cleanup_scan_history(conn: sqlite3.Connection, *,
                         scan_days: int = DEFAULT_SCAN_DAYS,
                         replay_days: int = DEFAULT_REPLAY_DAYS,
                         apply: bool = False) -> Dict[str, int]:
    """清理过期 scan_jobs + 对应 scan_results，并处理孤儿结果。

    - 普通 scan_type：started_at 早于 scan_days 天前 → 删 job 及其结果；
    - replay：单独用 replay_days（默认 180 天，比普通宽松，保住历史回放对照）；
    - 孤儿 scan_results（job_id 已不存在）无条件删除（防御性，正常为 0）。

    started_at 为 ISO 'YYYY-MM-DDTHH:MM:SS'，按字符串比日期即可。
    """
    # started_at 形如 2026-11-14T...，取前10位为 YYYY-MM-DD
    cut_scan = _cutoff(scan_days)
    cut_replay = _cutoff(replay_days)

    # 先统计候选 job id
    jobs = conn.execute(
        "SELECT id, scan_type, substr(CAST(started_at AS TEXT),1,10) d "
        "FROM scan_jobs").fetchall()
    del_ids = []
    for jid, stype, d in jobs:
        if not d:
            continue
        limit = cut_replay if str(stype).startswith("replay") else cut_scan
        if d < limit:
            del_ids.append(int(jid))
    del_ids.sort()

    n_jobs = len(del_ids)

    def _result_count() -> int:
        if not del_ids:
            return 0
        total = 0
        ids = list(del_ids)
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            ph = ",".join("?" * len(chunk))
            total += conn.execute(
                f"SELECT COUNT(*) FROM scan_results WHERE job_id IN ({ph})",
                chunk).fetchone()[0]
        return total

    n_results = _result_count()

    if apply and del_ids:
        # 分批删除，避免超过 SQLite 变量上限；n_results 上面已统计，无需重复计数
        for i in range(0, len(del_ids), 400):
            chunk = del_ids[i:i + 400]
            ph = ",".join("?" * len(chunk))
            conn.execute(f"DELETE FROM scan_results WHERE job_id IN ({ph})", chunk)
            conn.execute(f"DELETE FROM scan_jobs WHERE id IN ({ph})", chunk)

    # 孤儿结果（无论 apply 与否都先统计）
    orphan = conn.execute(
        "SELECT COUNT(*) FROM scan_results "
        "WHERE job_id NOT IN (SELECT id FROM scan_jobs)").fetchone()[0]
    if apply and orphan:
        conn.execute(
            "DELETE FROM scan_results WHERE job_id NOT IN "
            "(SELECT id FROM scan_jobs)")

    return {
        "scan_jobs_expired": n_jobs,
        "scan_results_expired": n_results,
        "scan_results_orphan": orphan,
        "cutoff_scan": cut_scan,
        "cutoff_replay": cut_replay,
    }


def cleanup_analysis_cache(conn: sqlite3.Connection, *,
                           cache_days: int = DEFAULT_CACHE_DAYS,
                           apply: bool = False) -> Dict[str, int]:
    """按 trade_date 清理陈旧 analysis_cache / chan_cache（含早期单票残留）。"""
    cut = _cutoff(cache_days)
    out: Dict[str, int] = {}
    for table in ("analysis_cache", "chan_cache"):
        if not _table_exists(conn, table):
            out[table] = 0
            continue
        n = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {_date_expr('trade_date')} < ?",
            (cut,)).fetchone()[0]
        out[table] = n
        if apply and n:
            conn.execute(
                f"DELETE FROM {table} WHERE {_date_expr('trade_date')} < ?",
                (cut,))
    out["cutoff_cache"] = cut
    return out


def cleanup_stale_kline(conn: sqlite3.Connection, *,
                        cap: int = DEFAULT_KLINE_CAP,
                        apply: bool = False) -> Dict[str, int]:
    """每票 daily K 线超 cap 根删最旧（复用 W45 `_prune_daily` 口径）。

    注意：写端已有 `_prune_daily` 钩子，本函数主要兜底"外部回写绕过钩子"
    或钩子上线前存量。cap<=0 视为关闭。

    事务：不在函数内 commit/rollback，完全由外层 `run_cleanup` 的单一事务
    控制，保证四类清理"全成功或全回滚"（早期版本每 500 票 commit 会破坏
    外层原子性，已废弃）。
    """
    if cap <= 0:
        return {"kline_cap": 0, "kline_over_codes": 0, "kline_rows": 0}
    over = conn.execute(
        "SELECT code, COUNT(*)-? n FROM stock_kline_data WHERE period='daily' "
        "GROUP BY code HAVING COUNT(*) > ?", (cap, cap)).fetchall()
    total = sum(int(r[1]) for r in over)
    deleted = 0
    if apply:
        for code, _n in over:
            cur = conn.execute(
                "DELETE FROM stock_kline_data WHERE code=? AND period='daily' "
                "AND date <= (SELECT date FROM stock_kline_data WHERE code=? "
                "AND period='daily' ORDER BY date DESC LIMIT 1 OFFSET ?)",
                (code, code, cap))
            deleted += cur.rowcount
    return {
        "kline_cap": cap,
        "kline_over_codes": len(over),
        "kline_rows": deleted if apply else total,
    }


def maybe_vacuum(conn: sqlite3.Connection, *, apply: bool,
                 ratio_threshold: float = VACUUM_FREELIST_RATIO) -> Dict[str, Any]:
    """按空闲页占比决定是否 VACUUM（auto_vacuum=0 时文件不缩小）。

    ratio_threshold <= 0 → 每次都回收；>= 1 → 禁用。
    """
    if not apply:
        return {"vacuum": False, "reason": "dry-run"}
    if ratio_threshold >= 1:
        return {"vacuum": False, "reason": "disabled"}
    try:
        pc = conn.execute("PRAGMA page_count").fetchone()[0]
        fc = conn.execute("PRAGMA freelist_count").fetchone()[0]
        ratio = (fc / pc) if pc else 0.0
    except Exception:
        return {"vacuum": False, "reason": "pragma_unavailable"}
    if ratio_threshold > 0 and ratio < ratio_threshold:
        return {"vacuum": False, "reason": "below_threshold",
                "freelist_ratio": round(ratio, 4)}
    t0 = time.time()
    try:
        conn.execute("VACUUM")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[retention] VACUUM 失败: {e}")
        return {"vacuum": False, "reason": "error", "error": str(e)}
    return {"vacuum": True, "reason": "threshold_met",
            "freelist_ratio": round(ratio, 4),
            "seconds": round(time.time() - t0, 1)}


def run_cleanup(db_path: str, *, apply: bool = False,
                scan_days: int = DEFAULT_SCAN_DAYS,
                replay_days: int = DEFAULT_REPLAY_DAYS,
                cache_days: int = DEFAULT_CACHE_DAYS,
                kline_cap: int = DEFAULT_KLINE_CAP,
                do_vacuum: bool = True,
                vacuum_ratio: float = VACUUM_FREELIST_RATIO) -> Dict[str, Any]:
    """统一入口：串起四类清理 + 可选 VACUUM。

    :param apply: False=dry-run（只统计不删），True=真删
    :return: {'applied': bool, 'db': path, 'scan':..., 'cache':..., 'kline':..., 'vacuum':...}
    """
    conn = sqlite3.connect(db_path, timeout=60)
    conn.execute("PRAGMA busy_timeout=60000")
    result: Dict[str, Any] = {
        "applied": bool(apply), "db": db_path,
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        if apply:
            conn.execute("BEGIN")
        result["scan"] = cleanup_scan_history(
            conn, scan_days=scan_days, replay_days=replay_days, apply=apply)
        result["cache"] = cleanup_analysis_cache(
            conn, cache_days=cache_days, apply=apply)
        result["kline"] = cleanup_stale_kline(
            conn, cap=kline_cap, apply=apply)
        if apply:
            conn.commit()
        result["vacuum"] = maybe_vacuum(
            conn, apply=(apply and do_vacuum), ratio_threshold=vacuum_ratio)
        result["ok"] = True
    except Exception as e:  # noqa: BLE001
        if apply:
            conn.rollback()
        logger.exception("[retention] 清理失败，已回滚")
        result["ok"] = False
        result["error"] = str(e)
    finally:
        conn.close()
    result["finished_at"] = datetime.now().isoformat(timespec="seconds")
    return result


def format_report(result: Dict[str, Any]) -> str:
    """把结构化结果格式化为终端/日志可读文本。"""
    mode = "APPLY" if result.get("applied") else "DRY-RUN"
    lines = [f"[retention] === {mode} {result.get('db', '')} ==="]
    if not result.get("ok"):
        lines.append(f"  失败: {result.get('error', 'unknown')}")
        return "\n".join(lines)
    s = result.get("scan") or {}
    lines.append(f"  扫描历史: 过期job {s.get('scan_jobs_expired', 0)} "
                 f"/ 过期结果 {s.get('scan_results_expired', 0)} "
                 f"/ 孤儿结果 {s.get('scan_results_orphan', 0)} "
                 f"(普通<{s.get('cutoff_scan')}, replay<{s.get('cutoff_replay')})")
    c = result.get("cache") or {}
    lines.append(f"  陈旧缓存: analysis_cache {c.get('analysis_cache', 0)} "
                 f"/ chan_cache {c.get('chan_cache', 0)} "
                 f"(<{c.get('cutoff_cache')})")
    k = result.get("kline") or {}
    lines.append(f"  冗余日K: 超{k.get('kline_cap')}根票 {k.get('kline_over_codes', 0)} "
                 f"/ 行 {k.get('kline_rows', 0)}")
    v = result.get("vacuum") or {}
    lines.append(f"  VACUUM: {v} ")
    return "\n".join(lines)
