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
from ..adapters.tdx_local import (find_blk_file, parse_blk_file,
                                  list_blk_files, _board_name_of)

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DEFAULT_WATCHLIST = os.environ.get(
    "MYSTERY_WATCHLIST",
    os.path.join(_REPO_ROOT, "data", "watchlist.json"),
)
# 通达信自选板块快照（data/ 已被 .gitignore，不入库）。每次同步整体覆盖，
# 保证与 tdx_local 的 blocknew 完全一致。
_DEFAULT_BOARDS_PATH = os.environ.get(
    "MYSTERY_TDX_BOARDS",
    os.path.join(_REPO_ROOT, "data", "tdx_boards.json"),
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
             "source_file": "", "board": [], "added_at": _today()} for c in _DEFAULT_STOCKS]


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
        "board": it.get("board") if isinstance(it.get("board"), list) else [],
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
                      "source_file": "", "board": [], "added_at": today})
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


# ---------------- 通达信自选板块（多 .blk 同步，与 tdx_local 保持一致） ----------------
def enumerate_tdx_boards(cfg: Optional[Dict] = None) -> Dict[str, Dict]:
    """枚举 tdx_local blocknew 全部 .blk → {板块名: {'file', 'codes'}}。

    只读不写通达信；板块名由文件名推导（zxg→自选股，中文名尽力 GBK 还原）。
    多个 blocknew 目录出现同名板块时，以首个命中目录为准。
    """
    cfg = cfg or {}
    files = list_blk_files()
    # config 直接指定 blocknew 目录时，其下 .blk 作为补充（自动探测之外的兜底）
    extra_dir = (cfg.get("tdx") or {}).get("blocknew")
    if extra_dir and os.path.isdir(extra_dir):
        import glob as _glob
        for fp in sorted(_glob.glob(os.path.join(extra_dir, "*.blk"))):
            if fp not in files:
                files.append(fp)
    out: Dict[str, Dict] = {}
    for fp in files:
        name = _board_name_of(fp)
        if name in out:
            continue
        out[name] = {"file": os.path.basename(fp),
                     "codes": parse_blk_file(fp)}
    return out


def list_tdx_board_names(cfg: Optional[Dict] = None) -> List[str]:
    """仅枚举 tdx_local 全部自选板块名（不解析成分股，供 UI 勾选用，轻量）。"""
    cfg = cfg or {}
    files = list_blk_files()
    extra_dir = (cfg.get("tdx") or {}).get("blocknew")
    if extra_dir and os.path.isdir(extra_dir):
        import glob as _glob
        for fp in sorted(_glob.glob(os.path.join(extra_dir, "*.blk"))):
            if fp not in files:
                files.append(fp)
    out: List[str] = []
    for fp in files:
        name = _board_name_of(fp)
        if name not in out:
            out.append(name)
    return out


def sync_tdx_boards(cfg: Optional[Dict] = None,
                    boards: Optional[List[str]] = None,
                    force_full: bool = False,
                    dry_run: bool = False) -> Dict:
    """从 tdx_local 同步自选板块（默认增量合并，--force-full 才整体覆盖）。

    :param boards: 要同步的板块名列表；None / 空 → 同步全部板块。
        同步后 data/tdx_boards.json 仅含所选板块（与通达信对应板块一致）。
    :param force_full: True → 旧行为：watchlist 中 source=tdx_local 的条目
        整体替换为所选板块并集（丢弃已有 tdx 条目的 added_at 等元信息）。
        False（默认）→ 增量合并：保留仍属所选板块并集的 tdx 条目（仅刷新其
        板块归属），仅按差集增删；绝不整体重写，避免误覆盖手工维护的自选股。
    :param dry_run: True → 仅预览增删差集（watchlist_added/removed），
        不写任何文件，便于确认后再真正执行。
    :return: {synced_at, source_dir, boards:{name:{file,n}}, total,
              selected, force_full, watchlist_added, watchlist_removed}
    """
    from ..adapters.tdx_local import resolve_blocknew_dirs

    cfg = cfg or {}
    dirs = resolve_blocknew_dirs()
    all_boards = enumerate_tdx_boards(cfg)

    # 选定要同步的板块
    if boards:
        selected = [b for b in boards if b in all_boards]
        skipped = [b for b in boards if b not in all_boards]
        if skipped:
            logger.warning("指定同步的板块在 tdx 中不存在，已跳过：%s", skipped)
    else:
        selected = list(all_boards.keys())
    if not selected:
        return {"synced_at": _today(), "source_dir": dirs[0] if dirs else "",
                "boards": {}, "total": 0, "selected": bool(boards),
                "watchlist_added": 0, "watchlist_removed": 0}

    chosen = {name: all_boards[name] for name in selected}
    union = sorted({c for b in chosen.values() for c in b["codes"]})
    synced_at = _today()
    source_dir = dirs[0] if dirs else ""

    # 代码 → 所属板块列表（两条路径共用）
    sym_boards: Dict[str, List[str]] = {}
    for name, b in chosen.items():
        for c in b["codes"]:
            sym_boards.setdefault(c, []).append(name)

    # 1) 板块快照（仅含所选板块，与通达信对应板块一致）
    snap = {
        "synced_at": synced_at,
        "source_dir": source_dir,
        "selected": bool(boards),
        "boards": {name: {"file": b["file"], "codes": b["codes"]}
                   for name, b in chosen.items()},
    }

    # 2) 自选股与 tdx 对齐
    items = _load_items()
    non_tdx = [it for it in items if it.get("source") != "tdx_local"]
    existing_tdx = [it for it in items if it.get("source") == "tdx_local"]
    existing_non_tdx = {it["symbol"] for it in non_tdx}

    get_name = None
    try:
        from ..adapters.market import MarketDataClient
        get_name = MarketDataClient(cfg).db.get_stock_name
    except Exception:
        get_name = None

    def _make_tdx_item(sym: str) -> Dict:
        nm = ""
        if get_name is not None:
            try:
                nm = get_name(sym) or ""
            except Exception:
                nm = ""
        return {"symbol": sym, "name": nm or "未知",
                "source": "tdx_local", "source_file": "",
                "board": sym_boards.get(sym, []),
                "added_at": synced_at}

    if force_full:
        # 整体覆盖（旧行为）：tdx 来源条目重建为所选板块并集
        new_tdx = [_make_tdx_item(sym) for sym in union
                   if sym not in existing_non_tdx]
        merged = non_tdx + new_tdx
        added = len(new_tdx)
        removed = len([s for s in {it["symbol"] for it in existing_tdx}
                       if s not in union])
    else:
        # 增量合并（默认）：保留仍属 union 的 tdx 条目（刷新其板块归属），
        # 仅增删差集，不整体重写 → 不丢失手工维护以外的 tdx 元信息
        kept = []
        for it in existing_tdx:
            if it["symbol"] in union:
                it["board"] = sym_boards.get(it["symbol"], [])  # 刷新板块归属
                kept.append(it)
        kept_syms = {it["symbol"] for it in kept}
        dropped = [it for it in existing_tdx if it["symbol"] not in union]
        new_syms = [s for s in union if s not in kept_syms
                    and s not in existing_non_tdx]
        new_tdx = [_make_tdx_item(sym) for sym in new_syms]
        merged = non_tdx + kept + new_tdx
        added = len(new_tdx)
        removed = len(dropped)

    if dry_run:
        # 仅预览差集，不写任何文件
        return {
            "dry_run": True,
            "source_dir": source_dir,
            "boards": {name: {"file": b["file"], "n": len(b["codes"])}
                       for name, b in chosen.items()},
            "total": len(union),
            "selected": bool(boards),
            "force_full": force_full,
            "watchlist_added": added,
            "watchlist_removed": removed,
            "snapshot_boards": len(chosen),
        }

    os.makedirs(os.path.dirname(_DEFAULT_BOARDS_PATH), exist_ok=True)
    with open(_DEFAULT_BOARDS_PATH, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1)
    _write_items(merged)

    return {
        "synced_at": synced_at,
        "source_dir": source_dir,
        "boards": {name: {"file": b["file"], "n": len(b["codes"])}
                   for name, b in chosen.items()},
        "total": len(union),
        "selected": bool(boards),
        "force_full": force_full,
        "watchlist_added": added,
        "watchlist_removed": removed,
    }


def load_tdx_boards() -> Optional[Dict]:
    """读板块快照（data/tdx_boards.json）；未同步过返回 None。"""
    if not os.path.exists(_DEFAULT_BOARDS_PATH):
        return None
    try:
        with open(_DEFAULT_BOARDS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"自选板块读取失败: {e}")
        return None


def refresh_board_names(cfg: Optional[Dict] = None) -> Dict:
    """仅刷新板块展示名（重读 blocknew.cfg/blockname.txt），不动 watchlist 成员。

    用于两类场景，且都无需整体重同步（整体重同步会改写 watchlist）：
    1. 通达信改了板块中文名；
    2. W42.3 名称解析升级（改读 blocknew.cfg）后，让已同步的 tdx_boards.json
       立即生效。

    行为：
    - 按各 .blk 文件重推导展示名，重建 tdx_boards.json 的板块名→成分股
      （同名板块合并 codes）；保留 codes/file/selected/synced_at 等元信息；
    - 同步把 watchlist 中 source=tdx_local 条目的 board 字符串重命名
      （仅改名，不增删任何股票）。
    未同步过时返回 {ok:False}（先 sync-boards）。
    """
    snap = load_tdx_boards()
    if not snap or not snap.get("boards"):
        return {"ok": False, "reason": "尚未同步过板块（先 sync-boards）",
                "refreshed": 0}
    from ..adapters.tdx_local import _board_name_of

    # 1) 按文件重推导展示名，重建 boards（同名合并 codes）
    new_boards: Dict[str, Dict] = {}
    rename: Dict[str, str] = {}  # 旧名 → 新名
    for old_name, b in snap["boards"].items():
        fp = b.get("file", "")
        new_name = _board_name_of(fp) if fp else old_name
        rename[old_name] = new_name
        if new_name in new_boards:
            new_boards[new_name]["codes"] = sorted(
                set(new_boards[new_name]["codes"]) | set(b.get("codes", [])))
        else:
            new_boards[new_name] = {"file": fp,
                                    "codes": list(b.get("codes", []))}
    snap["boards"] = new_boards
    snap["refreshed_at"] = _today()
    os.makedirs(os.path.dirname(_DEFAULT_BOARDS_PATH), exist_ok=True)
    with open(_DEFAULT_BOARDS_PATH, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1)

    # 2) 同步重命名 watchlist 中 tdx_local 条目的 board 字符串（纯改名）
    items = _load_items()
    renamed = 0
    for it in items:
        if it.get("source") != "tdx_local":
            continue
        boards = it.get("board") or []
        new_list = [rename.get(x, x) for x in boards]
        if new_list != boards:
            it["board"] = new_list
            renamed += 1
    if renamed:
        _write_items(items)

    return {"ok": True, "refreshed": len(new_boards),
            "renamed_watchlist": renamed, "refreshed_at": snap["refreshed_at"]}


def list_tdx_boards() -> List[str]:
    """已同步的板块名列表（顺序与文件一致）。"""
    b = load_tdx_boards()
    return list(b.get("boards", {}).keys()) if b else []


def codes_of_board(name: str, boards: Optional[Dict] = None) -> List[str]:
    """板块名 → 成分股代码列表（未同步/不存在返回空）。"""
    b = boards or load_tdx_boards()
    if not b:
        return []
    blk = b.get("boards", {}).get(name)
    return list(blk.get("codes", [])) if blk else []


def boards_of_symbol(symbol: str, boards: Optional[Dict] = None) -> List[str]:
    """代码 → 所属板块名列表（未同步/无归属返回空）。"""
    b = boards or load_tdx_boards()
    if not b:
        return []
    return [name for name, blk in b.get("boards", {}).items()
            if symbol in blk.get("codes", [])]


def tdx_boards_summary() -> Dict:
    """板块快照概要（Web 状态/日志用）。"""
    b = load_tdx_boards()
    if not b:
        return {"synced": False}
    return {"synced": True, "synced_at": b.get("synced_at"),
            "source_dir": b.get("source_dir"),
            "n_boards": len(b.get("boards", {})),
            "boards": {name: len(blk.get("codes", []))
                       for name, blk in b.get("boards", {}).items()}}


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
