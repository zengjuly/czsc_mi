"""mystery.services.watchlist — 自选股管理 + 股票名搜索。

自选股存 data/watchlist.json（gitignore，不入库）；个股页/daily/扫描共用。
"""
from __future__ import annotations

import json
import logging
import os
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DEFAULT_WATCHLIST = os.environ.get(
    "MYSTERY_WATCHLIST",
    os.path.join(_REPO_ROOT, "data", "watchlist.json"),
)
_DEFAULT_STOCKS = ['sh600519', 'sz000001', 'sh600150', 'sh600036',
                   'sz000858', 'sz300750']


def _load_raw() -> List[str]:
    try:
        if os.path.exists(_DEFAULT_WATCHLIST):
            with open(_DEFAULT_WATCHLIST, encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, list):
                    return [str(x) for x in data]
    except Exception as e:
        logger.warning(f"自选股读取失败: {e}")
    return list(_DEFAULT_STOCKS)


def load_watchlist() -> List[str]:
    """自选股代码列表（sh600519 格式）。"""
    return _load_raw()


def save_watchlist(codes: List[str]) -> None:
    os.makedirs(os.path.dirname(_DEFAULT_WATCHLIST), exist_ok=True)
    with open(_DEFAULT_WATCHLIST, 'w', encoding='utf-8') as f:
        json.dump(list(dict.fromkeys(codes)), f, ensure_ascii=False, indent=1)


def add_to_watchlist(code: str) -> List[str]:
    codes = _load_raw()
    if code not in codes:
        codes.append(code)
        save_watchlist(codes)
    return codes


def remove_from_watchlist(code: str) -> List[str]:
    codes = [c for c in _load_raw() if c != code]
    save_watchlist(codes)
    return codes


def search_stock(keyword: str, limit: int = 10, cfg: Optional[Dict] = None) -> List[Dict]:
    """按代码或名称搜索股票：[{code, name}]。

    code 输出 sh600519 格式（可直接用于分析）；keyword 为空/纯代码 → 精确/前缀匹配，
    否则按名称包含匹配（支持中文/拼音片段）。
    """
    kw = str(keyword or '').strip()
    if not kw:
        return []
    from ..adapters.market import MarketDataClient
    from ..adapters.codes import normalize_symbol

    svc = MarketDataClient(cfg or {})
    stocks = svc.fetch_stock_list()  # [{code, name}]
    out: List[Dict] = []
    try:
        norm = normalize_symbol(kw)  # 合法代码 → 精确匹配优先
        digits = norm.split('.')[0]
    except Exception:
        digits = ''.join(ch for ch in kw if ch.isdigit()) or None

    for s in stocks:
        code = str(s.get('code', ''))
        name = str(s.get('name', ''))
        if digits and digits in code:
            out.append({'code': code, 'name': name})
        elif kw.lower() in name.lower():
            out.append({'code': code, 'name': name})
        if len(out) >= limit:
            break
    return out
