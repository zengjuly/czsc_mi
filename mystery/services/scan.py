"""mystery.services.scan — 全市场扫描（只调 analyze_one_stock）。

与个股页 / daily 同源同分（同一 Service）。单票失败不中断（大规模扫描容错）。
W1-B：每条结果带三类信号（core.scan_signals.classify），末尾写
scan_jobs / scan_results 落库；``no_persist=True`` 只打印不写库。
W69：落库前把三浪主升结论（core.wave3）合并进每条结果，随 payload_json
落库；只读评估、异常隔离，不改变综合评分与信号分类。
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..core.scan_signals import classify
from ..store.db import MysteryDB
from .analyze import AnalysisService, MarketContextCache
from .task_lock import TaskBusyError, clear_progress, report_progress

logger = logging.getLogger(__name__)

# 扫描前自动同步：最新交易日的行情覆盖率下限（低于此值视为「数据不是最新」）。
# 只看 MAX(date) 太宽松——库里少量票有当天数据就会误判已最新。
_MIN_COVERAGE = float(os.environ.get('MYSTERY_AUTO_SYNC_MIN_COVERAGE', '0.8'))


# W77：每种 scan_type 保留最近 N 份 job（默认 5，可用 MYSTERY_SCAN_JOB_KEEP 调）。
# 旧行为（W18）是每次写库先删光同类型历史 job，导致「最近扫描任务与结果」
# 永远只有全市场/自选/板块各一条，后台扫描的历史记录无从查起。
# 保留有限份而非无限累积：全市场一次 ~5578 行 payload_json，5 份约几十 MB，
# 生产库 2.4G 量级下可接受；设 1 即回到 W18 旧语义。
_SCAN_JOB_KEEP_ENV = "MYSTERY_SCAN_JOB_KEEP"
_SCAN_JOB_KEEP_DEFAULT = 5
_SCAN_JOB_KEEP_MAX = 50


def _scan_job_keep() -> int:
    try:
        n = int(os.environ.get(_SCAN_JOB_KEEP_ENV, _SCAN_JOB_KEEP_DEFAULT))
    except (TypeError, ValueError):
        n = _SCAN_JOB_KEEP_DEFAULT
    return max(1, min(_SCAN_JOB_KEEP_MAX, n))


# W79：扫描 job 可解释性列（范围/股票池/limit/耗时/三浪开关）。
# 老库未应用 006 迁移时这些列不存在——所有写入/查询都做动态列探测，
# 缺失即退化为旧行为（零回归）。
_SCAN_META_COLS = ("scope", "universe_n", "limit_n", "duration_sec",
                   "wave3_enabled", "wave3_mode")


def _infer_scan_scope(watchlist, universe, limit, scan_type: str) -> str:
    """W79：判定本次扫描范围（避免自选扫描被误当成全市场）。"""
    if scan_type == "board":
        return "board"
    if watchlist:
        return "watchlist"
    if universe is not None:
        return "sector"
    if limit:
        return "limit"
    return "full"


def _build_scan_meta(watchlist, universe, limit, scan_type: str,
                     universe_n: int, t0: float) -> Dict[str, Any]:
    """W79：构造一次扫描的可解释性元数据（纯计算 + 只读运行时配置，不改评分/信号）。

    ``_t0`` 为内部键（扫描起始 time.time()），写库前用于计算 duration_sec，
    不会落到 scan_jobs。三浪开关/模式经 runtime_settings 稀疏覆盖层读取，
    与 W76 网页配置同源；配置读取失败降级为「关闭」，绝不影响扫描主流程。
    """
    wave3_enabled, wave3_mode = 0, "off"
    try:
        from . import runtime_settings as _rts
        eff = _rts.scan_wave3_options_effective()
        on = str(eff.get("MYSTERY_SCAN_WAVE3", "1")).strip() not in (
            "0", "false", "False", "")
        wave3_enabled = 1 if on else 0
        wave3_mode = _rts.scan_wave3_mode(eff) if on else "off"
    except Exception:  # noqa: BLE001 元数据读取失败不得影响扫描
        pass
    return {
        "scope": _infer_scan_scope(watchlist, universe, limit, scan_type),
        "universe_n": int(universe_n),
        "limit_n": int(limit) if limit else None,
        "wave3_enabled": wave3_enabled,
        "wave3_mode": wave3_mode,
        "started_at": datetime.fromtimestamp(t0).isoformat(timespec="seconds"),
        "_t0": t0,
    }


def _scan_job_cols(conn) -> set:
    """返回 scan_jobs 现有列名集合（PRAGMA，兼容未迁移老库）。"""
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(scan_jobs)")}
    except Exception:  # noqa: BLE001
        return set()


def _apply_scan_job_meta(conn, job_id: int, meta: Optional[Dict[str, Any]],
                         finished_iso: str) -> None:
    """W79：给已插入的 scan_job 补写可解释性列（动态列探测，老库/无 meta 安全）。

    INSERT 语句保持 6 列不变（所有既有调用点零回归）；本 UPDATE 仅在 W79 列
    全部存在且传入 meta 时执行。主路径（_write_scan_batch）与断点续跑收尾
    （_finalize_resume_job）共用，保证每条 job 都有范围/耗时/三浪口径。
    """
    if not meta:
        return
    cols = _scan_job_cols(conn)
    if not all(c in cols for c in _SCAN_META_COLS):
        return
    duration = round(max(0.0, time.time() - float(meta.get("_t0", time.time()))), 1)
    conn.execute(
        "UPDATE scan_jobs SET scope=?, universe_n=?, limit_n=?, "
        "duration_sec=?, wave3_enabled=?, wave3_mode=?, "
        "started_at=?, finished_at=? WHERE id=?",
        (meta.get("scope", ""), meta.get("universe_n"), meta.get("limit_n"),
         duration, 1 if meta.get("wave3_enabled") else 0,
         meta.get("wave3_mode", ""), meta.get("started_at"),
         finished_iso, job_id))


def _write_scan_batch(db, results: List[Dict[str, Any]], failed: int,
                      trade_date: Optional[str] = None,
                      scan_type: str = "market",
                      meta: Optional[Dict[str, Any]] = None) -> Optional[int]:
    """写一笔 scan_jobs + 全量 scan_results，返回 job_id。

    W77：同类型扫描保留最近 N 份（默认 5）——先插新 job，再清理排名超过 N
    的老 job 及其 scan_results。W18 的「只保留最新一份」会让「最近任务」
    看不到多次后台扫描，故改为有限历史；``MYSTERY_SCAN_JOB_KEEP=1`` 可回退
    旧语义。新插入的 job 始终保留，不受清理影响。

    W79：可选 ``meta``（由 _build_scan_meta 构造）经 _apply_scan_job_meta 补写
    范围/耗时/三浪开关列；meta=None 或老库未迁移时保持原行为（零回归）。
    """
    import sqlite3
    conn = db._connect()
    try:
        started = datetime.now().isoformat(timespec="seconds")
        cur = conn.execute(
            "INSERT INTO scan_jobs (trade_date, started_at, finished_at, "
            "n_ok, n_fail, scan_type) VALUES (?,?,?,?,?,?)",
            (trade_date or "", started, started, len(results), failed, scan_type))
        job_id = cur.lastrowid
        _apply_scan_job_meta(conn, job_id, meta, started)
        # W60（P2-4 同思路）：逐行 INSERT → executemany 批量，5000+ 票从
        # 数千次往返降为单次批量。
        _rows = [
            (job_id,
             str(r.get("symbol", "")),
             str(r.get("trade_date", "")),
             r.get("score"),
             1 if r.get("true_resonance") else 0,
             1 if r.get("vap_atr_break") else 0,
             1 if r.get("chip_low") else 0,
             json.dumps(r, ensure_ascii=False))
            for r in results
        ]
        if _rows:
            conn.executemany(
                "INSERT INTO scan_results (job_id, symbol, trade_date, score, "
                "true_resonance, vap_atr_break, chip_low, payload_json) "
                "VALUES (?,?,?,?,?,?,?,?)", _rows)
        # 清理：同类型除本次 job 外，只留最新 keep-1 份，更老的连结果一起删。
        keep_older = _scan_job_keep() - 1
        conn.execute(
            "DELETE FROM scan_results WHERE job_id IN ("
            " SELECT id FROM scan_jobs WHERE scan_type=? AND id<>? "
            " ORDER BY id DESC LIMIT -1 OFFSET ?)",
            (scan_type, job_id, keep_older))
        conn.execute(
            "DELETE FROM scan_jobs WHERE id IN ("
            " SELECT id FROM scan_jobs WHERE scan_type=? AND id<>? "
            " ORDER BY id DESC LIMIT -1 OFFSET ?)",
            (scan_type, job_id, keep_older))
        conn.commit()
        return job_id
    finally:
        conn.close()


def _open_resume_job(db, scan_type: str, trade_date: str):
    """断点续跑：开/复用同 scan_type+交易日 的 working job。

    返回 (job_id, done_symbols_set)。若已存在同类型同交易日 job 则复用
    （取其已扫个股为 done），否则新建空 job。崩溃后二次 resume 仍可续。
    """
    conn = db._connect()
    try:
        row = conn.execute(
            "SELECT id FROM scan_jobs WHERE scan_type=? AND trade_date=? "
            "ORDER BY id DESC LIMIT 1", (scan_type, trade_date)).fetchone()
        if row:
            job_id = int(row[0])
            done = {r[0] for r in conn.execute(
                "SELECT DISTINCT symbol FROM scan_results WHERE job_id=?",
                (job_id,)).fetchall()}
            return job_id, done
        cur = conn.execute(
            "INSERT INTO scan_jobs (trade_date, started_at, finished_at, "
            "n_ok, n_fail, scan_type) VALUES (?,?,?,?,?,?)",
            (trade_date, datetime.now().isoformat(timespec="seconds"),
             datetime.now().isoformat(timespec="seconds"), 0, 0, scan_type))
        conn.commit()
        return cur.lastrowid, set()
    finally:
        conn.close()


def _append_scan_results(db, job_id: int, results: List[Dict[str, Any]],
                         failed_inc: int) -> None:
    """向 working job 增量追加结果（PRIMARY KEY(job_id,symbol) 幂等覆盖）。"""
    import sqlite3
    conn = db._connect()
    try:
        # W60：逐行 INSERT OR REPLACE → executemany 批量（断点续跑增量落库，
        # PRIMARY KEY(job_id,symbol) 幂等覆盖语义不变）。
        _rows = [
            (job_id, str(r.get("symbol", "")),
             str(r.get("trade_date", "")), r.get("score"),
             1 if r.get("true_resonance") else 0,
             1 if r.get("vap_atr_break") else 0,
             1 if r.get("chip_low") else 0,
             json.dumps(r, ensure_ascii=False))
            for r in results
        ]
        if _rows:
            conn.executemany(
                "INSERT OR REPLACE INTO scan_results "
                "(job_id, symbol, trade_date, score, true_resonance, "
                "vap_atr_break, chip_low, payload_json) VALUES (?,?,?,?,?,?,?,?)",
                _rows)
        if failed_inc:
            conn.execute(
                "UPDATE scan_jobs SET n_fail = COALESCE(n_fail,0) + ? "
                "WHERE id=?", (failed_inc, job_id))
        conn.commit()
    finally:
        conn.close()


def _finalize_resume_job(db, job_id: int,
                         meta: Optional[Dict[str, Any]] = None) -> None:
    """收尾 working job：重算 n_ok、写 finished_at（n_fail 已在追加时累计）。

    W79：可选 meta 补写范围/耗时/三浪列（与 _write_scan_batch 同口径）。
    """
    conn = db._connect()
    try:
        n_ok = conn.execute(
            "SELECT COUNT(*) FROM scan_results WHERE job_id=?",
            (job_id,)).fetchone()[0] or 0
        finished = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            "UPDATE scan_jobs SET n_ok=?, finished_at=? WHERE id=?",
            (n_ok, finished, job_id))
        _apply_scan_job_meta(conn, job_id, meta, finished)
        conn.commit()
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
    # W63：本批 codes 一次性批量预取行业 + 财务，传入 analyze_one_stock，
    # 把 16 只 ×(行业 + 财务) 逐票查询降为 2 次 IN 查询（指数已有会话级
    # 缓存、行业 K 线已在 sector client 实例级缓存，不重复查）。
    from ..adapters.codes import db_code_of
    _norm = lambda c: db_code_of(c) if not c.startswith(('sh.', 'sz.', 'bj.')) else c
    _db_codes = [_norm(c) for c in codes_batch]
    _ctx_cache = MarketContextCache(
        industries=svc.market.db.get_primary_industries(_db_codes),
        financials=svc.market.db.get_financials(_db_codes),
    )
    out: List[Dict[str, Any]] = []
    failed = 0
    filtered = 0
    for code in codes_batch:
        try:
            r = svc.analyze_one_stock(code, include_detail=include_detail,
                                      use_cache=use_cache,
                                      fill_financial=False)
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
                        include_detail: bool, min_score: Optional[float],
                        ctx_cache=None):
    """线程池 worker（共享 svc）：返回 (dict or None, is_failed)。"""
    try:
        from ..store.cache import scan_cache_enabled
        r = svc.analyze_one_stock(code, include_detail=include_detail,
                                  use_cache=scan_cache_enabled(),
                                  fill_financial=False, ctx_cache=ctx_cache)
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
                # W49：换 TaskBusyError（RuntimeError 子类，既有
                # except RuntimeError 调用点兼容），CLI 统一 exit 3
                raise TaskBusyError(
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


def _warn_fail_rate(results: List[dict], failed: int) -> None:
    """W41 #18：扫描失败率 >2% 时 warning（旧实现逐股失败只 debug/个股
    warning，整晚数据源挂了管线日志里也看不出异常，只有分数静默变少）。"""
    total = len(results) + failed
    if total >= 20 and failed / total > 0.02:
        logger.warning(f"[scan] ⚠️ 失败率 {failed}/{total} = "
                       f"{failed / total:.1%} > 2%——疑似数据源/接口异常，"
                       f"查当日管线日志与 ths/tdx 可用性")


def _lag_reason(db_latest: str, latest: str, cov_n: int, cov_all: int,
                coverage: float) -> str:
    """落后原因的精确表述（日期落后 / 日期已到但覆盖不足，两种情形分开写）。"""
    if not db_latest:
        return f"库内无 daily 行情，最新交易日 {latest}"
    if db_latest < latest:
        return f"库最新 {db_latest} < 最新交易日 {latest}"
    return (f"库最新 {db_latest} 覆盖不足（{cov_n}/{cov_all} = "
            f"{coverage:.0%} < {_MIN_COVERAGE:.0%}）")


def _ensure_scan_data(codes: List[str], svc: AnalysisService,
                      cfg: Optional[Dict], auto_sync: bool) -> str:
    """扫描前行情时效检查 + 自动同步（auto_sync 关时只报告不动作）。

    规则（与 calendar.get_latest_trade_date 同口径）：
    - 库内 daily 最新 >= 真实最新交易日 → 已最新，跳过；
    - 落后且**已收盘**（≥15:30）→ 触发一次全量行情同步（全市场增量；
      upsert 幂等按 date 覆盖，自然覆盖未收盘时写入的盘中行情）；
    - 落后且**未收盘** → 只更新本次扫描需要分析的股票（symbols=codes），
      不做全量同步（避免盘中全量拉取污染/超时）。
    同步失败不阻断扫描（warning 后继续）。
    """
    if not auto_sync:
        return "auto_sync 关闭，跳过行情检查"
    from ..adapters.calendar import (_CLOSE_HOUR, _CLOSE_MINUTE,
                                     get_latest_trade_date, is_trade_day)
    try:
        latest = get_latest_trade_date()
    except Exception as e:
        logger.warning(f"[scan] 获取最新交易日失败，跳过自动同步: {str(e)[:80]}")
        return f"获取最新交易日失败（{str(e)[:40]}），未同步"
    if not latest:
        return "无法获取最新交易日，跳过自动同步"
    try:
        conn = svc.market.db._connect()
        try:
            # W62：三连查询（MAX(date) + 两遍 COUNT(DISTINCT)）合并为单条，
            # 1 次 round-trip + 1 次全表扫描，语义零回归。空表时 db_latest=None、
            # cov_n/cov_all=0，与原分支行为一致。
            row = conn.execute(
                "SELECT "
                "(SELECT MAX(date) FROM stock_kline_data WHERE period='daily') "
                "AS db_latest, "
                "COUNT(DISTINCT CASE WHEN date = "
                "(SELECT MAX(date) FROM stock_kline_data WHERE period='daily') "
                "THEN code END) AS cov_n, "
                "COUNT(DISTINCT code) AS cov_all "
                "FROM stock_kline_data WHERE period='daily'"
            ).fetchone()
            db_latest = str(row[0]) if row and row[0] else ""
            cov_n = int(row[1] or 0)
            cov_all = int(row[2] or 0)
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"[scan] 库内行情最新日查询失败: {str(e)[:80]}")
        db_latest, cov_n, cov_all = "", 0, 0
    coverage = (cov_n / cov_all) if cov_all else 0.0
    if db_latest and db_latest >= latest and coverage >= _MIN_COVERAGE:
        return (f"行情已最新（库 {db_latest} ≥ 最新交易日 {latest}，"
                f"覆盖 {cov_n}/{cov_all} = {coverage:.0%}），无需同步")

    from .sync import sync_market
    now = datetime.now()
    # 已收盘 = 过 15:30 或今天非交易日（周末/节假日没有盘中行情，
    # 落后时直接全量补齐，不走「仅更新待分析股票」）
    closed = (now.hour > _CLOSE_HOUR or
              (now.hour == _CLOSE_HOUR and now.minute >= _CLOSE_MINUTE))
    if not closed:
        try:
            closed = not is_trade_day(now)
        except Exception:
            closed = False
    try:
        if closed:
            # 已收盘/非交易日：全量同步一次（DuckDB 批量预同步 + 增量，
            # upsert 幂等按 date 覆盖，自然覆盖盘中写入的未收盘行情）
            out = sync_market(cfg=cfg)
            return (f"行情落后（{_lag_reason(db_latest, latest, cov_n, cov_all, coverage)}），"
                    f"已收盘 → 全量同步：synced={out.get('synced')} "
                    f"failed={out.get('failed')}")
        # 未收盘：只更新本次扫描需要分析的股票
        out = sync_market(symbols=codes, cfg=cfg)
        return (f"行情落后（{_lag_reason(db_latest, latest, cov_n, cov_all, coverage)}），"
                f"未收盘 → 仅更新本次扫描 {len(codes)} 只："
                f"synced={out.get('synced')} failed={out.get('failed')}")
    except Exception as e:
        logger.warning(f"[scan] 自动行情同步失败（继续扫描）: {str(e)[:100]}")
        return f"自动行情同步失败（{str(e)[:60]}），继续扫描"


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
                force: bool = False,
                auto_sync: Optional[bool] = None,
                board: Optional[str] = None,
                resume: bool = False) -> List[Dict[str, Any]]:
    """扫描入口（W25：持久化扫描全程持互斥锁；--force/no_persist 不加锁）。

    实现见 _scan_market_impl。db_path 解析口径与 MarketDataClient 一致。
    :param auto_sync: 扫描前自动确保行情最新（None=默认开；no_persist/force
          调试路径默认关）。已收盘且行情落后 → 全量同步；未收盘 → 仅更新
          本次扫描股票；已最新 → 跳过。见 _ensure_scan_data。
    :param board: 仅扫描某个 tdx_local 自选板块（需先 sync-tdx-boards 同步）；
          结果会带 'board' 字段，scan_type 记为 'board'。
    """
    db_path = MysteryDB(db_path=(cfg or {}).get('db_path')).db_path
    with _hold_scan_lock(db_path, enabled=(not force and not no_persist)):
        # W49：包一层心跳写 progress_scan.json——Web 状态区实时显示
        # （手动后台扫描走 progress_cb，cron/CLI 直跑也留进度）。
        inner_cb = progress_cb

        def _cb(d: int, t: int, stage: str = '') -> None:
            # W75：第三参数 stage 用于「收尾阶段」独立刻度（三浪评估/写库）。
            # 心跳文件（系统状态页）收尾时保持满进度、只更新阶段 label，
            # 避免 5580/5580 突然回退到 0/200 造成误导；逐票阶段保持原样。
            if stage:
                # 恒定满进度 + 阶段 label；交给 report_progress 节流，
                # 避免限流后 200 次逐票回调每次都原子写文件。
                report_progress(db_path, 'scan', t, t, label=stage)
            else:
                report_progress(db_path, 'scan', d, t, label=stage)
            if inner_cb is not None:
                try:
                    inner_cb(d, t, stage)          # 新版 3 参数 cb（Web 后台）
                except TypeError:
                    try:
                        inner_cb(d, t)             # 兼容旧 2 参数 cb（CLI/日报）
                    except Exception:
                        pass
                except Exception:
                    pass

        # board 筛选：从已同步板块中取成分股作为本次 universe
        board_codes: Optional[List[str]] = None
        if board is not None and universe is None:
            from . import watchlist as _wl
            board_codes = _wl.codes_of_board(board)
            if not board_codes:
                logger.warning(f"[scan] 板块「{board}」无成分股（未同步或不存在）")
                return []

        try:
            results = _scan_market_impl(
                limit=limit, watchlist=watchlist, include_detail=include_detail,
                cfg=cfg, universe=(universe if universe is not None else board_codes),
                min_score=min_score,
                no_persist=no_persist, progress_cb=_cb,
                job_holder=job_holder,
                scan_type=(scan_type if scan_type is not None
                           else ('board' if board is not None else None)),
                force=force,
                auto_sync=auto_sync,
                resume=resume)
        finally:
            clear_progress(db_path, 'scan')
        # 标记板块归属（额外字段，不影响落库结构）
        if board is not None:
            for r in results:
                r['board'] = board
        return results


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
                force: bool = False,
                auto_sync: Optional[bool] = None,
                resume: bool = False) -> List[Dict[str, Any]]:
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
    :param auto_sync: 扫描前自动确保行情最新（None=默认开；no_persist/force
          调试路径默认关）
    """
    # W79：本次扫描的可解释性元数据（范围/股票池/耗时/三浪口径）。t0 从 impl
    # 入口计时，覆盖 auto_sync、逐票扫描、三浪收尾、写库；锁等待不计入。
    _t0 = time.time()
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
    # W79：codes 确定后构造一次 meta，透传到所有写库点（主路径 + resume 收尾）。
    _scan_meta = _build_scan_meta(watchlist, universe, limit, scan_type,
                                  len(codes), _t0)

    # 扫描前确保行情最新：已收盘且落后 → 全量同步；未收盘 → 仅更新
    # 本次扫描股票；已最新 → 跳过（no_persist/force 调试路径默认关）
    if auto_sync is None:
        auto_sync = not (no_persist or force)
    if auto_sync:
        note = _ensure_scan_data(codes, svc, cfg, auto_sync)
        if note:
            logger.info(f"[scan] {note}")

    # W89：全市场大池改走「阶段化流式流水线」——大盘→板块→个股，每 10 只
    # 一批 INSERT OR REPLACE 落盘（幂等、可续跑），批对象随即释放；组合层用
    # KB 级 assessment 合成后 json_set 回填，全程不读回 payload。
    # 小池 / 自选 / 板块 / no_persist / 三浪限流口径一律保持旧路径（零回归）。
    _w89 = False
    try:
        from .streaming_scan import streaming_enabled, run_streaming_scan
        _w89 = streaming_enabled(scan_type=scan_type, watchlist=watchlist,
                                 universe=universe, no_persist=no_persist,
                                 n_codes=len(codes))
    except ImportError:
        logger.warning("[scan] W89 流式模块不可用，回退既有扫描路径")
    except Exception as e:  # noqa: BLE001 仅分流检查容错，异常不吞扫描
        logger.warning(f"[scan] W89 流式分流检查失败，回退旧路径: {str(e)[:100]}")
    if _w89:
        # 执行不再包 try：流水线内部各阶段已硬隔离，若真出现未预期异常应
        # 直接抛出（CLI 台账记 error），避免静默回退旧路径把 5579 只重扫一遍。
        return run_streaming_scan(
            svc=svc, codes=codes, scan_type=scan_type, cfg=cfg,
            meta=_scan_meta, progress_cb=progress_cb,
            include_detail=include_detail, min_score=min_score,
            resume=resume, job_holder=job_holder)

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

    # ④ 断点续跑（--resume）：复用同 scan_type+交易日 的 working job，
    # 跳过已完成个股；扫描过程按 chunk 增量落库，崩溃可二次续跑。
    # 默认（resume=False）行为完全不变。
    resume_job_id = None
    if resume and not no_persist:
        # W94/D：回放复用 as_of 作为 resume 交易日键，避免把历史回放
        # 挂到当日正式 job 上（否则 --resume 会串到今天的台账）。
        _as_of = getattr(svc.market, "as_of", "") or ""
        if _as_of:
            _td = _as_of
        else:
            try:
                from ..adapters.calendar import get_latest_trade_date
                _td = get_latest_trade_date() or ""
            except Exception:
                _td = ""
        resume_job_id, _done = _open_resume_job(
            svc.market.db, scan_type, _td)
        if _done:
            codes = [c for c in codes if c not in _done]
            total = len(codes)
            if total == 0:
                _finalize_resume_job(svc.market.db, resume_job_id, meta=_scan_meta)
                logger.info(f"[scan] resume：无剩余个股，复用 job {resume_job_id}")
                return _resume_results(svc, resume_job_id, cfg)
        logger.info(f"[scan] resume：working job {resume_job_id}，"
                    f"剩余 {total} 只（已完成 {len(_done)}）")

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
                    if resume_job_id is not None:
                        _append_scan_results(svc.market.db, resume_job_id,
                                             part, part_failed)
            logger.info(f"[scan] 完成 {len(results)} 只（失败 {failed} 只，"
                        f"processes={workers}）")
            _warn_fail_rate(results, failed)  # W41 #18：失败率>2% 上 warning
            results.sort(key=lambda x: (x.get('score') is not None,
                                        float(x.get('score') or -1)), reverse=True)
            if resume_job_id is not None:
                _finalize_resume_job(svc.market.db, resume_job_id, meta=_scan_meta)
                return _resume_results(svc, resume_job_id, cfg)
            return _persist_or_return(results, failed, no_persist, svc,
                                      job_holder, scan_type,
                                      progress_cb=progress_cb,
                                      meta=_scan_meta)
        except Exception as e:
            logger.warning(f"[scan] 进程池不可用，回退线程池: {str(e)[:100]}")

    # 线程池回退 / workers==1 / 小列表：共享 svc 单实例（W13 线程安全）
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from ..adapters.codes import db_code_of
    _norm = lambda c: db_code_of(c) if not c.startswith(('sh.', 'sz.', 'bj.')) else c
    _db_codes = [_norm(c) for c in codes]
    _ctx_cache = MarketContextCache(
        industries=svc.market.db.get_primary_industries(_db_codes),
        financials=svc.market.db.get_financials(_db_codes),
    )
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(_scan_thread_worker, code, svc,
                          include_detail, min_score, _ctx_cache): code
                for code in codes}
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
    if resume_job_id is not None and results:
        _append_scan_results(svc.market.db, resume_job_id, results, failed)
    _warn_fail_rate(results, failed)  # W41 #18：失败率>2% 上 warning
    results.sort(key=lambda x: (x.get('score') is not None,
                                float(x.get('score') or -1)), reverse=True)
    if resume_job_id is not None:
        _finalize_resume_job(svc.market.db, resume_job_id, meta=_scan_meta)
        return _resume_results(svc, resume_job_id, cfg)
    return _persist_or_return(results, failed, no_persist, svc, job_holder,
                              scan_type, progress_cb=progress_cb,
                              meta=_scan_meta)


def _attach_wave3(results: List[Dict[str, Any]], *,
                  db=None, client=None, progress_cb=None) -> None:
    """W69：写库/返回前把三浪主升结论合并进结果（只读评估，异常硬隔离）。

    注入扫描用的 ``MarketDataClient`` 与 ``MysteryDB``，保证与本次扫描同源
    同库；任何异常都只记 warning，绝不改动既有结果或中断落库。
    W75：透传 ``progress_cb`` 供三浪收尾阶段上报独立刻度进度。
    """
    if not results:
        return
    # W84：指数条目（is_index=True）无三浪意义，单独剔除后只给个股附加，避免
    # 对指数空字段做无意义运算；指数条目仍随 results 落库与展示。
    stock_results = [r for r in results if not r.get("is_index")]
    if not stock_results:
        return
    try:
        from .wave3_scan import attach_wave3_to_results
        summary = attach_wave3_to_results(stock_results, db=db, client=client,
                                          progress_cb=progress_cb)
        if summary.get("enabled"):
            logger.info(
                f"[scan] 三浪主升已合并：候选 {summary.get('candidates')}/"
                f"{summary.get('scanned')}，组合总风险 "
                f"{summary.get('portfolio_risk')} 元，缩放 "
                f"×{summary.get('scale_down')}")
    except Exception as e:  # noqa: BLE001 附加策略不得影响既有扫描
        logger.warning(f"[scan] 三浪合并跳过（不影响既有扫描）: {str(e)[:100]}")


def _index_result(d: Dict[str, Any]) -> Dict[str, Any]:
    """W84：指数研判 → 兼容 scan_results 的结果 dict（落库 + Web/报告展示）。

    不含 turn/行业/财务（指数为纯价格），is_index=True 标记，供落库与渲染
    识别；candlestick 仅填 summary_daily/weekly 两段，复用既有形态列。
    """
    return {
        "is_index": True,
        "symbol": d.get("code", ""),
        "name": d.get("name", ""),
        "trade_date": d.get("date", ""),
        "score": None,
        "true_resonance": False,
        "vap_atr_break": False,
        "chip_low": False,
        "price": d.get("close"),
        "advice": "",
        "candlestick": {
            "summary_daily": d.get("candle_daily") or "—",
            "summary_weekly": d.get("candle_weekly") or "—",
            "period": "index", "lookback": 0, "n_bars": 0,
            "enabled": [], "n_hits": 0, "hits": [], "direction": "—",
        },
        "index_analysis": d,
    }


def _resume_results(svc: AnalysisService, job_id: int,
                    cfg: Optional[Dict]) -> List[Dict[str, Any]]:
    """W69：断点续跑收尾后统一附加三浪字段，并回写 payload_json 保持库表一致。"""
    rows = scan_results_of(job_id, cfg=cfg)
    if not rows:
        return rows
    symbols = [str(r.get("symbol", "")) for r in rows]
    _attach_wave3(rows, db=svc.market.db, client=svc.market)
    if not rows or not rows[0].get("wave3_portfolio"):
        return rows
    try:
        conn = svc.market.db._connect()
        try:
            conn.executemany(
                "UPDATE scan_results SET payload_json=? "
                "WHERE job_id=? AND symbol=?",
                [(json.dumps(r, ensure_ascii=False), job_id, sym)
                 for sym, r in zip(symbols, rows)])
            conn.commit()
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 回写失败不影响内存结果
        logger.warning(f"[scan] resume 三浪回写跳过（不影响既有扫描）: "
                       f"{str(e)[:100]}")
    return rows


def _persist_or_return(results: List[Dict[str, Any]], failed: int,
                       no_persist: bool, svc: AnalysisService,
                       job_holder: Optional[list],
                       scan_type: str = "market",
                       progress_cb=None,
                       meta: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """扫描结果落库（scan_jobs/scan_results）或仅返回。与历史行为一致。

    W69：落库/返回前把三浪主升结论合并进结果（只读评估，失败不影响既有扫描），
    字段随 payload_json 一起写入，日报 / Web / scan_results_of 直接可读。
    W75：收尾阶段（三浪评估 / 写库）上报进度，避免页面停在 100% 空转。
    W79：可选 meta 透传到 _write_scan_batch 补写范围/耗时/三浪列。
    """
    _attach_wave3(results, db=svc.market.db, client=svc.market,
                  progress_cb=progress_cb)
    # W84：全量/全市场扫描把各大指数并入结果（落库 + 日报独立区块）。
    # 自选/板块扫描不含指数（用户指定的个股池，混入宽基会污染池语义）。
    # W94/D：历史回放（scan_type='replay'）也并入 —— 指数经同一 as_of
    # 截断的 MarketDataClient 读取，回放报告能展示「当日」大盘研判。
    if scan_type in ("market", "full", "replay"):
        try:
            from .index_analysis import analyze_major_indexes
            for _d in analyze_major_indexes(svc.market):
                results.append(_index_result(_d))
        except Exception as e:  # noqa: BLE001 指数并入失败不影响个股扫描
            logger.warning(f"[scan] 各大指数并入跳过（不影响既有扫描）: "
                           f"{str(e)[:100]}")
    if progress_cb is not None:
        try:
            progress_cb(len(results), len(results), "收尾·写入扫描结果")
        except Exception:
            pass

    if not no_persist and results:
        try:
            trade_date = max((str(r.get('trade_date', '')) for r in results),
                             default='')
            job_id = _write_scan_batch(svc.market.db, results, failed,
                                       trade_date, scan_type=scan_type, meta=meta)
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


# W79：范围中文标签（Web 展示统一口径，避免自选扫描被误当成全市场）。
_SCOPE_CN = {
    "watchlist": "自选股", "limit": "全市场·限量", "full": "全市场",
    "sector": "板块池", "board": "自选板块",
}
_WAVE3_MODE_CN = {"auto": "自动限流", "fixed": "固定上限", "full": "强制全量",
                  "off": "关闭"}


def scan_job_meta(job_id: int, cfg: Optional[Dict] = None) -> Dict[str, Any]:
    """W79：读取单个 scan_job 的可解释性字段（范围/耗时/三浪口径，动态列兼容老库）。

    返回 dict 含原始列 + ``label``（可直接展示的中文一行摘要）+
    ``scope_cn`` / ``wave3_cn``。老库未迁移或无 W79 列时返回空 dict，
    Web 侧对空 dict 有兜底文案，历史 job 不受影响。
    """
    svc = AnalysisService(cfg)
    conn = svc.market.db._connect()
    try:
        cols = _scan_job_cols(conn)
        if not all(c in cols for c in _SCAN_META_COLS):
            return {}
        row = conn.execute(
            "SELECT scan_type, scope, universe_n, limit_n, duration_sec, "
            "wave3_enabled, wave3_mode, started_at, finished_at "
            "FROM scan_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return {}
        (scan_type, scope, universe_n, limit_n, duration, w3_on, w3_mode,
         started, finished) = row
        meta = {
            "job_id": job_id, "scan_type": scan_type, "scope": scope or "",
            "universe_n": universe_n, "limit_n": limit_n,
            "duration_sec": duration, "wave3_enabled": int(w3_on or 0),
            "wave3_mode": w3_mode or "", "started_at": started,
            "finished_at": finished,
        }
        meta["scope_cn"] = _SCOPE_CN.get(scope or "", scan_type or "扫描")
        if scope == "limit" and limit_n:
            meta["scope_cn"] = f"全市场·限量 {limit_n}"
        meta["wave3_cn"] = (f"开（{_WAVE3_MODE_CN.get(w3_mode or '', w3_mode or '')}）"
                            if meta["wave3_enabled"] else "关")
        parts = [meta["scope_cn"]]
        if universe_n:
            parts.append(f"扫描 {universe_n} 只")
        if duration is not None:
            parts.append(f"耗时 {round(float(duration), 1)}s")
        parts.append(f"三浪 {meta['wave3_cn']}")
        if started:
            parts.append(str(started)[:19])
        meta["label"] = " · ".join(parts)
        return meta
    except Exception:  # noqa: BLE001 元数据读取失败不得影响页面
        return {}
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
