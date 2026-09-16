"""W37 大盘滤网回归（纯展示，零规则改动）：
1. anchor_env：数据不足 → 全 None（诚实 unknown）；数据足 → 数值正确。
2. market_env：三态判定 + 缺锚 → 大盘未知。
3. market_env_line：文本摘要不含 None/异常。
4. market_env_summary：用假 market 验证走 fetch_index 通道且容错。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mystery.core.market_env import anchor_env, market_env
from mystery.services.analyze import market_env_line, market_env_summary


def _closes(n=300, base=100.0, slope=0.1):
    return [base + i * slope for i in range(n)]


# ---------- 1. anchor_env ----------

def test_anchor_env_insufficient_data_is_unknown():
    a = anchor_env("上证指数", "000001.SH", [])
    assert a["above"] is None and a["aligned"] is None and a["close"] is None
    b = anchor_env("上证指数", "000001.SH", _closes(249))  # 差 1 根
    assert b["close"] is None and b["ma250"] is None


def test_anchor_env_uptrend():
    a = anchor_env("上证指数", "000001.SH", _closes(300))
    assert a["close"] is not None
    assert a["above"] is True and a["aligned"] is True
    assert a["chg_20d"] > 0


def test_anchor_env_broken_year_line():
    closes = _closes(299) + [1.0]        # 末根砸穿年线
    a = anchor_env("X", "000001.SH", closes)
    assert a["above"] is False and a["aligned"] is False


# ---------- 2. market_env 三态 ----------

def _anchor(above, aligned):
    return {"name": "x", "code": "c", "close": 1.0, "ma250": 1.0,
            "above": above, "aligned": aligned, "chg_20d": 0.0, "date": ""}


def test_market_env_states():
    assert "多头" in market_env([_anchor(True, True),
                                 _anchor(True, True)])["verdict"]
    assert "警示" in market_env([_anchor(True, False),
                                 _anchor(False, False)])["verdict"]
    assert "震荡" in market_env([_anchor(True, False),
                                _anchor(True, False)])["verdict"]
    assert "未知" in market_env([_anchor(True, True),
                                _anchor(None, None)])["verdict"]


# ---------- 3/4. 摘要与通道 ----------

def test_line_handles_unknown():
    env = market_env([_anchor(None, None)])
    line = market_env_line(env)
    assert "大盘滤网" in line and "None" not in line


class _Bar:
    def __init__(self, close, dt="2026-09-16"):
        self.close = close
        self.dt = dt


class _Series:
    def __init__(self, closes):
        self.bars = [_Bar(c) for c in closes]


class _FakeMarket:
    def __init__(self, first_ok=True):
        self.calls = []
        self.first_ok = first_ok

    def fetch_index(self, code, freq):
        self.calls.append((code, freq))
        if not self.first_ok and len(self.calls) == 2:
            raise RuntimeError("源挂了")
        return _Series(_closes(300))


def test_summary_goes_through_fetch_index_and_tolerates_error():
    mkt = _FakeMarket(first_ok=True)
    env = market_env_summary(mkt)
    assert [c for c, _ in mkt.calls] == ["000001.SH", "399311.SZ"]
    assert all(f == "1d" for _, f in mkt.calls)
    assert len(env["anchors"]) == 2
    assert env["anchors"][0]["date"] == "2026-09-16"
    # 第二锚抛异常 → 该锚缺数据 → 整体「大盘未知」，不外抛
    mkt2 = _FakeMarket(first_ok=False)
    env2 = market_env_summary(mkt2)
    assert "未知" in env2["verdict"]
    assert env2["anchors"][1]["close"] is None
