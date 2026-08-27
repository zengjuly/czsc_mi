"""mystery.services.sync — 行情同步到本地库（迁自 sync_all_market.py）。

单票失败不中断。默认只同步自选/指定列表，全市场 --force 需用户明确要求。
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from .analyze import AnalysisService

logger = logging.getLogger(__name__)

_PERIOD_FREQ = {'daily': '1d', 'weekly': '1w', 'monthly': '1M'}


def sync_market(period: str = 'daily', days: int = 365, force: bool = False,
                symbols: Optional[List[str]] = None,
                limit: Optional[int] = None,
                cfg: Optional[Dict] = None) -> dict:
    """同步行情到本地库。

    :param period: daily / weekly / monthly（周月由日K重采样）
    :param symbols: 指定代码（缺省用证券列表）
    :param limit: 最多同步 N 只（缺省全列表）
    :param force: 强制全量（勿轻易使用，CLAUDE.md §12）
    """
    freq = _PERIOD_FREQ.get(period, '1d')
    svc = AnalysisService(cfg)
    if symbols:
        codes = list(symbols)
    else:
        codes = [s['code'] for s in svc.market.fetch_stock_list()]
    if limit:
        codes = codes[:limit]

    synced, failed, updated_rows = 0, 0, 0
    errors: List[str] = []
    for code in codes:
        try:
            s = svc.market.fetch_bars(code, freq)
            if not s.bars:
                logger.warning(f"[sync] {code} 无数据，跳过")
                failed += 1
                continue
            df = svc.market.to_df(s)
            db_code = code if '.' in code else code[:2] + '.' + code[2:]
            svc.market.db.upsert_kline(df, db_code, period)
            synced += 1
            updated_rows += len(df)
        except Exception as e:
            failed += 1
            errors.append(f"{code}: {str(e)[:60]}")
            logger.warning(f"[sync] {code} 失败: {str(e)[:80]}")
    return {'period': period, 'synced': synced, 'failed': failed,
            'rows': updated_rows, 'errors': errors[:10]}
