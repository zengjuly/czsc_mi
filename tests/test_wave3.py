"""tests/test_wave3.py — W68 三浪主升策略扫描离线回归（纯函数，零 IO）。

全部用合成 OHLCV，不打行情、不碰生产库 / 生产自选股。
"""
from __future__ import annotations

import json
import math

import pytest

from mystery.core import wave3


# ---------------------------------------------------------------------------
# 合成 K 线
# ---------------------------------------------------------------------------

def _mk(closes, volumes, start_day=1):
    """由收盘价序列造 OHLCV dict 列表（high/low 固定 ±1%，日期顺序递增）。"""
    out = []
    prev = None
    for i, c in enumerate(closes):
        v = volumes[i] if i < len(volumes) else volumes[-1]
        out.append({
            "dt": f"2026-01-{start_day + i:02d}" if start_day + i <= 28
                  else f"2026-02-{start_day + i - 28:02d}",
            "open": prev if prev is not None else c * 0.995,
            "high": c * 1.01, "low": c * 0.99, "close": c,
            "volume": v,
        })
        prev = c
    return out


def _trend(n, start=10.0, step=0.10, vol=1000.0):
    """单调递增序列 → MA 多头排列；用于周/月线位置评估。"""
    return _mk([start + step * i for i in range(n)], [vol] * n)


def _three_wave():
    """标准三浪：低位缓升 → 一浪放量上涨 → 二浪缩量回调 → 三浪放量突破。"""
    seg0 = [10.0 + 0.002 * i for i in range(30)]        # 低位缓升
    seg1 = [10.06 + 0.097 * i for i in range(20)]       # 第一浪 → 约 12.0
    seg2 = [12.0 - 0.083 * i for i in range(12)]        # 第二浪 → 约 11.0
    seg3 = [12.30, 12.40, 12.50]                        # 第三浪突破
    closes = seg0 + seg1 + seg2 + seg3
    vols = ([800.0] * 30) + ([1000.0] * 20) + ([600.0] * 12) + ([2500.0] * 3)
    return _mk(closes, vols)


def _good_periods(daily=None):
    return (daily or _three_wave(), _trend(30), _trend(30, 10.0, 0.05))


# ---------------------------------------------------------------------------
# 1. 多周期位置
# ---------------------------------------------------------------------------

def test_period_position_bull_high_zone():
    p = wave3._period_position(_trend(30), "日线", "1d")
    assert p["数据不足"] is False
    assert p["趋势"] == "多头" and p["方向"] == 1
    assert p["站上MA20"] is True and p["站上MA60"] is None
    assert p["位置"] == "高位" and p["位置分位"] > 0.9
    # 30 根不足以回看 20 根前的 MA20 → 斜率诚实留空，不编数
    assert p["斜率%"] is None
    p60 = wave3._period_position(_trend(60), "日线", "1d")
    assert p60["斜率%"] is not None and p60["斜率%"] > 0


def test_period_position_bear_low_zone():
    p = wave3._period_position(
        _mk([15.0 - 0.1 * i for i in range(40)], [1000] * 40), "周线", "1w")
    assert p["趋势"] == "空头" and p["方向"] == -1
    assert p["位置"] == "低位"


def test_period_position_insufficient_data():
    p = wave3._period_position(_trend(10), "月线", "1M")
    assert p["数据不足"] is True and p["方向"] == 0


def test_assess_positions_all_bull():
    d, w, m = _good_periods()
    a = wave3.assess_positions(d, w, m)
    assert a["数据完整"] is True and a["有效周期数"] == 3
    # 日线处于二浪回调后的突破初期，MA10 尚未上穿 MA20 → 判「震荡」(dir=0)，
    # 但无空头且周线多头 → 铁律一仍放行；如实反映位置，不强行凑三周期同向。
    assert a["向上周期数"] == 2 and a["共振"] == "部分同向向上"
    assert a["趋势允许"] is True
    assert "月线高位" in a["位置摘要"]


def test_assess_positions_bear_month_blocks():
    d, w, _ = _good_periods()
    bear_m = _mk([20.0 - 0.1 * i for i in range(30)], [1000] * 30)
    a = wave3.assess_positions(d, w, bear_m)
    assert a["趋势允许"] is False
    assert a["共振"] == "存在逆势周期"


def test_assess_positions_missing_periods_is_conservative():
    a = wave3.assess_positions(_three_wave(), None, None)
    assert a["数据完整"] is False and a["有效周期数"] == 1
    assert a["趋势允许"] is False


# ---------------------------------------------------------------------------
# 2. 三浪结构
# ---------------------------------------------------------------------------

def test_detect_full_three_wave():
    w = wave3.detect_wave_structure(_three_wave())
    assert w["phase"] == "第三浪启动"
    assert w["wave1"]["ok"] is True
    assert w["wave2"]["structure_ok"] is True
    assert w["wave2"]["volume_shrink"] is True
    assert w["wave3"]["breakout"] is True
    assert w["wave3"]["volume_expand"] is True
    assert w["stop_valid"] is True
    assert w["chase_risk"] is False and w["exhausted"] is False
    assert w["entry_price"] > w["stop_price"] > 0


def test_detect_no_first_wave():
    flat = _mk([10.0] * 70, [1000] * 70)
    w = wave3.detect_wave_structure(flat)
    assert w["phase"] == "无第一浪结构"
    assert w["wave1"]["ok"] is False and w["stop_price"] is None


def test_detect_insufficient_bars():
    w = wave3.detect_wave_structure(_trend(30))
    assert w["phase"] == "数据不足"
    assert w["wave3"]["breakout"] is False


def test_detect_params_override_gain():
    w = wave3.detect_wave_structure(_three_wave(), {"W1_MIN_GAIN": 0.60})
    assert w["wave1"]["ok"] is False
    assert w["phase"] == "无第一浪结构"


def test_detect_wave2_breaking_start():
    seg0 = [10.0 + 0.002 * i for i in range(30)]   # 缓升，保证第一浪收阳占比达标
    seg1 = [10.06 + 0.1 * i for i in range(20)]
    seg2 = [12.0 - 0.25 * i for i in range(12)]   # 跌回 9.25，破一浪起点
    closes = seg0 + seg1 + seg2
    w = wave3.detect_wave_structure(_mk(closes, [1000] * len(closes)))
    assert w["wave1"]["ok"] is True
    assert w["wave2"]["structure_ok"] is False
    assert w["phase"] == "第二浪调整不合格"


def test_detect_wave2_no_shrink_is_soft():
    seg0 = [10.0 + 0.002 * i for i in range(30)]
    seg1 = [10.06 + 0.097 * i for i in range(20)]
    seg2 = [12.0 - 0.083 * i for i in range(12)]
    seg3 = [12.30, 12.40, 12.50]
    closes = seg0 + seg1 + seg2 + seg3
    vols = ([800] * 30) + ([600] * 20) + ([3000] * 12) + ([2500] * 3)
    w = wave3.detect_wave_structure(_mk(closes, vols))
    assert w["wave2"]["volume_shrink"] is False
    assert w["wave3"]["breakout"] is True   # 软项不否决结构本身


def test_detect_no_breakout_waits():
    seg0 = [10.0 + 0.002 * i for i in range(30)]
    seg1 = [10.06 + 0.097 * i for i in range(20)]
    seg2 = [12.0 - 0.083 * i for i in range(12)]
    tail = [11.0, 11.1, 11.0]               # 未站上 12.12
    closes = seg0 + seg1 + seg2 + tail
    vols = ([800] * 30) + ([1000] * 20) + ([600] * 12) + ([700] * 3)
    w = wave3.detect_wave_structure(_mk(closes, vols))
    assert w["phase"] == "等待突破确认"
    assert w["wave3"]["breakout"] is False
    assert w["wave2"]["stabilized"] is True


def test_detect_breakout_without_volume():
    seg0 = [10.0 + 0.002 * i for i in range(30)]
    seg1 = [10.06 + 0.097 * i for i in range(20)]
    seg2 = [12.0 - 0.083 * i for i in range(12)]
    seg3 = [12.30, 12.40, 12.50]
    closes = seg0 + seg1 + seg2 + seg3
    vols = ([800] * 30) + ([1000] * 20) + ([600] * 12) + ([700] * 3)
    w = wave3.detect_wave_structure(_mk(closes, vols))
    assert w["wave3"]["breakout"] is True
    assert w["wave3"]["volume_expand"] is False


def test_detect_chase_risk():
    seg0 = [10.0 + 0.002 * i for i in range(30)]
    seg1 = [10.06 + 0.097 * i for i in range(20)]
    seg2 = [12.0 - 0.083 * i for i in range(12)]
    seg3 = [13.50, 13.60, 13.70]           # 相对 H1≈12.12 涨超 5%
    closes = seg0 + seg1 + seg2 + seg3
    vols = ([800] * 30) + ([1000] * 20) + ([600] * 12) + ([2500] * 3)
    w = wave3.detect_wave_structure(_mk(closes, vols))
    assert w["wave3"]["breakout"] is True
    assert w["chase_risk"] is True


def test_normalize_bars_filters_invalid():
    rows = [{"dt": "2026-01-01", "open": 1, "high": 1, "low": 1,
             "close": 1, "volume": 5},
            {"dt": "2026-01-02", "open": 0, "high": 0, "low": 0,
             "close": 0, "volume": 5},
            {"dt": "2026-01-03", "open": 1, "high": 1, "low": 2,
             "close": 1, "volume": 5}]
    assert len(wave3._normalize_bars(rows)) == 1
    assert wave3._normalize_bars({"bars": rows})[0]["close"] == 1


# ---------------------------------------------------------------------------
# 3. 仓位公式
# ---------------------------------------------------------------------------

def test_position_size_formula_and_lot_rounding():
    s = wave3.position_size(1_000_000, 0.01, 12.30, 10.6722)
    assert s["valid"] is True
    assert s["risk_budget"] == 10000.0
    assert abs(s["per_share_risk"] - 1.6278) < 1e-6
    assert s["shares"] % 100 == 0 and s["shares"] > 0
    assert abs(s["risk_amount"] - s["shares"] * s["per_share_risk"]) < 0.02
    assert s["risk_amount"] <= s["risk_budget"] + 1e-6
    assert s["lots"] == s["shares"] // 100


def test_position_size_no_stop_refuses():
    s = wave3.position_size(1_000_000, 0.01, 10.0, 10.0)
    assert s["valid"] is False and s["shares"] == 0
    assert "坚决不买" in s["reason"]
    s2 = wave3.position_size(1_000_000, 0.01, 10.0, 12.0)
    assert s2["valid"] is False and s2["shares"] == 0


def test_position_size_bad_inputs():
    for eq, rp, e, st in [(0, 0.01, 12, 10), (1e6, 0, 12, 10),
                          (1e6, 1.5, 12, 10), (1e6, 0.01, None, 10),
                          (1e6, 0.01, 12, None)]:
        assert wave3.position_size(eq, rp, e, st)["valid"] is False


def test_position_size_cap_and_small_budget():
    s = wave3.position_size(100_000, 0.30, 10.0, 9.5,
                            max_position_pct=0.10)
    assert s["capped"] is True
    assert s["position_pct"] <= 10.0 + 1e-6
    tiny = wave3.position_size(1000, 0.001, 100.0, 99.0)
    assert tiny["shares"] == 0 and tiny["valid"] is False


# ---------------------------------------------------------------------------
# 4. 九条铁律 / 十步流程
# ---------------------------------------------------------------------------

def _eval(**kw):
    d, w, m = _good_periods()
    opts = dict(daily=d, weekly=w, monthly=m, equity=1_000_000,
                risk_pct=0.01, symbol="sh600519", name="贵州茅台")
    opts.update(kw)
    return wave3.evaluate(**opts)


def test_iron_rules_all_pass_and_verdict_candidate():
    a = _eval()
    assert a["rules"]["all_pass"] is True
    assert a["rules"]["failed_hard"] == []
    assert a["rules"]["rule_quality"] == 100.0
    assert a["verdict"] == "候选"
    assert a["sizing"]["valid"] is True
    assert a["disclaimer"] == wave3.DISCLAIMER
    json.dumps(a, ensure_ascii=False)      # JSON 可序列化


def test_iron_rules_soft_default_strict_upgrade():
    seg0 = [10.0 + 0.002 * i for i in range(30)]
    seg1 = [10.06 + 0.097 * i for i in range(20)]
    seg2 = [12.0 - 0.083 * i for i in range(12)]
    seg3 = [12.30, 12.40, 12.50]
    d = _mk(seg0 + seg1 + seg2 + seg3,
            ([800] * 30) + ([600] * 20) + ([3000] * 12) + ([700] * 3))
    w, m = _trend(30), _trend(30, 10.0, 0.05)
    soft = wave3.evaluate(d, w, m, equity=1_000_000, risk_pct=0.01)
    hard = wave3.evaluate(d, w, m, equity=1_000_000, risk_pct=0.01,
                          strict=True)
    assert soft["rules"]["all_pass"] is True       # 缩量/放量只是偏好
    assert soft["rules"]["soft_total"] == 2
    assert hard["rules"]["all_pass"] is False
    assert 4 in hard["rules"]["failed_hard"] and 6 in hard["rules"]["failed_hard"]
    assert len(hard["rules"]["vetoes"]) >= 2


def test_iron_rules_chase_veto():
    seg0 = [10.0 + 0.002 * i for i in range(30)]
    seg1 = [10.06 + 0.097 * i for i in range(20)]
    seg2 = [12.0 - 0.083 * i for i in range(12)]
    d = _mk(seg0 + seg1 + seg2 + [13.5, 13.6, 13.7],
            ([800] * 30) + ([1000] * 20) + ([600] * 12) + ([2500] * 3))
    a = wave3.evaluate(d, _trend(30), _trend(30), equity=1e6, risk_pct=0.01)
    assert 8 in a["rules"]["failed_hard"]
    assert a["rules"]["all_pass"] is False
    assert a["verdict"] == "观察"


def test_workflow_full_candidate_in_holding_stage():
    a = _eval()
    wf = a["workflow"]
    assert wf["stage"] == 9
    assert wf["stage_name"] == "跟随趋势持有"
    assert len(wf["steps"]) == 10
    assert all(s["status"] == "已过" for s in wf["steps"][:8])
    assert "持有" in wf["next_action"]


def test_workflow_waits_at_breakout_step():
    """二浪缩量企稳但未突破 → 停在第 6 步；衰竭门控不许跳到第 10 步。"""
    a = {
        "positions": {"趋势允许": True, "月线": {"方向": 1}},
        "wave": {
            "wave1": {"ok": True},
            "wave2": {"exists": True, "volume_shrink": True,
                      "stabilized": True},
            "wave3": {"breakout": False},
            "stop_valid": True, "exhausted": False,
        },
        "rules": {"all_pass": False},
    }
    wf = wave3.workflow(a)
    assert wf["stage"] == 6
    assert wf["stage_name"] == "等待突破确认"
    assert wf["steps"][5]["status"] == "当前"

    # 真实合成序列：回调期跌破 MA20（exhausted 原始信号 True）也不得进入止盈阶段
    seg0 = [10.0 + 0.002 * i for i in range(30)]
    seg1 = [10.06 + 0.097 * i for i in range(20)]
    seg2 = [12.0 - 0.083 * i for i in range(12)]
    d = _mk(seg0 + seg1 + seg2 + [11.0, 11.1, 11.0],
            ([800] * 30) + ([1000] * 20) + ([600] * 12) + ([700] * 3))
    real = wave3.evaluate(d, _trend(30), _trend(30), equity=1e6, risk_pct=0.01)
    assert real["workflow"]["stage"] != 10


def test_workflow_exhaustion_goes_to_step10():
    """衰竭信号优先于持有状态：直接构造 assessment 锁定状态机分支。"""
    a = {
        "positions": {"趋势允许": True, "月线": {"方向": 1}},
        "wave": {
            "wave1": {"ok": True},
            "wave2": {"exists": True, "volume_shrink": True,
                      "stabilized": True},
            "wave3": {"breakout": True},
            "stop_valid": True, "exhausted": True,
        },
        "rules": {"all_pass": True},
    }
    wf = wave3.workflow(a)
    assert wf["stage"] == 10
    assert wf["stage_name"] == "根据趋势衰竭信号分批止盈"
    assert wf["steps"][9]["status"] == "当前"
    assert all(s["status"] == "已过" for s in wf["steps"][:9])


def test_detect_exhaustion_below_ma20():
    """突破后跌破 MA20 → exhausted 独立置位（不等第三浪分支）。"""
    seg0 = [10.0 + 0.002 * i for i in range(30)]
    seg1 = [10.06 + 0.097 * i for i in range(20)]
    seg2 = [12.0 - 0.083 * i for i in range(12)]
    tail = [12.30, 12.40, 12.50, 11.20, 10.20]
    d = _mk(seg0 + seg1 + seg2 + tail,
            ([800] * 30) + ([1000] * 20) + ([600] * 12) + ([2500] * 5))
    w = wave3.detect_wave_structure(d)
    assert w["exhausted"] is True


def test_evaluate_no_data_is_avoid():
    a = wave3.evaluate(None, None, None, equity=1e6)
    assert a["verdict"] == "回避"
    assert a["sizing"]["shares"] == 0
    assert a["rules"]["all_pass"] is False


def test_texts_and_constants():
    assert len(wave3.IRON_RULES) == 9
    assert len(wave3.WORKFLOW_STEPS) == 10
    assert wave3.IRON_RULES[3][1] == "soft" and wave3.IRON_RULES[5][1] == "soft"
    assert "不代表第三浪必然出现" in wave3.DISCLAIMER
    assert "历史回测和模拟交易验证" in wave3.DISCLAIMER


# ---------------------------------------------------------------------------
# 5. 相关性与组合总风险
# ---------------------------------------------------------------------------

def test_pearson_identical_and_constant():
    xs = [0.01, -0.02, 0.03, 0.015, -0.01, 0.02] * 4
    assert wave3.pearson(xs, xs[:], min_overlap=3) == pytest.approx(1.0)
    assert wave3.pearson([1.0] * 30, xs[:30], min_overlap=3) is None
    assert wave3.pearson(xs[:2], xs[:2], min_overlap=20) is None


def test_correlation_matrix_identical_and_assumed():
    rets = {f"2026-01-{i + 1:02d}": 0.001 * (i % 7 - 3) for i in range(30)}
    cm = wave3.correlation_matrix({"A": rets, "B": dict(rets)})
    assert cm["symbols"] == ["A", "B"]
    assert cm["matrix"]["A"]["B"] == pytest.approx(1.0)
    assert cm["assumed_pairs"] == []
    short = wave3.correlation_matrix({"A": rets, "B": {"2026-02-01": 0.01}})
    assert short["matrix"]["A"]["B"] == 0.0
    assert short["assumed_pairs"] == [["A", "B"]]


def test_portfolio_risk_uncorrelated():
    r = wave3.portfolio_risk({"A": 1000, "B": 1000}, equity=100_000)
    assert r["portfolio_risk"] == pytest.approx(math.sqrt(2_000_000), abs=0.02)
    assert r["sum_risk"] == 2000
    assert r["diversification_ratio"] < 1


def test_portfolio_risk_perfect_corr_equals_sum():
    corr = {"matrix": {"A": {"A": 1.0, "B": 1.0},
                       "B": {"A": 1.0, "B": 1.0}}}
    r = wave3.portfolio_risk({"A": 1000, "B": 1000}, corr, equity=100_000)
    assert r["portfolio_risk"] == 2000.0
    assert r["diversification_ratio"] == 1.0
    assert len(r["high_corr_pairs"]) == 1


def test_portfolio_risk_high_corr_triggers_scale_down():
    corr = {"matrix": {a: {b: 0.9 for b in "ABC"} for a in "ABC"}}
    for a in "ABC":
        corr["matrix"][a][a] = 1.0
    r = wave3.portfolio_risk({"A": 5000, "B": 5000, "C": 5000}, corr,
                             equity=100_000, max_total_risk_pct=0.005)
    assert r["portfolio_risk"] > r["sum_risk"] * 0.9
    assert len(r["high_corr_pairs"]) == 3
    assert r["scale_down"] < 1.0
    assert "需下调总仓位" in r["note"]


def test_portfolio_risk_empty_and_negative_matrix():
    e = wave3.portfolio_risk({})
    assert e["portfolio_risk"] == 0.0 and e["scale_down"] == 1.0
    # 越界 rho 会被 clamp 到 [-1,1]：两票 -2 → -1，合成风险归零（合法边界）
    clamped = {"matrix": {"A": {"A": 1.0, "B": -2.0},
                          "B": {"A": -2.0, "B": 1.0}}}
    c = wave3.portfolio_risk({"A": 1000, "B": 1000}, clamped)
    assert c["portfolio_risk"] == 0.0 and c["matrix_psd"] is True
    # 三票两两完全负相关在数学上不可能 → 非半正定，合成风险截断为 0
    bad = {"matrix": {a: {b: (1.0 if a == b else -1.0) for b in "ABC"}
                      for a in "ABC"}}
    r = wave3.portfolio_risk({"A": 1000, "B": 1000, "C": 1000}, bad)
    assert r["matrix_psd"] is False
    assert r["portfolio_risk"] == 0.0
    assert "非半正定" in r["note"]


# ---------------------------------------------------------------------------
# 6. 组合层编排
# ---------------------------------------------------------------------------

def test_evaluate_portfolio_scales_down_positions():
    d, w, m = _good_periods()
    items = [
        {"symbol": "A", "name": "甲", "daily": d, "weekly": w, "monthly": m},
        {"symbol": "B", "name": "乙", "daily": d, "weekly": w, "monthly": m},
    ]
    rets = {"A": {f"d{i}": 0.001 * (i % 5) for i in range(30)},
            "B": {f"d{i}": 0.001 * (i % 5) for i in range(30)}}
    out = wave3.evaluate_portfolio(
        items, equity=100_000, risk_pct=0.01,
        returns_by_symbol=rets, max_total_risk_pct=0.005)
    assert out["scanned"] == 2 and out["candidates"] == 2
    assert out["correlation"]["matrix"]["A"]["B"] == pytest.approx(1.0)
    assert out["portfolio"]["scale_down"] < 1.0
    for r in out["results"]:
        s = r["sizing"]
        assert s["suggested_shares"] <= s["shares"]
        assert s["suggested_shares"] % 100 == 0
        assert s["suggested_risk_amount"] <= s["risk_amount"]
    assert out["disclaimer"] == wave3.DISCLAIMER
    json.dumps(out, ensure_ascii=False)


def test_evaluate_portfolio_empty():
    out = wave3.evaluate_portfolio([], equity=100_000)
    assert out["scanned"] == 0 and out["candidates"] == 0
    assert out["portfolio"]["portfolio_risk"] == 0.0
    assert out["results"] == []


# ---------------------------------------------------------------------------
# 7. 服务层：只读与 helpers（不碰生产库 / 生产自选股）
# ---------------------------------------------------------------------------

def test_service_returns_and_num_helpers():
    from mystery.services import wave3_scan as svc
    assert svc._num(float("nan")) is None
    assert svc._num("12.5") == 12.5
    recs = _mk([10.0, 11.0, 10.5], [100, 200, 300])
    r = svc._returns(recs)
    assert r[recs[1]["dt"]] == pytest.approx(0.1)
    assert r[recs[2]["dt"]] < 0
    assert svc._records(None) == []


def test_service_is_readonly_no_watchlist_writes():
    """W65 教训：扫描服务不得调用任何写库 / 写自选股 API（AST 级，避开 docstring）。"""
    import ast
    import inspect
    from mystery.services import wave3_scan as svc
    tree = ast.parse(inspect.getsource(svc))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
            {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    # 名称拼接：避免本文件被 W65 的「写 API 字面量」静态扫描误判为调用方
    forbidden = ("save_" + "watchlist", "sync_tdx" + "_boards",
                 "_write_scan_batch", "upsert_kline", "upsert_kline_many",
                 "commit", "set_turn", "invalidate_analysis")
    for api in forbidden:
        assert api not in names, f"只读服务不应调用 {api}"
    assert "_daily_from_db" in names and "resample_bars" in names
    assert "load_watchlist" in names
