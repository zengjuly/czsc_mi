"""mystery.services.sync — 行情同步到本地库（迁自 sync_all_market.py）。

单票失败不中断。默认只同步自选/指定列表，全市场 --force 需用户明确要求。
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

    synced, failed, updated_rows = 0, 0, 0
    skipped = 0
    errors: List[str] = []
    for code in codes:
        db_code = code if '.' in code else code[:2] + '.' + code[2:]
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
