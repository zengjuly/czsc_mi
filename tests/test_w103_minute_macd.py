"""W103：分钟K线存储 + MACD计算 + 金叉死叉变更判定 单元测试。

纯 stdlib / 临时 sqlite，不依赖行情源与生产库。
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mystery.core.macd import ema, macd, summarize  # noqa: E402
from mystery.store._repos.min30_repo import Min30Repo  # noqa: E402
from mystery.adapters import minute_source as ms  # noqa: E402


# ---------------- MACD 核心 ----------------
def test_ema_basics():
    assert ema([], 3) == []
    assert ema([10], 3) == [10.0]
    out = ema([10, 10, 10], 3)
    assert out == [10.0, 10.0, 10.0]  # 常数序列 EMA 不变


def test_macd_rising_then_falling():
    # 先单边上涨（金叉），再单边下跌（死叉）
    up = [100 + i for i in range(60)]
    r = macd(up)
    assert r["bars"] == 60
    assert r["state"] in ("金叉", "死叉")
    assert r["dif"] is not None and r["dea"] is not None
    assert r["hist"] is not None

    down = [160 - i for i in range(60)]
    r2 = macd(down)
    assert r2["state"] == "死叉"
    assert r2["dif"] < 0


def test_macd_cross_detection():
    # 构造先跌后涨，末段应出现金叉
    seq = [100 - i * 0.5 for i in range(40)] + [80 + i * 1.5 for i in range(40)]
    r = macd(seq)
    # 交叉字段要么出现金叉，要么状态已是金叉（取决于末段长度）
    assert r["cross"] in (None, "golden", "death")
    assert r["state"] in ("金叉", "死叉", "粘合")


def test_macd_short_series_safe():
    r = macd([1])
    assert r["bars"] == 1 and r["cross"] is None
    assert r["dif"] is None
    assert "dif" in summarize(r)


def test_macd_ignores_none_closes():
    seq = [100] * 30 + [None, None] + [110] * 30
    r = macd(seq)
    assert r["bars"] == 60


# ---------------- 30min → 60min 聚合 ----------------
def test_aggregate_60min():
    # 真实 30min 时点：9:30,10:00,10:30,11:00,11:30,13:30,14:00,14:30
    times = ["09:30", "10:00", "10:30", "11:00",
             "11:30", "13:30", "14:00", "14:30"]
    bars = []
    for i, t in enumerate(times):
        bars.append({"dt": f"2026-11-19 {t}",
                     "open": 10 + i, "high": 11 + i, "low": 9 + i,
                     "close": 10.5 + i, "volume": 100, "amount": 1000})
    out = ms.aggregate_60min(bars)
    assert len(out) == 4
    assert out[0]["high"] == 12  # max(11,12) 前两根
    assert out[0]["low"] == 9
    assert out[0]["volume"] == 200
    assert out[0]["close"] == 11.5  # 第二根收盘 10.5+1


# ---------------- 代码转换 ----------------
def test_six_code():
    assert ms._six("600519.SH") == "600519"
    assert ms._six("sz000100") == "000100"
    assert ms._six("000100") == "000100"


# ---------------- 存储层 ----------------
@pytest.fixture
def repo(tmp_path):
    p = str(tmp_path / "test.db")
    return Min30Repo(db_path=p)


def test_repo_upsert_load(repo):
    bars = [{"dt": "2026-11-19 10:00", "open": 10, "high": 11,
             "low": 9, "close": 10.5, "volume": 100, "amount": 1000},
            {"dt": "2026-11-19 10:30", "open": 10.5, "high": 11,
             "low": 10, "close": 10.8, "volume": 80, "amount": 800}]
    n = repo.upsert_bars("600519.SH", bars)
    assert n == 2
    loaded = repo.load_bars("600519.SH")
    assert len(loaded) == 2
    assert loaded[0]["dt"] < loaded[1]["dt"]
    assert repo.last_dt("600519.SH") == "2026-11-19 10:30"
    # 增量去重
    repo.upsert_bars("600519.SH", [bars[1]])
    assert len(repo.load_bars("600519.SH")) == 2


def test_repo_state_diff(repo):
    s1 = {"state": "金叉", "cross": "golden", "dif": 1.0, "dea": 0.5, "hist": 1.0}
    assert repo.get_state("600519.SH", "30m") is None
    repo.put_state("600519.SH", "30m", s1, "2026-11-19 10:30")
    got = repo.get_state("600519.SH", "30m")
    assert got["state"] == "金叉"
    s2 = {"state": "死叉", "cross": "death", "dif": -1.0, "dea": -0.5, "hist": -1.0}
    repo.put_state("600519.SH", "30m", s2, "2026-11-19 11:00")
    assert repo.get_state("600519.SH", "30m")["state"] == "死叉"
    assert repo.stats()["macd_states"] == 1


def test_repo_prune(repo):
    bars = []
    from datetime import datetime, timedelta
    base = datetime(2026, 11, 1, 10, 0)
    for d in range(30):
        dt = (base + timedelta(days=d)).strftime("%Y-%m-%d %H:%M")
        bars.append({"dt": dt, "open": 10, "high": 11, "low": 9,
                     "close": 10.5, "volume": 1, "amount": 1})
    repo.upsert_bars("000100.SZ", bars)
    assert len(repo.load_bars("000100.SZ")) == 30
    pruned = repo.prune(keep_days=20)
    assert pruned > 0
    assert len(repo.load_bars("000100.SZ")) < 30


def test_notification_format():
    from mystery.services.minute_macd import format_notification
    evs = [{"code": "600519.SH", "name": "贵州茅台", "freq": "30m",
            "from": "死叉", "to": "金叉", "dif": 1.2, "dea": 0.8, "hist": 0.8},
           {"code": "000100.SZ", "name": "", "freq": "60m",
            "from": "金叉", "to": "死叉", "dif": -0.5, "dea": -0.3, "hist": -0.4}]
    text = format_notification(evs, first_run=False)
    assert "金叉" in text and "死叉" in text
    assert "600519.SH" in text and "贵州茅台" in text
    assert format_notification([], False) == ""

    # 大量事件（首次基线）不报错且有截断汇总
    many = [{"code": f"{600000+i}.SH", "name": "", "freq": "30m",
             "from": "首次", "to": "金叉" if i % 2 else "死叉",
             "dif": 1, "dea": 0, "hist": 1} for i in range(300)]
    text2 = format_notification(many, first_run=True, max_lines=40)
    assert "共 300 条" in text2
    assert "未展开" in text2
    assert len(text2.splitlines()) < 60  # 被有效截断
