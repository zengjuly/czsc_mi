"""mystery.services.wave3_scan — 三浪主升策略扫描编排（只读）。

职责边界（AGENTS.md §2.2 / §2.10）
----------------------------------
- **只读**：不写 ``scan_jobs`` / ``scan_results``，不参与 W25 扫描互斥锁，
  不触碰 ``analysis_cache``；与 ``czsc-mi scan`` 的写库职责完全隔离。
- **不评分**：只调用 ``mystery.core.wave3`` 的过滤/风控纯函数，不调用
  ``analyze_one_stock``、不产出综合评分，因此不构成第二套计分。
- **数据口径**：默认纯本地日 K（零网络、零写库），周线/月线由**本轮同一份
  日 K**经 ``MarketDataClient.resample_bars`` 派生（与 analyze/scoring 同口径，
  遵守「一轮分析只锁一种复权 qfq、不为规则另读一遍日 K」）。
  可选 ``offline=False`` 走统一多源出口 ``fetch_bars``（其在线命中回写 K 线
  缓存是既有数据层机制，非本策略新增写库）。
- 名称取自证券表，缺失显示「未知」（§2.9 有代码必有名称）。

输出为纯 dict（JSON 可序列化），由 CLI / 报表 / Web 展示层消费。
"""
from __future__ import annotations

import logging
import math
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..adapters.codes import db_code_of, normalize_symbol
from ..adapters.market import MarketDataClient
from ..core import wave3
from ..core.models import Bar, BarSeries

logger = logging.getLogger(__name__)

DEFAULT_EQUITY = 1_000_000.0
DEFAULT_RISK_PCT = 0.01


def _num(v: Any) -> Optional[float]:
    """DataFrame 单元格 → float（NaN/Inf/空 → None）。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f


def _daily_from_db(db: Any, symbol: str) -> Optional[BarSeries]:
    """纯本地库日 K → BarSeries（不触发任何在线降级或缓存回写）。"""
    code = db_code_of(symbol)
    df = db.load_kline(code, 'daily')
    if df is None or getattr(df, "empty", True):
        return None
    n = len(df)

    def col(name: str) -> List[Any]:
        return df[name].tolist() if name in df.columns else [None] * n

    dts = [str(x)[:10] for x in col('date')]
    opens, highs, lows = col('open'), col('high'), col('low')
    closes, volumes = col('close'), col('volume')
    amounts, pcts = col('amount'), col('pctChg')
    bars = [
        Bar(dt=dts[i], open=_num(opens[i]), high=_num(highs[i]),
            low=_num(lows[i]), close=_num(closes[i]),
            volume=_num(volumes[i]) or 0.0,
            amount=_num(amounts[i]) or 0.0,
            turnover=None, pct_chg=_num(pcts[i]) or 0.0)
        for i in range(n)
    ]
    return BarSeries(symbol=normalize_symbol(symbol), freq='1d',
                     adjust='qfq', bars=bars, source='db')


def _records(series: Optional[BarSeries]) -> List[Dict[str, Any]]:
    """BarSeries → core 层可消费的 dict 列表。"""
    if series is None or not series.bars:
        return []
    return [b.to_dict() for b in series.bars]


def _returns(records: List[Dict[str, Any]]) -> Dict[str, float]:
    """由日 K 收盘价算日收益（按日期对齐，供相关性矩阵使用）。"""
    out: Dict[str, float] = {}
    prev: Optional[float] = None
    for r in records:
        c = wave3._f(r.get('close'))
        d = str(r.get('dt') or '')
        if c is None or c <= 0 or not d:
            continue
        if prev is not None and prev > 0:
            out[d] = c / prev - 1.0
        prev = c
    return out


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning(f"[wave3] 环境变量 {name} 非数值，使用默认值 {default}")
        return default


def _resolve_symbols(db: Any, symbols: Optional[List[str]],
                     watchlist: bool, market: bool,
                     limit: Optional[int]) -> List[str]:
    if symbols:
        out = [normalize_symbol(str(s)) for s in symbols]
    elif watchlist or not market:
        from . import watchlist as _wl
        out = [normalize_symbol(str(s)) for s in _wl.load_watchlist()]
    else:
        out = []
        for row in db.get_stock_list() or []:
            code = row.get('code') if isinstance(row, dict) else (row[0] if row else None)
            if code:
                try:
                    out.append(normalize_symbol(str(code)))
                except Exception:
                    continue
    if limit and limit > 0:
        out = out[:limit]
    return out


def scan_wave3(symbols: Optional[List[str]] = None,
               watchlist: bool = True, market: bool = False,
               limit: Optional[int] = None,
               equity: Optional[float] = None,
               risk_pct: Optional[float] = None,
               cfg: Optional[Dict] = None, strict: bool = False,
               offline: bool = True, lot_size: int = 100,
               max_position_pct: Optional[float] = None,
               max_total_risk_pct: float = wave3.MAX_PORTFOLIO_RISK_PCT,
               high_corr: float = wave3.HIGH_CORR,
               min_bars: int = 60) -> Dict[str, Any]:
    """执行三浪主升策略扫描，返回组合层评估结果（纯 dict）。

    :param watchlist: 默认扫自选股；``market=True`` 显式扫全市场（配合 limit）。
    :param offline: 默认 True = 只读本地 K 线；False = 走统一多源出口。
    :param equity/risk_pct: 缺省取 env ``MYSTERY_WAVE3_EQUITY`` /
        ``MYSTERY_WAVE3_RISK_PCT``，再回退 1,000,000 / 1%。
    """
    eq = float(equity) if equity is not None else _env_float(
        'MYSTERY_WAVE3_EQUITY', DEFAULT_EQUITY)
    rp = float(risk_pct) if risk_pct is not None else _env_float(
        'MYSTERY_WAVE3_RISK_PCT', DEFAULT_RISK_PCT)
    client = MarketDataClient(cfg)
    db = client.db
    codes = _resolve_symbols(db, symbols, watchlist, market, limit)
    default_min = int(wave3.DEFAULTS["MIN_BARS"])
    params: Optional[Dict] = ({"MIN_BARS": min_bars}
                              if min_bars != default_min else None)

    items: List[Dict[str, Any]] = []
    returns_by_symbol: Dict[str, Any] = {}
    failed: List[Dict[str, str]] = []
    for sym in codes:
        try:
            daily = (_daily_from_db(db, sym) if offline
                     else client.fetch_bars(sym, '1d'))
            if daily is None or not daily.bars:
                failed.append({"symbol": sym, "reason": "无日K数据"})
                continue
            daily_rec = _records(daily)
            if len(daily_rec) < min_bars:
                failed.append({"symbol": sym,
                               "reason": f"日K仅{len(daily_rec)}根，不足{min_bars}根"})
                continue
            weekly = client.resample_bars(daily, '1w')
            monthly = client.resample_bars(daily, '1M')
            try:
                name = db.get_stock_name(sym) or "未知"
            except Exception:
                name = "未知"
            items.append({
                "symbol": sym, "name": name,
                "daily": daily_rec,
                "weekly": _records(weekly),
                "monthly": _records(monthly),
                "params": params,
            })
            returns_by_symbol[sym] = _returns(daily_rec)
        except Exception as e:  # 单票失败不中断整轮扫描
            logger.warning(f"[wave3] {sym} 取数失败: {str(e)[:100]}")
            failed.append({"symbol": sym, "reason": str(e)[:100]})

    result = wave3.evaluate_portfolio(
        items, equity=eq, risk_pct=rp,
        returns_by_symbol=returns_by_symbol, strict=strict,
        lot_size=lot_size, max_position_pct=max_position_pct,
        max_total_risk_pct=max_total_risk_pct, high_corr=high_corr)
    result["meta"] = {
        "source": "db" if offline else "multi",
        "offline": bool(offline),
        "generated_at": datetime.now().isoformat(timespec='seconds'),
        "universe": "symbols" if symbols else ("watchlist" if (watchlist or not market)
                                               else "market"),
        "requested": len(codes), "loaded": len(items), "failed": failed,
        "min_bars": min_bars,
    }
    return result
