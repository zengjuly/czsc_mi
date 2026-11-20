"""W112：自选股导入/导出功能测试（隔离数据，不碰真实 watchlist.json）。"""
import json
import os
import pytest


@pytest.fixture
def isolated_watchlist(tmp_path, monkeypatch):
    """把 WATCHLIST 指向临时空文件，每个测试独立。"""
    path = tmp_path / "watchlist.json"
    path.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("MYSTERY_WATCHLIST", str(path))
    # watchlist 模块在导入时读取路径，需 reload 使其生效
    import importlib
    from mystery.services import watchlist as wl
    importlib.reload(wl)
    return wl


def test_export_formats(isolated_watchlist):
    wl = isolated_watchlist
    wl.save_watchlist(["600519.SH", "000001.SZ"])

    j = json.loads(wl.export_watchlist("json"))
    assert len(j) == 2
    assert j[0]["symbol"] == "600519.SH"

    csv = wl.export_watchlist("csv")
    assert "代码,名称,来源" in csv
    assert "600519.SH" in csv

    txt = wl.export_watchlist("txt")
    assert txt.splitlines() == ["600519.SH", "000001.SZ"]


def test_import_txt_merge(isolated_watchlist):
    wl = isolated_watchlist
    wl.save_watchlist(["600519.SH"])
    r = wl.import_watchlist("600519.SH\n600036.SH\n300750.SZ", fmt="txt", mode="merge")
    assert r["added"] == 2
    assert r["skipped"] == 1
    assert set(wl.load_watchlist()) == {"600519.SH", "600036.SH", "300750.SZ"}


def test_import_txt_replace(isolated_watchlist):
    wl = isolated_watchlist
    wl.save_watchlist(["600519.SH"])
    r = wl.import_watchlist("000001.SZ,000002.SZ", fmt="txt", mode="replace")
    assert r["added"] == 2
    assert set(wl.load_watchlist()) == {"000001.SZ", "000002.SZ"}


def test_import_json_keeps_full_info(isolated_watchlist):
    wl = isolated_watchlist
    items = [
        {"symbol": "600872.SH", "name": "中炬高新", "source": "manual",
         "board": [], "added_at": "2026-11-05"},
        {"symbol": "000400.SZ", "name": "许继电气", "source": "tdx_local",
         "board": ["核心", "关注"], "added_at": "2026-11-05"},
    ]
    r = wl.import_watchlist(json.dumps(items, ensure_ascii=False), fmt="json", mode="replace")
    assert r["added"] == 2
    out = wl.load_watchlist_items()
    zh = [x for x in out if x["symbol"] == "000400.SZ"][0]
    assert zh["name"] == "许继电气"
    assert zh["source"] == "tdx_local"
    assert zh["board"] == ["核心", "关注"]


def test_import_auto_detect(isolated_watchlist):
    wl = isolated_watchlist
    r = wl.import_watchlist('[{"symbol":"600519.SH","name":"贵州茅台"}]', fmt="auto")
    assert r["fmt"] == "json"
    assert "600519.SH" in wl.load_watchlist()


def test_import_dedup(isolated_watchlist):
    wl = isolated_watchlist
    r = wl.import_watchlist("600519.SH\nsh600519\n600519.SH", fmt="txt", mode="merge")
    # sh600519 与 600519.SH 规范化后相同，只加 1 只
    assert r["added"] == 1
    assert wl.load_watchlist() == ["600519.SH"]


def test_import_csv(isolated_watchlist):
    wl = isolated_watchlist
    csv = "代码,名称,来源,板块\n600519.SH,贵州茅台,manual,\n000001.SZ,平安银行,tdx_local,关注|核心\n"
    r = wl.import_watchlist(csv, fmt="csv", mode="replace")
    assert r["added"] == 2
    items = {x["symbol"]: x for x in wl.load_watchlist_items()}
    assert items["000001.SZ"]["board"] == ["关注", "核心"]
