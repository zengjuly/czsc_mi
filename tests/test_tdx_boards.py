"""test_tdx_boards — tdx_local 自选板块同步 + 划分（W42）。

不依赖本机通达信：临时构造 blocknew/*.blk 并 monkeypatch 目录探测。
"""
from __future__ import annotations

import json
import os

import pytest

from mystery.adapters import tdx_local
from mystery.services import watchlist as wl
from mystery.services.scan import scan_market


def _make_blocknew(tmp_path, files: dict) -> str:
    """files: {filename: [code_lines]}。返回 blocknew 目录路径。"""
    d = tmp_path / "T0002" / "blocknew"
    d.mkdir(parents=True)
    for fn, lines in files.items():
        p = d / fn
        p.write_bytes(("\n".join(lines) + "\n").encode("gbk"))
    return str(d)


@pytest.fixture
def patch_tdx(monkeypatch, tmp_path):
    """把 blocknew 目录重定向到 tmp，并把自选股/板块快照文件也重定向。

    W65：必须同时隔离 `_DEFAULT_WATCHLIST`。此前只隔离了 `_DEFAULT_BOARDS_PATH`，
    `wl.save_watchlist([])` 会清空生产 `data/watchlist.json`，随后 sync 写入
    mock 的 3 只（600519/000001/600036），导致日报扫描范围被测试污染。
    """
    blk_dir = _make_blocknew(tmp_path, {
        "zxg.blk": ["1600519", "0000001"],          # → 自选股
        "龙头股.blk": ["1600519", "600036"],          # GBK 文件名
    })
    monkeypatch.setattr(tdx_local, "resolve_blocknew_dirs",
                        lambda: [blk_dir])
    boards_path = str(tmp_path / "tdx_boards.json")
    monkeypatch.setattr(wl, "_DEFAULT_BOARDS_PATH", boards_path)
    # W65：自选股文件同样重定向，杜绝测试写生产 data/watchlist.json
    monkeypatch.setattr(wl, "_DEFAULT_WATCHLIST",
                        str(tmp_path / "watchlist.json"))
    wl.save_watchlist([])  # 空自选起步
    return blk_dir


def test_enumerate_tdx_boards(patch_tdx):
    boards = wl.enumerate_tdx_boards({})
    assert "自选股" in boards          # zxg.blk → 自选股
    assert "龙头股" in boards
    assert set(boards["自选股"]["codes"]) == {"600519.SH", "000001.SZ"}
    assert set(boards["龙头股"]["codes"]) == {"600519.SH", "600036.SH"}
    assert boards["自选股"]["file"] == "zxg.blk"


def test_sync_tdx_boards_writes_snapshot_and_reconciles(patch_tdx, tmp_path):
    # 起始：000001.SZ 为手动自选（应保留，不被 tdx 覆盖）
    wl.add_to_watchlist("000001.SZ", source="manual")
    r = wl.sync_tdx_boards({})
    assert r["total"] == 3
    assert set(r["boards"].keys()) == {"自选股", "龙头股"}
    assert r["watchlist_added"] == 2   # 600519 / 600036 新增 tdx
    assert r["watchlist_removed"] == 0

    # 快照文件已写入且与 tdx 一致
    snap = wl.load_tdx_boards()
    assert snap["boards"]["自选股"]["codes"] == ["600519.SH", "000001.SZ"]
    assert set(snap["boards"]["龙头股"]["codes"]) == {"600519.SH", "600036.SH"}

    # watchlist：手动 000001 保留；tdx 来源为板块并集（去重）
    items = {it["symbol"]: it for it in wl.load_watchlist_items()}
    assert items["000001.SZ"]["source"] == "manual"        # 不覆盖手动
    assert items["600519.SH"]["source"] == "tdx_local"
    assert items["600519.SH"]["board"] == ["自选股", "龙头股"]
    assert items["600036.SH"]["board"] == ["龙头股"]


def test_sync_tdx_boards_consistency_overwrite(patch_tdx, tmp_path):
    """再同步：tdx 删除的股票要从 watchlist 的 tdx 来源中移除（与 tdx 一致）。"""
    wl.sync_tdx_boards({})  # 首次同步，tdx 含 600519/000001/600036
    # 把 600519 从两个板块都删掉，并给自选股新增 600999（龙头股仅留 600036）
    d = patch_tdx
    with open(os.path.join(d, "zxg.blk"), "wb") as f:
        f.write("1600999\n0000001\n".encode("gbk"))
    with open(os.path.join(d, "龙头股.blk"), "wb") as f:
        f.write("600036\n".encode("gbk"))
    r = wl.sync_tdx_boards({})
    snap = wl.load_tdx_boards()
    union = {c for b in snap["boards"].values() for c in b["codes"]}
    assert "600519.SH" not in union
    assert "600999.SH" in union
    # 600519 原本是 tdx 来源 → 应被移除（removed 计入）
    assert r["watchlist_removed"] == 1
    items = {it["symbol"]: it for it in wl.load_watchlist_items()}
    assert "600519.SH" not in items
    assert "600999.SH" in items
    assert items["600999.SH"]["source"] == "tdx_local"


def test_board_classification_helpers(patch_tdx):
    wl.sync_tdx_boards({})
    assert set(wl.list_tdx_boards()) == {"自选股", "龙头股"}
    assert wl.codes_of_board("自选股") == ["600519.SH", "000001.SZ"]
    assert wl.codes_of_board("不存在") == []
    assert wl.boards_of_symbol("600519.SH") == ["自选股", "龙头股"]
    assert wl.boards_of_symbol("600036.SH") == ["龙头股"]
    assert wl.boards_of_symbol("999999.SH") == []
    s = wl.tdx_boards_summary()
    assert s["synced"] is True
    assert s["boards"]["自选股"] == 2


def test_scan_market_board_tags_results(monkeypatch):
    """scan_market(board=) 用板块成分股作 universe，并给结果打 board 标记。"""
    monkeypatch.setattr(wl, "codes_of_board",
                        lambda name, boards=None: ["600519.SH", "600036.SH"])
    captured = {}

    def fake_impl(**kwargs):
        captured.update(kwargs)
        return [{"symbol": "600519.SH", "score": 10},
                {"symbol": "600036.SH", "score": 20}]
    monkeypatch.setattr("mystery.services.scan._scan_market_impl", fake_impl)

    res = scan_market(board="龙头股", force=True, no_persist=True)
    assert [r["symbol"] for r in res] == ["600519.SH", "600036.SH"]
    assert all(r.get("board") == "龙头股" for r in res)
    # universe 应被设为板块成分股，scan_type=board
    assert captured["universe"] == ["600519.SH", "600036.SH"]
    assert captured["scan_type"] == "board"


def test_scan_market_unknown_board_returns_empty(monkeypatch):
    monkeypatch.setattr(wl, "codes_of_board", lambda name, boards=None: [])
    called = {"n": 0}

    def fake_impl(**kwargs):
        called["n"] += 1
        return [{"symbol": "x"}]
    monkeypatch.setattr("mystery.services.scan._scan_market_impl", fake_impl)
    res = scan_market(board="不存在的板块", force=True, no_persist=True)
    assert res == []
    assert called["n"] == 0  # 空板块不触发实际扫描


def test_list_tdx_board_names(patch_tdx):
    names = wl.list_tdx_board_names({})
    assert "自选股" in names
    assert "龙头股" in names
    assert len(names) == 2  # 轻量枚举，不解析成分股，仅文件名级


def test_sync_tdx_boards_partial_selection(patch_tdx):
    """只同步选定板块：快照与 tdx_local 条目都仅含所选板块。"""
    # 当前已无手动股；仅同步「龙头股」板块
    r = wl.sync_tdx_boards({}, boards=["龙头股"])
    assert r["total"] == 2                       # 600519 / 600036
    assert set(r["boards"].keys()) == {"龙头股"}  # 自选股不在快照里
    assert r["selected"] is True

    snap = wl.load_tdx_boards()
    assert set(snap["boards"].keys()) == {"龙头股"}
    assert "自选股" not in snap["boards"]

    # watchlist 的 tdx_local 条目 = 龙头股并集（不含自选股板块的 000001）
    items = {it["symbol"]: it for it in wl.load_watchlist_items()}
    assert set(items.keys()) == {"600519.SH", "600036.SH"}
    assert items["600519.SH"]["board"] == ["龙头股"]
    # selected 标记写入快照
    assert snap["selected"] is True


def test_sync_tdx_boards_partial_then_add(patch_tdx):
    """先同步龙头股，再叠加自选股：多选即增量（默认已勾选已同步）。"""
    wl.sync_tdx_boards({}, boards=["龙头股"])
    r = wl.sync_tdx_boards({}, boards=["龙头股", "自选股"])
    assert set(r["boards"].keys()) == {"龙头股", "自选股"}
    assert r["total"] == 3                       # 600519 / 600036 / 000001
    items = {it["symbol"]: it for it in wl.load_watchlist_items()}
    # 600519 现在同时属于两个板块
    assert set(items["600519.SH"]["board"]) == {"龙头股", "自选股"}


def test_sync_tdx_boards_partial_ignores_unknown(patch_tdx):
    """指定不存在的板块名 → 跳过，不报错；返回空。"""
    r = wl.sync_tdx_boards({}, boards=["不存在的板块"])
    assert r["boards"] == {}
    assert r["total"] == 0
    assert r["selected"] is True
    items = wl.load_watchlist_items()
    assert all(it["source"] != "tdx_local" for it in items)  # 未引入任何 tdx 股


# ---------------- ② 刷新板块名称（不动 watchlist 成员） ----------------

def test_refresh_board_names_restores_names_and_renames(patch_tdx, tmp_path):
    """refresh_board_names 重读 blocknew.cfg 还原板块名，并重命名 watchlist
    board 字段，但成员（股票集合）保持不变。"""
    wl.sync_tdx_boards({})  # 建立正确快照 + watchlist
    sym_before = set(wl.load_watchlist())

    # 篡改：把快照名改成错名，并同步改 watchlist 的 board 字段
    snap = wl.load_tdx_boards()
    bad = {"BAD_自选": snap["boards"]["自选股"],
           "BAD_龙头": snap["boards"]["龙头股"]}
    with open(wl._DEFAULT_BOARDS_PATH, "w", encoding="utf-8") as f:
        json.dump({**snap, "boards": bad}, f, ensure_ascii=False, indent=1)
    items = wl.load_watchlist_items()
    for it in items:
        if it["source"] == "tdx_local":
            it["board"] = ["BAD_自选" if b == "自选股" else
                           ("BAD_龙头" if b == "龙头股" else b) for b in it["board"]]
    wl._write_items(items)

    r = wl.refresh_board_names({})
    assert r["ok"] is True
    assert set(wl.load_tdx_boards()["boards"].keys()) == {"自选股", "龙头股"}
    # watchlist 成员不变，board 字段被重命名回正确名
    assert set(wl.load_watchlist()) == sym_before
    boards = {it["symbol"]: it["board"] for it in wl.load_watchlist_items()}
    assert boards["600519.SH"] == ["自选股", "龙头股"]
    assert boards["600036.SH"] == ["龙头股"]


def test_refresh_board_names_without_sync_returns_false(patch_tdx, tmp_path):
    """未同步过时 refresh 应返回 ok=False（先 sync-boards）。"""
    # patch_tdx 仅 save_watchlist，未建 tdx_boards.json 快照
    r = wl.refresh_board_names({})
    assert r["ok"] is False


# ---------------- ③ 增量合并（默认）vs 整体覆盖 --force-full / --dry-run ----------------

def test_sync_incremental_preserves_added_at(patch_tdx, tmp_path):
    """默认增量：保留仍属 union 的 tdx 条目元信息（added_at 不被重置）。"""
    wl.sync_tdx_boards({})
    items = wl.load_watchlist_items()
    for it in items:
        if it["symbol"] == "600519.SH":
            it["added_at"] = "2000-01-01"
    wl._write_items(items)
    r = wl.sync_tdx_boards({})  # 增量
    items2 = wl.load_watchlist_items()
    six = [it for it in items2 if it["symbol"] == "600519.SH"][0]
    assert six["added_at"] == "2000-01-01"   # 元信息保留
    assert r["watchlist_added"] == 0
    assert r["watchlist_removed"] == 0


def test_sync_force_full_resets_added_at(patch_tdx, tmp_path):
    """--force-full 整体覆盖：保留的 tdx 条目 added_at 被重建为今日。"""
    wl.sync_tdx_boards({})
    items = wl.load_watchlist_items()
    for it in items:
        if it["symbol"] == "600519.SH":
            it["added_at"] = "2000-01-01"
    wl._write_items(items)
    r = wl.sync_tdx_boards({}, force_full=True)
    assert r["force_full"] is True
    items2 = wl.load_watchlist_items()
    six = [it for it in items2 if it["symbol"] == "600519.SH"][0]
    assert six["added_at"] == wl._today()   # 重建为今日


def test_sync_dry_run_preview_only(patch_tdx, tmp_path):
    """--dry-run 只预览差集，不写任何文件。"""
    wl.sync_tdx_boards({})
    d = patch_tdx
    # 600519 同时从自选股与龙头股移除 → 真正移出 union
    with open(os.path.join(d, "zxg.blk"), "wb") as f:
        f.write("0000001\n".encode("gbk"))
    with open(os.path.join(d, "龙头股.blk"), "wb") as f:
        f.write("600036\n".encode("gbk"))
    r = wl.sync_tdx_boards({}, dry_run=True)
    assert r["dry_run"] is True
    assert r["watchlist_removed"] == 1   # 预览应报告移除 600519
    # 实际上未写：watchlist 仍含 600519
    assert "600519.SH" in {it["symbol"] for it in wl.load_watchlist_items()}

