"""mystery.services.watchlist — 自选股管理 + 股票名搜索 + 通达信自选导入。

自选股存 data/watchlist.json（gitignore，不入库）；个股页/daily/扫描共用。
新格式（003.md）：对象列表 [{symbol, name, source, source_file, added_at}]。
source 枚举：tdx_local | manual | scan。旧字符串数组读到后自动升级为 manual。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date
from typing import Dict, List, Optional

from ..adapters.codes import normalize_symbol

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DEFAULT_WATCHLIST = os.environ.get(
    "MYSTERY_WATCHLIST",
    os.path.join(_REPO_ROOT, "data", "watchlist.json"),
)
_DEFAULT_STOCKS = ['sh600519', 'sz000001', 'sh600150', 'sh600036',
                   'sz000858', 'sz300750']

_SOURCES = ("tdx_local", "manual", "scan")
SOURCE_LABELS = {
    "tdx_local": "通达信（zxg.blk）",
    "manual": "手工",
    "scan": "扫描加入",
}


def _today() -> str:
    return date.today().isoformat()


def _default_items() -> List[Dict]:
    return [{"symbol": normalize_symbol(c), "name": "", "source": "manual",
             "source_file": "", "added_at": _today()} for c in _DEFAULT_STOCKS]


def _normalize_item(it: Dict) -> Optional[Dict]:
    if not isinstance(it, dict) or not it.get("symbol"):
        return None
    try:
        sym = normalize_symbol(str(it["symbol"]))
    except Exception:
        return None
    src = it.get("source") if it.get("source") in _SOURCES else "manual"
    return {
        "symbol": sym,
        "name": str(it.get("name") or ""),
        "source": src,
        "source_file": str(it.get("source_file") or ""),
        "added_at": str(it.get("added_at") or ""),
    }


def _load_items() -> List[Dict]:
    """读 watchlist.json → 对象列表。旧字符串数组自动升级为 manual 并写回。"""
    if not os.path.exists(_DEFAULT_WATCHLIST):
        items = _default_items()
        _write_items(items)
        return items
    try:
        with open(_DEFAULT_WATCHLIST, encoding='utf-8') as f:
            raw = json.load(f)
    except Exception as e:
        logger.warning(f"自选股读取失败: {e}")
        return _default_items()
    if not isinstance(raw, list):
        return _default_items()
    if raw and isinstance(raw[0], str):
        # 旧格式：字符串数组 → 视为 manual，升级写回新格式
        items = []
        for c in raw:
            try:
                sym = normalize_symbol(str(c))
            except Exception:
                continue
            items.append({"symbol": sym, "name": "", "source": "manual",
                          "source_file": "", "added_at": _today()})
        _write_items(items)
        return items
    items = []
    for it in raw:
        norm = _normalize_item(it)
        if norm:
            items.append(norm)
    return items


def _write_items(items: List[Dict]) -> None:
    os.makedirs(os.path.dirname(_DEFAULT_WATCHLIST), exist_ok=True)
    with open(_DEFAULT_WATCHLIST, 'w', encoding='utf-8') as f:
        json.dump(items, f, ensure_ascii=False, indent=1)


def load_watchlist_items() -> List[Dict]:
    """自选股条目列表（Web 子页用，含 source/name）。"""
    return _load_items()


def load_watchlist() -> List[str]:
    """自选股代码列表（600519.SH 格式，供 daily --watchlist / 分析）。"""
    return [it["symbol"] for it in _load_items()]


def save_watchlist(codes: List[str]) -> None:
    """兼容旧接口：以代码列表整体覆盖（全部 source=manual）。"""
    today = _today()
    items = []
    for c in codes:
        try:
            sym = normalize_symbol(str(c))
        except Exception:
            continue
        items.append({"symbol": sym, "name": "", "source": "manual",
                      "source_file": "", "added_at": today})
    _write_items(items)


def add_to_watchlist(code: str, name: str = "", source: str = "manual",
                     source_file: str = "") -> List[Dict]:
    """加入自选（source ∈ tdx_local|manual|scan）；已存在则保留原 source。"""
    items = _load_items()
    try:
        sym = normalize_symbol(str(code))
    except Exception:
        return items
    if not any(it["symbol"] == sym for it in items):
        items.append({"symbol": sym, "name": name or "",
                      "source": source if source in _SOURCES else "manual",
                      "source_file": source_file or "",
                      "added_at": _today()})
        _write_items(items)
    return items


def remove_from_watchlist(code: str) -> List[Dict]:
    try:
        sym = normalize_symbol(str(code))
    except Exception:
        sym = str(code)
    items = [it for it in _load_items() if it["symbol"] != sym]
    _write_items(items)
    return items


def source_label(source: str, source_file: str = "") -> str:
    """来源文案：tdx_local → 通达信（文件名）；manual → 手工；scan → 扫描加入。"""
    if source == "tdx_local":
        return f"通达信（{source_file or 'zxg.blk'}）"
    return SOURCE_LABELS.get(source, "手工")


def import_from_tdx(cfg: Optional[Dict] = None,
                    filename: str = "zxg.blk") -> Dict:
    """从通达信本地自选 .blk 导入（合并不覆盖，只读不写通达信）。

    :return: {imported: int, skipped: int, path: str or ''}
    """
    from ..adapters.tdx_local import find_blk_file, parse_blk_file

    cfg = cfg or {}
    path = (cfg.get("tdx") or {}).get("blocknew") or ""
    if path and not path.endswith(".blk"):
        path = os.path.join(path, filename)
    if not (path and os.path.isfile(path)):
        path = find_blk_file(filename) or ""
    if not path:
        return {"imported": 0, "skipped": 0, "path": ""}

    codes = parse_blk_file(path)
    items = _load_items()
    existing = {it["symbol"] for it in items}

    # 一次性建名称查询（复用 client，避免每只 new MarketDataClient）
    get_name = None
    try:
        from ..adapters.market import MarketDataClient
        get_name = MarketDataClient(cfg).db.get_stock_name
    except Exception:
        get_name = None

    imported = 0
    for sym in codes:
        if sym in existing:
            continue
        name = ""
        if get_name is not None:
            try:
                name = get_name(sym) or ""
            except Exception:
                name = ""
        items.append({"symbol": sym, "name": name or "未知", "source": "tdx_local",
                      "source_file": os.path.basename(path),
                      "added_at": _today()})
        existing.add(sym)
        imported += 1
    if imported:
        _write_items(items)
    return {"imported": imported, "skipped": len(codes) - imported,
            "path": path}


def search_stock(keyword: str, limit: int = 10, cfg: Optional[Dict] = None) -> List[Dict]:
    """按代码或名称搜索股票：[{code, name}]。

    code 输出 sh600519 格式（可直接用于分析）；keyword 为空/纯代码 → 精确/前缀匹配，
    否则按名称包含匹配（支持中文/拼音片段）。
    """
    kw = str(keyword or '').strip()
    if not kw:
        return []
    from ..adapters.market import MarketDataClient

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
