"""test_watchlist — 自选股新格式 + tdx_local 导入（003.md，不依赖本机通达信）。"""
from __future__ import annotations

import json

import pytest

from mystery.services import watchlist as wl
from mystery.adapters import tdx_local


@pytest.fixture
def tmp_wl(tmp_path, monkeypatch):
    """把 watchlist 文件重定向到 tmp，隔离真实 data/watchlist.json。"""
    p = str(tmp_path / "watchlist.json")
    monkeypatch.setattr(wl, "_DEFAULT_WATCHLIST", p)
    wl.save_watchlist([])  # 空自选起步，避免默认 6 只干扰断言
    return p


def test_old_string_array_upgraded(tmp_wl):
    """旧字符串数组能读，且自动升级为对象列表（source=manual）写回。"""
    with open(tmp_wl, "w", encoding="utf-8") as f:
        json.dump(["sh600519", "000001"], f)
    items = wl.load_watchlist_items()
    assert {it["symbol"] for it in items} == {"600519.SH", "000001.SZ"}
    assert all(it["source"] == "manual" for it in items)
    with open(tmp_wl, encoding="utf-8") as f:
        raw = json.load(f)
    assert isinstance(raw[0], dict)


def test_load_watchlist_returns_symbols(tmp_wl):
    wl.add_to_watchlist("sh600519", name="贵州茅台", source="manual")
    assert wl.load_watchlist() == ["600519.SH"]


def test_add_preserves_existing_source(tmp_wl):
    wl.add_to_watchlist("600519.SH", name="茅台", source="manual")
    # 再次加入（tdx_local）→ 已存在保留原 source
    wl.add_to_watchlist("600519.SH", name="茅台", source="tdx_local")
    items = wl.load_watchlist_items()
    assert len(items) == 1
    assert items[0]["source"] == "manual"


def test_remove_from_watchlist(tmp_wl):
    wl.add_to_watchlist("600519.SH", source="manual")
    wl.add_to_watchlist("000001.SZ", source="manual")
    wl.remove_from_watchlist("600519.SH")
    assert [it["symbol"] for it in wl.load_watchlist_items()] == ["000001.SZ"]


def test_parse_blk_file_gbk(tmp_path):
    """假 zxg.blk（GBK 字节）：7位 1xxx→SH、0xxx→SZ；6位按规则推断。"""
    p = tmp_path / "zxg.blk"
    p.write_bytes("1600519\n0000001\n600036\n".encode("gbk"))
    codes = tdx_local.parse_blk_file(str(p))
    assert "600519.SH" in codes
    assert "000001.SZ" in codes
    assert "600036.SH" in codes
    assert len(codes) == 3


def test_parse_blk_file_latin1(tmp_path):
    """latin-1/纯 ASCII 字节同样可解析。"""
    p = tmp_path / "zxg.blk"
    p.write_bytes(b"1600519\r\n")
    assert tdx_local.parse_blk_file(str(p)) == ["600519.SH"]


def test_import_from_tdx_merge(tmp_path, tmp_wl, monkeypatch):
    """从假 zxg.blk 导入：合并不覆盖已有 source，新代码 source=tdx_local。"""
    wl.add_to_watchlist("600519.SH", name="贵州茅台", source="manual")
    blk = tmp_path / "zxg.blk"
    blk.write_bytes("1600519\n0000001\n".encode("gbk"))
    monkeypatch.setattr(tdx_local, "find_blk_file",
                        lambda filename="zxg.blk": str(blk))
    cfg = {"db_path": str(tmp_path / "none.db")}  # 空库，名称查询返回"未知"
    r = wl.import_from_tdx(cfg)
    assert r["imported"] == 1          # 600519 已存在跳过，000001 新增
    assert r["skipped"] == 1
    by = {it["symbol"]: it for it in wl.load_watchlist_items()}
    assert by["600519.SH"]["source"] == "manual"       # 不覆盖原 source
    assert by["000001.SZ"]["source"] == "tdx_local"
    assert by["000001.SZ"]["source_file"] == "zxg.blk"


def test_import_from_tdx_no_file(tmp_wl, monkeypatch):
    """文件不存在：提示路径不报栈。"""
    monkeypatch.setattr(tdx_local, "find_blk_file",
                        lambda filename="zxg.blk": "")
    r = wl.import_from_tdx({})
    assert r == {"imported": 0, "skipped": 0, "path": ""}


def test_source_label():
    assert wl.source_label("tdx_local") == "通达信（zxg.blk）"
    assert wl.source_label("manual") == "手工"
    assert wl.source_label("scan") == "扫描加入"
    assert wl.source_label("bogus") == "手工"
