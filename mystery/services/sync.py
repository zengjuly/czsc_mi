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

from .analyze import AnalysisService

logger = logging.getLogger(__name__)

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
    # 逐股查 DuckDB 一次 ~0.14s × 5222 只 ≈ 12 分钟仍太慢：
    # 先一次性取每票 MAX(date)（DuckDB 内聚合，秒级），再只对"需要增量"
    # 的票做逐股查询——已最新的票 0 次查询。
    try:
        all_last = duck_conn.execute(
            "SELECT thscode, MAX(date) FROM v_daily_qfq GROUP BY thscode"
        ).fetchall()
        duck_last = {str(r[0]): str(r[1])[:10] for r in all_last}
    except Exception as e:
        logger.warning(f"[sync] DuckDB 全市场 MAX(date) 查询失败: {str(e)[:80]}")
        duck_last = {}

    sql = ("SELECT date, open, high, low, close, volume, turnover "
           "FROM v_daily_qfq WHERE thscode = ? AND date > ? ORDER BY date ASC")
    n_checked = 0
    for code in codes:
        if is_bj_stock(code):
            continue

        db_code = db_code_of(code)
        ths_code = normalize_symbol(code)

        # 检查 SQLite 是否已是最新（MAX(date)，不整表加载）
        try:
            cache_last = db.kline_last_date(db_code, 'daily')
            if cache_last and cache_last >= latest:
                continue  # SQLite 已是最新，跳过
        except Exception:
            cache_last = None

        # DuckDB 里没有该票或也不新鲜 → 跳过（后续 fetch_bars 走在线）
        d_last = duck_last.get(ths_code)
        if not d_last or d_last < latest:
            continue
        n_checked += 1

        # 从 DuckDB 读取增量行
        try:
            df = duck_conn.execute(sql, [ths_code, cache_last or '1900-01-01']).fetchdf()
            if df is None or df.empty:
                continue
            # 写入 SQLite（只写增量行）
            db.upsert_kline(df, db_code, 'daily')
            synced += 1
        except Exception as e:
            logger.debug(f"[sync] {code} DuckDB 读取失败: {str(e)[:60]}")

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

    synced, failed, updated_rows = 0, 0, 0
    skipped = 0
    errors: List[str] = []
    for code in codes:
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
