"""mystery.services.sync_shares — 流通股本低频快照（W22，006.md 阶段 2.4）。

股本只在送转/增发/回购注销/解禁等事件日变化，不需要每日拉取：
- 空库或 --force：初始化拉取（auction-snapshot stage=final，100 只/批）
- 每周对账（单独 cron，不进 18:00 主链）
- 跳变校验：新推算股本相对旧值变化 >3% 才覆盖（滤掉纯价格波动）
- 生产用 final，不用盘中 live；auction_turnover_pct 不入库 turn

W26c（007.md）：`--from-adjustments` 除权日触发重拉——从本地 MarketDB
`raw_adjustment_events` 筛 since 以来的送转/增发事件，只对命中票批拉竞价；
无事件 = 零 HTTP 秒回。事件票即使股本变化 ≤3% 也写新 as_of（新锚点），
日志标 unchanged。周对账仍用 3% 门槛。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from ..adapters.ths import ThsClient
from ..core.turnover import estimate_float_shares, shares_changed_enough
from ..store.db import MysteryDB

logger = logging.getLogger(__name__)


def _to_thscode(code: str) -> str:
    """sh.600519 / sh600519 → 600519.SH。"""
    s = str(code).replace('.', '')
    if s[:2].lower() in ('sh', 'sz', 'bj'):
        return f"{s[2:]}.{s[:2].upper()}"
    return str(code).upper()


def _sync_state_path(db_path: str) -> str:
    """股本同步状态文件（与 db 同目录，模式同 <db>.scanlock）。"""
    return db_path + '.shares_state'


def _last_shares_sync(db_path: str) -> Optional[str]:
    try:
        with open(_sync_state_path(db_path), encoding='utf-8') as f:
            return (json.load(f) or {}).get('last_sync') or None
    except Exception:  # noqa: BLE001 状态缺失 = 从未成功对账
        return None


def _record_shares_sync(db_path: str, as_of: str) -> None:
    try:
        with open(_sync_state_path(db_path), 'w', encoding='utf-8') as f:
            json.dump({'last_sync': as_of}, f)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"股本对账状态写入失败: {e}")


def sync_shares(codes: Optional[List[str]] = None,
                force: bool = False,
                as_of: Optional[str] = None,
                db: Optional[MysteryDB] = None,
                event_codes: Optional[set] = None) -> Dict:
    """刷新流通股本快照。

    codes: 目标票（默认全市场证券列表）。force=True 全量重拉并写快照；
    否则只对有旧快照的票做跳变对账 + 无快照的票补齐。
    event_codes: W26c 除权事件票（thscode 集合）——命中的票跳过 3% 门槛，
    股本未变也写新 as_of（锚点日），日志 unchanged 计数。
    返回 QA 摘要 {as_of, fetched, written, skipped_small_change, failed}。
    """
    db = db or MysteryDB()
    ths = ThsClient()
    as_of = as_of or datetime.now().strftime('%Y-%m-%d')

    if codes is None:
        codes = [x['code'] for x in db.get_stock_list(stock_only=True)]
    codes = [c for c in codes if c]
    if not codes:
        return {'as_of': as_of, 'fetched': 0, 'written': 0,
                'skipped_small_change': 0, 'failed': 0,
                'note': '证券列表为空，跳过（先跑 sync 或检查缓存）'}

    thscodes = [_to_thscode(c) for c in codes]
    code_by_ths = {_to_thscode(c): c for c in codes}

    snaps = ths.get_auction_snapshot(thscodes, stage='final')
    written = small = failed = unchanged = 0
    for it in snaps:
        try:
            thscode = it.get('thscode', '')
            shares = estimate_float_shares(it.get('float_market_cap'),
                                           it.get('last_price'))
            if shares is None:
                failed += 1
                continue
            code = code_by_ths.get(thscode, thscode)
            old = db.get_float_share(thscode)
            is_event = bool(event_codes and thscode in event_codes)
            if (not force and not is_event and old
                    and old.get('float_shares')):
                if not shares_changed_enough(old['float_shares'], shares):
                    small += 1      # 变化 ≤3%：纯价格波动，不覆盖
                    continue
            if is_event and old and old.get('float_shares') \
                    and not shares_changed_enough(old['float_shares'], shares):
                unchanged += 1      # 事件日股本未变：仍写新锚点 as_of
            db.upsert_float_share(thscode, as_of,
                                  it.get('float_market_cap'),
                                  it.get('last_price'), shares)
            written += 1
        except Exception as e:  # noqa: BLE001 单票失败不阻断整批
            logger.debug(f"股本快照写入失败 {it.get('thscode')}: {str(e)[:60]}")
            failed += 1
    out = {'as_of': as_of, 'fetched': len(snaps), 'written': written,
           'skipped_small_change': small, 'unchanged_event': unchanged,
           'failed': failed, 'targets': len(codes)}
    if written:
        _record_shares_sync(db.db_path, as_of)
    logger.info(f"[sync_shares] {out}")
    return out


def find_adjustment_events(marketdb_path: str, since: str) -> Optional[List[str]]:
    """从本地 MarketDB raw_adjustment_events 筛 [since, ∞) 股本类事件票。

    事件口径（可用字段内）：送转 per_share_bonus>0、增发 allotment_ratio>0。
    表/文件不存在 → None（调用方 warn 跳过，周对账兜底）；有表无事件 → []。
    """
    if not marketdb_path or not os.path.exists(marketdb_path):
        return None
    try:
        import duckdb
        con = duckdb.connect(marketdb_path, read_only=True)
        try:
            n = con.execute(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_name='raw_adjustment_events'").fetchone()[0]
            if not n:
                return None
            rows = con.execute(
                "SELECT DISTINCT thscode FROM raw_adjustment_events "
                "WHERE ex_date >= ? AND (per_share_bonus > 0 "
                "OR allotment_ratio > 0) ORDER BY thscode",
                (since,)).fetchall()
            return [r[0] for r in rows]
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001 表损坏/字段不符 = 跳过
        logger.warning(f"[from_adjustments] 事件表读取失败: {str(e)[:80]}")
        return None


def sync_shares_from_adjustments(since: Optional[str] = None,
                                 db: Optional[MysteryDB] = None,
                                 ths: Optional[ThsClient] = None,
                                 as_of: Optional[str] = None) -> Dict:
    """W26c：除权事件触发的股本刷新。无事件 = 零 HTTP，立即返回。

    since 默认：上次成功对账日（状态文件），否则近 7 自然日。
    """
    db = db or MysteryDB()
    ths = ths or ThsClient()
    as_of = as_of or datetime.now().strftime('%Y-%m-%d')
    if not since:
        since = _last_shares_sync(db.db_path)
    if not since:
        since = (datetime.strptime(as_of, '%Y-%m-%d')
                 - timedelta(days=7)).strftime('%Y-%m-%d')

    events = find_adjustment_events(ths.marketdb_path, since)
    if events is None:
        return {'as_of': as_of, 'since': since, 'events': 0,
                'requested': 0, 'note': '调整事件表缺失/不可读，跳过'
                                        '（周对账兜底）'}
    if not events:
        return {'as_of': as_of, 'since': since, 'events': 0,
                'requested': 0, 'fetched': 0, 'written': 0,
                'note': '无股本类除权事件，零 HTTP'}

    # 事件 thscode → 库内代码（能映射则映射，保持 watchlist/代码风格一致）
    known = {x['code']: _to_thscode(x['code'])
             for x in db.get_stock_list(stock_only=True)}
    ev_set = set(events)
    codes = [c for c, t in known.items() if t in ev_set]
    extra = sorted(ev_set - set(known.values()))   # 库里没有的证券：直接按 thscode 打
    targets = codes + [_thscode_to_prefixed(t) for t in extra]
    out = sync_shares(codes=targets, db=db, as_of=as_of, event_codes=ev_set)
    out.update({'since': since, 'events': len(ev_set),
                'requested': len(targets)})
    logger.info(f"[from_adjustments] since={since} events={len(ev_set)} "
                f"requested={len(targets)}")
    return out


def _thscode_to_prefixed(thscode: str) -> str:
    """600519.SH → sh600519（_to_thscode 的逆，供统一走竞价批拉）。"""
    s = str(thscode)
    if '.' in s:
        digits, exch = s.rsplit('.', 1)
        if exch.lower() in ('sh', 'sz', 'bj'):
            return f"{exch.lower()}{digits}"
    return s
