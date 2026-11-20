"""W113 测试：持仓标记 + 同花顺/通达信导入导出 + 飞书通知分组。"""
import importlib
import json
import os

import pytest


@pytest.fixture
def wl(tmp_path, monkeypatch):
    from mystery.services import watchlist as w
    monkeypatch.setenv("MYSTERY_WATCHLIST", str(tmp_path / "watchlist.json"))
    importlib.reload(w)
    # 显式写空，避免默认 6 只占位干扰
    w._write_items([])
    return w


# ---------- 持仓标记 ----------
def test_set_held_and_query(wl):
    wl.add_to_watchlist("600519.SH", name="贵州茅台")
    assert wl.is_held("600519.SH") is False
    wl.set_held("600519.SH", True)
    assert wl.is_held("600519.SH") is True
    assert wl.held_codes() == ["600519.SH"]
    wl.set_held("600519.SH", False)
    assert wl.held_codes() == []


def test_set_held_auto_add(wl):
    wl.set_held("000001.SZ", True)  # 不在自选里 → 自动加入
    assert "000001.SZ" in wl.load_watchlist()
    assert wl.is_held("000001.SZ") is True


def test_held_survives_reload(wl):
    wl.set_held("600036.SH", True)
    importlib.reload(wl)
    assert wl.is_held("600036.SH") is True


def test_json_export_keeps_held(wl):
    wl.set_held("300750.SZ", True)
    data = json.loads(wl.export_watchlist("json"))
    assert any(x["symbol"] == "300750.SZ" and x["held"] for x in data)


# ---------- 同花顺 / 通达信格式 ----------
def test_import_ths(wl):
    text = "600519 贵州茅台\n000001 平安银行\n300750.SZ 宁德时代"
    r = wl.import_watchlist(text, fmt="ths", mode="replace")
    assert r["added"] == 3
    assert set(wl.load_watchlist()) == {"600519.SH", "000001.SZ", "300750.SZ"}
    items = {x["symbol"]: x for x in wl.load_watchlist_items()}
    assert items["600519.SH"]["name"] == "贵州茅台"


def test_import_tdx(wl):
    # 首位 1=沪/0=深/2=北
    text = "1600519\n0000001\n0300750\n600519"
    r = wl.import_watchlist(text, fmt="tdx", mode="replace")
    assert r["added"] == 3  # 600519 与 1600519 重复
    assert set(wl.load_watchlist()) == {"600519.SH", "000001.SZ", "300750.SZ"}


def test_export_ths_tdx(wl):
    wl.add_to_watchlist("600519.SH", name="贵州茅台")
    wl.add_to_watchlist("000001.SZ", name="平安银行")
    ths = wl.export_watchlist("ths")
    assert "600519 贵州茅台" in ths
    tdx = wl.export_watchlist("tdx")
    assert "1600519" in tdx and "0000001" in tdx


def test_csv_export_import_held(wl):
    wl.set_held("600519.SH", True)
    csv_text = wl.export_watchlist("csv")
    assert "持仓" in csv_text.splitlines()[0]
    importlib.reload(wl)
    wl._write_items([])
    wl.import_watchlist(csv_text, fmt="csv", mode="replace")
    assert wl.is_held("600519.SH") is True


# ---------- 飞书通知分组 ----------
def _ev(code, freq, to, held=False, above_5w=False, name=""):
    return {"code": code, "name": name, "freq": freq, "to": to,
            "from": "死叉" if to == "金叉" else "金叉",
            "dif": 0.01, "dea": 0.02, "held": held, "above_5w": above_5w}


def test_notification_group_order():
    from mystery.services.minute_macd import format_notification
    events = [
        _ev("600519.SH", "30m", "金叉", held=False),          # 组6 其他30金叉
        _ev("000001.SZ", "60m", "死叉", held=False),          # 组7 其他死叉
        _ev("300750.SZ", "60m", "金叉", held=True),           # 组2 持仓60金叉
        _ev("600036.SH", "30m", "死叉", held=True),           # 组1 持仓死叉
        _ev("002594.SZ", "60m", "金叉", held=False, above_5w=True),  # 组4 强势
        _ev("601318.SH", "60m", "金叉", held=False),          # 组5 其他60金叉
        _ev("000858.SZ", "30m", "金叉", held=True),           # 组3 持仓30金叉
    ]
    text = format_notification(events, first_run=False)
    pos = {k: text.index(k) for k in [
        "持仓股·死叉", "持仓股·60分钟金叉", "持仓股·30分钟金叉",
        "站上5周均线", "其他自选·60分钟金叉", "其他自选·30分钟金叉",
        "其他自选·死叉"]}
    vals = list(pos.values())
    assert vals == sorted(vals), f"分组顺序错误: {pos}"


def test_notification_empty():
    from mystery.services.minute_macd import format_notification
    assert format_notification([]) == ""
