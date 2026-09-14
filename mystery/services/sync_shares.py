"""mystery.services.sync_shares — 流通股本低频快照（W22，006.md 阶段 2.4）。

股本只在送转/增发/回购注销/解禁等事件日变化，不需要每日拉取：
- 空库或 --force：初始化拉取（auction-snapshot stage=final，100 只/批）
- 每周对账（单独 cron，不进 18:00 主链）
- 跳变校验：新推算股本相对旧值变化 >3% 才覆盖（滤掉纯价格波动）
- 生产用 final，不用盘中 live；auction_turnover_pct 不入库 turn
"""
from __future__ import annotations

import logging
from datetime import datetime
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


def sync_shares(codes: Optional[List[str]] = None,
                force: bool = False,
                as_of: Optional[str] = None,
                db: Optional[MysteryDB] = None) -> Dict:
    """刷新流通股本快照。

    codes: 目标票（默认全市场证券列表）。force=True 全量重拉并写快照；
    否则只对有旧快照的票做跳变对账 + 无快照的票补齐。
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
    written = small = failed = 0
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
            if not force and old and old.get('float_shares'):
                if not shares_changed_enough(old['float_shares'], shares):
                    small += 1      # 变化 ≤3%：纯价格波动，不覆盖
                    continue
            db.upsert_float_share(thscode, as_of,
                                  it.get('float_market_cap'),
                                  it.get('last_price'), shares)
            written += 1
        except Exception as e:  # noqa: BLE001 单票失败不阻断整批
            logger.debug(f"股本快照写入失败 {it.get('thscode')}: {str(e)[:60]}")
            failed += 1
    out = {'as_of': as_of, 'fetched': len(snaps), 'written': written,
           'skipped_small_change': small, 'failed': failed,
           'targets': len(codes)}
    logger.info(f"[sync_shares] {out}")
    return out
