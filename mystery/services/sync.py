"""mystery.services.sync — 行情同步到本地库（迁自 sync_all_market.py）。

单票失败不中断。默认只同步自选/指定列表，全市场 --force 需用户明确要求。
DuckDB 优先：sync 开始时批量从 DuckDB 读取最新数据写入 SQLite，避免逐股走在线降级链。
W2-A：
- 多周期：``sync_market(periods=[...])``，周/月由日K重采样写入（不再打在线链）。
- 断点：``data/sync_checkpoint.json`` 记录 days/periods/done_symbols；
  参数变化丢弃旧断点，中断再跑跳过已完成。
- 证券列表为空必须报错退出（禁止打印"已完成"）。
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Dict, List, Optional

import pandas as pd

from .analyze import AnalysisService
from .task_lock import clear_progress, hold_task_lock, report_progress

logger = logging.getLogger(__name__)


# ---------------- W116 提速：可单测的纯判断函数 ----------------
def _pick_incremental_baseline(lasts, fallback_days=7):
    """从各票最新日挑 DuckDB 增量基准；忽略 None/空值。

    关键：若把 None（完全无数据票）当作 '1900-01-01' 参与 min()，会把
    WHERE date > 基准 拉成全表扫描（1041 万行 → 9 分钟 + OOM）。
    全是空值时返回 None，调用方应跳过增量。
    """
    from datetime import datetime as _dt, timedelta as _td
    valid = [str(v).strip()[:10] for v in lasts if v and str(v).strip()]
    if not valid:
        return None
    return min(valid)


def fresh_codes_by_latest(codes, last_map, db_code_of, is_bj,
                          latest, stale_days=3):
    """返回数据充足（最新日 >= latest - stale_days）的非北交所代码集合。

    last_map: {db_code: 'YYYY-MM-DD' 或 None}。轻微 stale（数据源缺最新日）
    也视为充足——在线重试只会空等超时，且会在下次 presync 自然补上。
    """
    from datetime import datetime as _dt, timedelta as _td
    if not latest:
        return set()
    thr = (_dt.strptime(latest, '%Y-%m-%d') - _td(days=stale_days)).strftime('%Y-%m-%d')
    out = set()
    for c in codes:
        if is_bj(c):
            continue
        last = last_map.get(db_code_of(c), '')
        if last and str(last)[:10] >= thr:
            out.add(c)
    return out


def fresh_sectors_by_latest(sec_codes, last_dates, latest, base_of=None):
    """返回已最新（库内最新日 >= latest）的板块代码集合。"""
    if not latest:
        return set()
    if base_of is None:
        base_of = lambda sc: sc if str(sc).startswith('ths_') else f'ths_{str(sc).split(".")[0]}'
    return {sc for sc in sec_codes
            if (last_dates.get(base_of(sc)) or '') >= latest}


_PERIOD_FREQ = {'daily': '1d', 'weekly': '1w', 'monthly': '1M'}

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DEFAULT_CHECKPOINT = os.environ.get(
    "MYSTERY_SYNC_CHECKPOINT",
    os.path.join(_REPO_ROOT, "data", "sync_checkpoint.json"),
)


# ---------------- 断点 ----------------
def _load_checkpoint() -> dict:
    try:
        if os.path.exists(_DEFAULT_CHECKPOINT):
            with open(_DEFAULT_CHECKPOINT, encoding="utf-8") as f:
                cp = json.load(f)
                if isinstance(cp, dict):
                    return cp
    except Exception as e:
        logger.warning(f"[sync] 断点读取失败，按新任务处理: {str(e)[:60]}")
    return {}


def _save_checkpoint(cp: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_DEFAULT_CHECKPOINT), exist_ok=True)
        with open(_DEFAULT_CHECKPOINT, "w", encoding="utf-8") as f:
            json.dump(cp, f, ensure_ascii=False, indent=1)
    except Exception as e:
        logger.warning(f"[sync] 断点写入失败: {str(e)[:60]}")


# ---------------- DuckDB 批量预同步（v1.22.x）----------------
def _batch_presync_from_duckdb(svc: AnalysisService, codes: List[str]) -> int:
    """批量从 DuckDB 读取最新数据写入 SQLite，避免逐股走在线降级链。

    返回成功写入的股票数。DuckDB 不存在或无数据时返回 0（静默，后续走逐股 sync）。
    """
    from ..adapters.codes import db_code_of, is_bj_stock, normalize_symbol
    from ..adapters.calendar import get_latest_trade_date

    db = svc.market.db
    ths = svc.market.ths

    # DuckDB 路径检查
    if not ths.marketdb_path or not os.path.exists(ths.marketdb_path):
        logger.debug("[sync] DuckDB 不存在，跳过批量预同步")
        return 0

    # 获取最新交易日作为新鲜度阈值
    try:
        latest = get_latest_trade_date()
        if not latest:
            logger.debug("[sync] 无法获取最新交易日，跳过批量预同步")
            return 0
    except Exception as e:
        logger.debug(f"[sync] 获取最新交易日失败: {str(e)[:60]}")
        return 0

    logger.info(f"[sync] 批量预同步：DuckDB → SQLite（阈值 {latest}）")
    t_presync = time.time()

    synced = 0
    duck_conn = None
    try:
        import duckdb
        duck_conn = duckdb.connect(ths.marketdb_path, read_only=True)
    except Exception as e:
        logger.debug(f"[sync] DuckDB 连接失败: {str(e)[:60]}")
        return 0

    # W12 增量化：只取 date > cache_last 的增量行。旧实现全历史拉取+
    # 全行 upsert，5200 只逐股写 4.2GB SQLite，是 18:00 管线 50+ 分钟
    # 黑盒耗时的主因。
    # W12b：逐股查 DuckDB 49ms×5200 ≈ 4 分钟也是大头，改为一次全市场
    # 增量查询（WHERE date > 全局基准）+ pandas 分组写入；SQLite 写入
    # 从每票一事务改为 upsert_kline_many 单大事务 executemany。
    # W116: 原逐票 db.kline_last_date()（5582 次单查询，约 2-3 分钟）改为
    # 一次 GROUP BY 全量取每票最新日，Python 侧查字典，语义等价、耗时降到毫秒级。
    n_checked = 0
    per_code_last: Dict[str, Optional[str]] = {}
    try:
        _conn = db._connect()
        try:
            _rows = _conn.execute(
                "SELECT code, MAX(date) FROM stock_kline_data "
                "WHERE period='daily' GROUP BY code").fetchall()
        finally:
            _conn.close()
        _cache_last_map = {str(r[0]): (str(r[1]) if r[1] else None) for r in _rows}
    except Exception as e:
        logger.debug(f"[sync] 批量查最新日失败，回退逐票: {str(e)[:60]}")
        _cache_last_map = None
    for code in codes:
        if is_bj_stock(code):
            continue
        db_code = db_code_of(code)
        ths_code = normalize_symbol(code)
        if _cache_last_map is not None:
            cache_last = _cache_last_map.get(db_code)
        else:
            try:
                cache_last = db.kline_last_date(db_code, 'daily')
            except Exception:
                cache_last = None
        if cache_last and cache_last >= latest:
            continue  # SQLite 已是最新，跳过
        per_code_last[ths_code] = cache_last
    n_checked = len(per_code_last)

    synced = 0
    if per_code_last:
        # W116: 忽略 None（无数据票），否则 min_last 被拉到 1900 →
        # WHERE date>'1900' 拉出 1041 万行全表扫描，是 9 分钟+OOM 真凶。
        _valid_count = sum(1 for v in per_code_last.values() if v)
        min_last = _pick_incremental_baseline(per_code_last.values())
        if not min_last:
            # 无任何有数据票：无需增量，直接返回（避免全表扫描）
            duck_conn.close()
            logger.info("[sync] 无可增量基准票，跳过批量预同步")
            return 0
        logger.info(f"[sync] 增量基准 min_last={min_last} "
                    f"(有效票 {_valid_count}/{len(per_code_last)})")
        try:
            duck_df = duck_conn.execute(
                "SELECT thscode, date, open, high, low, close, volume, turnover "
                "FROM v_daily_qfq WHERE date > ? ORDER BY thscode, date ASC",
                [min_last]).fetchdf()
        except Exception as e:
            logger.warning(f"[sync] DuckDB 全市场增量查询失败: {str(e)[:80]}")
            duck_df = None

        if duck_df is not None and not duck_df.empty:
            # 按每票 cache_last 过滤出各自增量（min_last 之上部分票只差
            # 1 天、部分差更多——各取各的），且只保留本次同步清单内的票
            parts = []
            for ths_code, g in duck_df.groupby('thscode'):
                cache_last = per_code_last.get(str(ths_code))
                if cache_last is None:
                    continue  # 不在同步清单（DuckDB 多出的票）
                parts.append(g[g['date'] > (cache_last or '1900-01-01')])
            inc = pd.concat(parts, ignore_index=True) if parts \
                else duck_df.iloc[0:0]
            if not inc.empty:
                # 单大事务批量写入（upsert_kline_many 内部 executemany）
                db.upsert_kline_many(inc, 'daily', code_col='thscode')
                synced = int(inc['thscode'].nunique())

    try:
        duck_conn.close()
    except Exception:
        pass

    logger.info(f"[sync] 批量预同步完成：{synced} 只写入（检查 {n_checked} 只非最新，"
                f"总 {len(codes)} 只），耗时 {time.time() - t_presync:.1f}s")
    return synced


def sync_market(period: Optional[str] = None,
                days: int = 365,
                force: bool = False,
                symbols: Optional[List[str]] = None,
                limit: Optional[int] = None,
                cfg: Optional[Dict] = None,
                periods: Optional[List[str]] = None,
                no_persist: bool = False) -> dict:
    """同步行情到本地库（W49：全程持 sync 任务锁 + 进度心跳）。

    同名任务并发直接拒绝（不排队）；--force 旧语义（强制全量重灌）保留，
    但 W49 起 force 不再绕锁——绕锁重灌正是 09-18 WAL 换建僵死事故的形态。
    """
    db_path = AnalysisService(cfg).market.db.db_path
    _label_periods = periods or [period or 'daily']
    label = (f"days={days} periods={'/'.join(_label_periods)}"
             + (f" limit={limit}" if limit else ""))
    with hold_task_lock(db_path, 'sync', label=label):
        try:
            return _sync_market_impl(db_path, period=period, days=days, force=force,
                                     symbols=symbols, limit=limit, cfg=cfg,
                                     periods=periods, no_persist=no_persist)
        finally:
            clear_progress(db_path, 'sync')


def _sync_market_impl(db_path: str,
                period: Optional[str] = None,
                days: int = 365,
                force: bool = False,
                symbols: Optional[List[str]] = None,
                limit: Optional[int] = None,
                cfg: Optional[Dict] = None,
                periods: Optional[List[str]] = None,
                no_persist: bool = False) -> dict:
    """同步行情到本地库。

    :param period: 单周期（兼容旧签名）；与 periods 同时给出时取 periods
    :param periods: 多周期列表，如 ['daily', 'weekly']（周月由日K重采样）
    :param symbols: 指定代码（缺省用证券列表）
    :param limit: 最多同步 N 只（缺省全列表）
    :param force: 强制全量（勿轻易使用，CLAUDE.md §12）
    :param no_persist: True 不写断点
    """
    if periods is None:
        periods = [period] if period else ['daily']
    periods = [p for p in periods if p in _PERIOD_FREQ]
    if not periods:
        periods = ['daily']

    svc = AnalysisService(cfg)
    if symbols:
        codes = list(symbols)
    else:
        codes = [s['code'] for s in svc.market.fetch_stock_list()]
    if not codes:
        raise RuntimeError("证券列表为空，拒绝同步（检查行情源/本地库证券表）")
    if limit:
        codes = codes[:limit]

    # 断点：参数变化（days/periods）或 force → 丢弃旧断点
    cp = {} if no_persist else _load_checkpoint()
    params_ok = (cp.get("days") == days
                 and sorted(cp.get("periods", [])) == sorted(periods))
    if force or not params_ok:
        cp = {"days": days, "periods": sorted(periods), "done_symbols": {}}
    done = cp.get("done_symbols", {})

    # ---------- DuckDB 批量预同步（v1.22.x）----------
    # 在逐股 sync 之前，先从 DuckDB 批量读取最新数据写入 SQLite。
    # 这样后续的 fetch_bars() 会直接命中 SQLite，避免逐股走在线降级链。
    if not force:
        _batch_presync_from_duckdb(svc, codes)

    # W116 新鲜度快速通道：presync 已把 DuckDB 增量灌进 SQLite，若绝大多数
    # 票已是最新交易日，逐股「读出又写回」纯属重复劳动（日报 9 分钟 + OOM
    # 根源）。这里一次性统计各票最新日，fresh 票直接跳过，仅 stale 票走在线。
    # 开关 SYNC_FRESH_SKIP=0 关闭；阈值 SYNC_FRESH_MIN_RATIO 默认 0.9。
    _fresh_skip = os.environ.get('SYNC_FRESH_SKIP', '1') != '0'
    _min_ratio = float(os.environ.get('SYNC_FRESH_MIN_RATIO', '0.9'))
    _stale_days = int(os.environ.get('SYNC_FRESH_STALE_DAYS', '3'))
    _fresh_codes = 0
    if _fresh_skip and not force and 'daily' in periods:
        try:
            from ..adapters.codes import db_code_of as _db_code_of, is_bj_stock as _is_bj
            from ..adapters.calendar import get_latest_trade_date as _gltd
            _latest = _gltd()
            if _latest:
                _conn = svc.market.db._connect()
                try:
                    # 一次聚合查询替代逐票 MAX：GROUP BY 拿每票最新日
                    _rows = _conn.execute(
                        "SELECT code, MAX(date) FROM stock_kline_data "
                        "WHERE period='daily' GROUP BY code").fetchall()
                finally:
                    _conn.close()
                _last_map = {str(r[0]): str(r[1] or '') for r in _rows}
                _non_bj = [c for c in codes if not _is_bj(c)]
                _fresh_set = fresh_codes_by_latest(
                    _non_bj, _last_map, _db_code_of, _is_bj,
                    _latest, stale_days=_stale_days)
                _fresh_codes = len(_fresh_set)
                _ratio = (_fresh_codes / len(_non_bj)) if _non_bj else 0.0
                if _ratio >= _min_ratio:
                    # 只保留严重落后/无数据票 + 北交所（北交所本就跳过）
                    codes = [c for c in codes if c not in _fresh_set]
                    logger.info(
                        f"[sync] 新鲜度快速通道：{_fresh_codes}/{len(_non_bj)} "
                        f"票数据充足(>={_latest} 前{_stale_days}天, {_ratio:.0%})，跳过逐股；"
                        f"仅同步剩余 {len(codes)} 只")
                else:
                    logger.info(
                        f"[sync] 新鲜度快速通道未启用：充足 {_ratio:.0%} < "
                        f"{_min_ratio:.0%}，回退全量逐股同步")
                    _fresh_codes = 0
        except Exception as e:
            logger.debug(f"[sync] 新鲜度快速通道检查失败，回退全量: {str(e)[:80]}")
            _fresh_codes = 0

    synced = _fresh_codes  # fresh 票已由 presync 落库，计入成功
    failed, updated_rows = 0, 0
    skipped = 0
    errors: List[str] = []
    # W49 进度心跳：total=票数×周期数；每票处理完上报一次（内部节流）
    total_steps = len(codes) * len(periods)
    done_steps = 0
    report_progress(db_path, 'sync', 0, total_steps)
    for ci, code in enumerate(codes):
        # 循环顶部上报：continue/跳过分支不漏报（票内多周期一并计入）
        done_steps = ci * len(periods) + len(periods)
        report_progress(db_path, 'sync', done_steps, total_steps, label=code)
        # 跳过北交所(920xxx.BJ)
        from ..adapters.codes import is_bj_stock
        if is_bj_stock(code):
            logger.debug(f"[sync] 跳过北交所: {code}")
            continue
        # W8-fix2: 统一 db_code 归一（600010.SH → sh.600010）。旧实现
        # "code if '.' in code" 把内部格式原样写库，与 db_code_of() 读格式
        # 不一致 → 每次写入新行、读取永远看到旧行，数据永远"过期"。
        from ..adapters.codes import db_code_of
        db_code = db_code_of(code)
        for p in periods:
            key = f"{db_code}::{p}"
            if not force and done.get(key):
                skipped += 1
                continue
            try:
                freq = _PERIOD_FREQ[p]
                s = svc.market.fetch_bars(code, freq)
                if not s.bars:
                    logger.warning(f"[sync] {code} 无数据（{p}），跳过")
                    failed += 1
                    continue
                df = svc.market.to_df(s)
                svc.market.db.upsert_kline(df, db_code, p)
                synced += 1
                updated_rows += len(df)
                if not no_persist:
                    done[key] = True
            except Exception as e:
                failed += 1
                errors.append(f"{code}({p}): {str(e)[:60]}")
                logger.warning(f"[sync] {code}({p}) 失败: {str(e)[:80]}")
    if not no_persist:
        cp["done_symbols"] = done
        _save_checkpoint(cp)
    clear_progress(db_path, 'sync')
    return {'periods': periods, 'days': days, 'synced': synced, 'failed': failed,
            'skipped': skipped, 'rows': updated_rows, 'errors': errors[:10]}


def sync_sector_constituents(sector_code: str,
                             cfg: Optional[Dict] = None,
                             limit: Optional[int] = None) -> dict:
    """同步单个板块的成分股 → stock_sector_rel（is_primary=0，不覆盖主行业）。

    空结果（网络失败/板块无成分）不写库、返回 error，禁止静默"成功"。
    """
    from ..adapters.ths import ThsClient
    from ..store.db import MysteryDB

    ths = ThsClient(cfg)
    db = MysteryDB(cfg.get('db_path') if cfg else None)
    cons = ths.fetch_constituents(sector_code)
    if limit:
        cons = cons[:limit]
    if not cons:
        return {'sector_code': sector_code, 'total': 0, 'written': 0,
                'error': '成分拉取为空（网络失败或该板块无成分股）'}
    written = 0
    for c in cons:
        try:
            db.upsert_stock_sector_rel(c['thscode'], sector_code, is_primary=0)
            written += 1
        except Exception as e:
            logger.warning(f"[sync] rel 写入失败 {c.get('thscode')}: {str(e)[:60]}")
    # 刷新板块元数据活跃时间（成分已落库即视为活跃；不覆盖已有板块名）
    try:
        db.ensure_sector_meta(sector_code)
    except Exception as e:
        logger.debug(f"[sync] meta 刷新跳过: {str(e)[:60]}")
    return {'sector_code': sector_code, 'total': len(cons), 'written': written}


def sync_sector_kline(cfg: Optional[Dict] = None,
                      days: int = 15,
                      sectors: Optional[List[str]] = None,
                      limit: Optional[int] = None,
                      full_since: Optional[str] = None) -> dict:
    """增量同步板块指数日K → sector_kline（W39，三振行业腿数据源）。

    W49：持 sector_kline 任务锁 + 进度心跳，同名任务并发拒绝。

    取数归一：只走 ThsClient.get_index_daily（fuyao index-historical），
    与 fetch_index 降级链同源，禁止旁路 HTTP。增量：每板块从库内最新日期
    前扩 days 天重拉（幂等 UPSERT 覆盖修订），无库底的板块全量拉 days 窗口。
    full_since（如 '2023-01-01'）：忽略库内断点，从该日起全量重灌——
    W39 date_ms 错位修复重建专用，仍是同一取数/写库通道。
    """
    from datetime import datetime, timedelta

    from ..adapters.ths import ThsClient
    from ..store.db import MysteryDB

    cfg = cfg or {}
    db = MysteryDB(cfg.get('db_path') if cfg else None)
    label = ('全量重建 since=' + full_since) if full_since \
        else f"增量 days={days}" + (f" limit={limit}" if limit else "")
    with hold_task_lock(db.db_path, 'sector_kline', label=label):
        try:
            return _sync_sector_kline_impl(
                cfg=cfg, days=days, sectors=sectors, limit=limit,
                full_since=full_since, db_path=db.db_path)
        finally:
            clear_progress(db.db_path, 'sector_kline')


def _sync_sector_kline_impl(cfg: Optional[Dict] = None,
                            days: int = 15,
                            sectors: Optional[List[str]] = None,
                            limit: Optional[int] = None,
                            full_since: Optional[str] = None,
                            db_path: str = "") -> dict:
    from datetime import datetime, timedelta

    from ..adapters.ths import ThsClient
    from ..store.db import MysteryDB

    ths = ThsClient(cfg)
    db = MysteryDB(cfg.get('db_path') if cfg else None)

    if sectors:
        codes = list(sectors)
    else:
        codes = db.get_sector_kline_sectors()
    if not codes:
        raise RuntimeError("sector_kline 板块清单为空（先填充 sector_meta/存量）")
    if limit:
        codes = codes[:limit]

    last_dates = db.get_sector_kline_dates()
    names = db.get_sector_kline_names()
    if full_since:
        # W39 date_ms 错位重建：清单/名称已在上面取好，先清空再重灌，
        # 否则旧错位的周末键行会作为孤儿脏数据残留。
        db.clear_sector_kline()
        last_dates = {}
    today = datetime.now()

    # W116 板块新鲜度快速通道：已最新到交易日的板块无需每天重拉 15 天。
    # 实测 687/688 板块已最新，逐个 fuyao 在线重拉是日报 5 分钟瓶颈。
    # 开关 SECTOR_FRESH_SKIP=0 关闭；阈值 SECTOR_FRESH_MIN_RATIO 默认 0.9。
    # full_since 重建不走快速通道，保证全量覆盖。
    _sec_fresh_skip = (os.environ.get('SECTOR_FRESH_SKIP', '1') != '0'
                       and not full_since)
    if _sec_fresh_skip and codes:
        try:
            from ..adapters.calendar import get_latest_trade_date as _gltd
            _latest = _gltd()
            if _latest:
                _fresh = fresh_sectors_by_latest(codes, last_dates, _latest)
                _ratio = len(_fresh) / len(codes)
                _min = float(os.environ.get('SECTOR_FRESH_MIN_RATIO', '0.9'))
                if _ratio >= _min:
                    _skip_n = len(_fresh)
                    codes = [sc for sc in codes if sc not in _fresh]
                    logger.info(
                        f"[sync-sector-kline] 新鲜度快速通道：{_skip_n}/{_skip_n + len(codes)} "
                        f"板块已最新(≥{_latest}, {_ratio:.0%})，跳过；"
                        f"仅补 {len(codes)} 只 stale 板块")
                else:
                    logger.info(
                        f"[sync-sector-kline] 快速通道未启用：fresh {_ratio:.0%} < "
                        f"{_min:.0%}，回退全量")
        except Exception as e:
            logger.debug(f"[sync-sector-kline] 新鲜度检查失败，回退全量: {str(e)[:80]}")
    if not codes:
        # 全部板块已最新：跳过在线拉取，返回成功（rows=0）
        return {'sectors': 0, 'skipped_fresh': True,
                'synced': 0, 'failed': 0, 'rows': 0,
                'max_date': today.strftime('%Y-%m-%d'),
                'note': '所有板块已最新，跳过在线拉取'}
    synced, failed, rows_total = 0, 0, 0
    errors: List[str] = []
    total_steps = len(codes)
    report_progress(db_path, 'sector_kline', 0, total_steps)

    # W115: 串行 688 板块 × fuyao 在线 1-3s = 15-20 分钟，日报瓶颈。
    # 改为 ThreadPoolExecutor 并发（默认 8 workers，可用 SECTOR_SYNC_WORKERS 覆盖）。
    # ThsClient 每次 get_index_daily 起独立 fuyao 子进程，彼此无共享状态，线程安全。
    import os as _os
    from concurrent.futures import ThreadPoolExecutor, as_completed
    workers = max(1, min(32, int(_os.environ.get('SECTOR_SYNC_WORKERS', '8'))))

    def _one(sc):
        base = sc if sc.startswith('ths_') else f'ths_{sc.split(".")[0]}'
        fuyao_code = base[4:] + '.TI'
        anchor = last_dates.get(base)
        if full_since:
            start = full_since
        elif anchor:
            try:
                start = (datetime.strptime(anchor, '%Y-%m-%d')
                         - timedelta(days=days)).strftime('%Y-%m-%d')
            except ValueError:
                start = (today - timedelta(days=days)).strftime('%Y-%m-%d')
        else:
            start = (today - timedelta(days=days)).strftime('%Y-%m-%d')
        try:
            df = ths.get_index_daily(fuyao_code, start=start,
                                     end=today.strftime('%Y-%m-%d'))
            if df is None or df.empty:
                return ('empty', base, 0, '')
            n = db.upsert_sector_kline(df, base, sector_name=names.get(base, ''))
            return ('ok', base, n, '')
        except Exception as e:
            return ('err', base, 0, str(e)[:60])

    done_n = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_one, sc): sc for sc in codes}
        for fut in as_completed(futs):
            status, base, n, err = fut.result()
            done_n += 1
            if done_n % 20 == 0 or done_n == total_steps:
                report_progress(db_path, 'sector_kline', done_n, total_steps)
            if status == 'ok':
                rows_total += n
                synced += 1
            elif status == 'empty':
                failed += 1
                errors.append(f"{base}: 空返回")
            else:
                failed += 1
                errors.append(f"{base}: {err}")
                logger.warning(f"[sync-sector-kline] {base} 失败: {err}")
    return {'sectors': len(codes), 'synced': synced, 'failed': failed,
            'rows': rows_total, 'max_date': today.strftime('%Y-%m-%d'),
            'errors': errors[:10]}
