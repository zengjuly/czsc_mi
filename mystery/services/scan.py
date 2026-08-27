"""mystery.services.scan — 全市场扫描（只调 analyze_one_stock(include_detail=False)）。

与个股页 / daily 同源同分（同一 Service）。单票失败不中断（大规模扫描容错）。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .analyze import AnalysisService

logger = logging.getLogger(__name__)


def scan_market(limit: Optional[int] = None,
                watchlist: Optional[List[str]] = None,
                include_detail: bool = False,
                cfg: Optional[Dict] = None,
                universe: Optional[List[str]] = None,
                min_score: Optional[float] = None) -> List[Dict[str, Any]]:
    """扫描市场，返回 AnalysisResult.to_dict() 列表（按分数降序）。

    :param watchlist: 指定代码列表（优先）
    :param universe: 自定义股票池（watchlist 为空时用）
    :param limit: 最多分析 N 只
    :param min_score: 只保留 >= 该分的股票
    """
    svc = AnalysisService(cfg)
    if watchlist:
        codes = list(watchlist)
    elif universe:
        codes = list(universe)
    else:
        codes = [s['code'] for s in svc.market.fetch_stock_list()]
    if limit:
        codes = codes[:limit]

    results: List[Dict[str, Any]] = []
    failed = 0
    for code in codes:
        try:
            r = svc.analyze_one_stock(code, include_detail=include_detail)
            d = r.to_dict()
            if min_score is None or (d.get('score') is not None
                                     and float(d['score']) >= min_score):
                results.append(d)
        except Exception as e:
            failed += 1
            logger.warning(f"[scan] {code} 分析失败跳过: {str(e)[:80]}")
    results.sort(key=lambda x: (x.get('score') is not None,
                                float(x.get('score') or -1)), reverse=True)
    logger.info(f"[scan] 完成 {len(results)} 只（失败 {failed} 只）")
    return results
