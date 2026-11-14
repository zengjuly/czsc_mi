"""W95/①：「大盘非高位」门闩可配置化（默认 hard，生产零回归）专项。

背景（W94 交付后的待拍板项 1）：1/14 是「高位」，被 W71 高位追涨风控
一票否决。本轮按用户「执行 1 2 3 4」把该门闩做成三态可配置：

  hard = 一票否决 + 高位 -15 软惩罚（W71/W72 逐字段不变，生产默认）
  soft = 保留 -15 惩罚，但不再作为 all(gates) 的否决门闩
  off  = 移除 -15 惩罚，且不再作为否决门闩

核心约束：core 默认值与生产默认都写死 hard，任何未显式开启的开关都
不改变既有 true_resonance / 综合评分 / RULE_VER；放宽只能经 service 层
env 或网页覆盖层显式注入。

本测试覆盖：
- resolve_resonance_options 三态与非法值保守回退；
- calculate_resonance_score 三态下的 score / gates / is_true / reject；
- 非高位截面上三态完全等价（零回归的最强保证）；
- B 诊断面板 row_gates / effective_options / force_hard 与 core 严格一致；
- 旧 job payload 无「高位风控」字段时回退 hard；
- diagnose 反事实对拍（hard/soft/off 三态真三振数，等价推导）；
- service 层 env / 面板字段 / 生产默认接线不漏；
- diag_sheet_rows 新增口径行与反事实行。
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from mystery.core import resonance as R
from mystery.apps.reports import resonance_diag as D


def _kwargs(position="高位"):
    """构造「个股+大盘+行业+资金四维全过、大盘高位」的评分入参。

    基准 score = 30 + 25 + 25 + 20 = 100；高位 -15 → 85。
    阈值取 85（core 库级默认），三态下刚好能区分。
    """
    return dict(
        individual_result={"基础过滤": True, "均线多头": True},
        market_result={"趋势方向": "向上", "position": position},
        industry_result={"整体趋势": "向上"},
        capital_result={"active": True, "score": 20.0, "detail": ""},
        true_threshold=85.0,
    )


def _row(*, pos="高位", gate="hard", score=85.0, thr=80.0, true=False,
         capital=True, mkt=True, ind=True, stock=True):
    return {
        "true_resonance": true,
        "mystery": {"resonance": {
            "共振评分": score,
            "共振口径": {"真三振阈值": thr, "大盘口径": "yearline",
                         "高位风控": gate},
            "资金活跃": capital, "大盘趋势": mkt, "行业趋势": ind,
            "个股严格腿": stock, "大盘位置": pos,
        }},
    }


# ---------------------------------------------------------------------------
# resolve_resonance_options
# ---------------------------------------------------------------------------
def test_default_gate_is_hard():
    assert R.resolve_resonance_options()["high_position_gate"] == "hard"
    assert R.DEFAULT_RESONANCE_OPTIONS["high_position_gate"] == "hard"


@pytest.mark.parametrize("v,exp", [
    ("soft", "soft"), ("off", "off"), ("hard", "hard"),
    ("SOFT", "soft"), (" Off ", "off"),
    ("bogus", "hard"), (None, "hard"), (123, "hard"), ("", "hard"),
])
def test_resolve_gate_values(v, exp):
    assert R.resolve_resonance_options(
        {"high_position_gate": v})["high_position_gate"] == exp


# ---------------------------------------------------------------------------
# core：三态行为
# ---------------------------------------------------------------------------
def test_hard_vetoes_high_position():
    r = R.calculate_resonance_score(**_kwargs())
    assert r["score"] == 85.0
    assert r["gates"]["大盘非高位"] is False
    assert r["is_true_three_strike"] is False
    assert "大盘非高位" in r["reject_reasons"]
    assert r["high_position_gate"] == "hard"


def test_soft_keeps_penalty_but_not_veto():
    r = R.calculate_resonance_score(**_kwargs(), high_position_gate="soft")
    assert r["score"] == 85.0                      # -15 软惩罚保留
    assert r["gates"]["大盘非高位"] is True         # 不再否决
    assert r["is_true_three_strike"] is True
    assert "大盘非高位" not in r["reject_reasons"]
    assert r["high_position_gate"] == "soft"


def test_off_removes_penalty_and_veto():
    r = R.calculate_resonance_score(**_kwargs(), high_position_gate="off")
    assert r["score"] == 100.0                     # 无 -15
    assert r["gates"]["大盘非高位"] is True
    assert r["is_true_three_strike"] is True
    assert r["high_position_gate"] == "off"


def test_invalid_gate_falls_back_to_hard():
    r = R.calculate_resonance_score(**_kwargs(), high_position_gate="BOGUS")
    assert r["score"] == 85.0
    assert r["is_true_three_strike"] is False
    assert r["high_position_gate"] == "hard"


def test_non_high_position_identical_across_modes():
    """非高位截面上三态逐字段一致 —— 默认零回归的最强保证。"""
    base = R.calculate_resonance_score(**_kwargs(position="中位"))
    assert base["is_true_three_strike"] is True
    for m in ("soft", "off"):
        r = R.calculate_resonance_score(**_kwargs(position="中位"),
                                        high_position_gate=m)
        assert r["score"] == base["score"]
        assert r["gates"] == base["gates"]
        assert r["is_true_three_strike"] == base["is_true_three_strike"]
        assert r["reject_reasons"] == base["reject_reasons"]


def test_six_gate_keys_stable_in_all_modes():
    """六门闩键集合恒定（B 面板结构不随口径漂移）。"""
    base = set(R.calculate_resonance_score(**_kwargs())["gates"])
    for m in ("soft", "off"):
        assert set(R.calculate_resonance_score(
            **_kwargs(), high_position_gate=m)["gates"]) == base
    assert len(base) == 6


# ---------------------------------------------------------------------------
# B 诊断面板：与 core 严格一致
# ---------------------------------------------------------------------------
def test_row_gates_follows_payload_gate():
    assert D.row_gates(_row(gate="hard"))["market_not_high"] is False
    assert D.row_gates(_row(gate="soft"))["market_not_high"] is True
    assert D.row_gates(_row(gate="off"))["market_not_high"] is True


def test_force_hard_ignores_relaxed_payload():
    assert D.row_gates(_row(gate="soft"),
                       force_hard=True)["market_not_high"] is False


def test_missing_field_stays_unknown():
    r = _row()
    r["mystery"]["resonance"]["大盘位置"] = None
    assert D.row_gates(r)["market_not_high"] is None
    # 放宽态下即便位置未知，门闩也恒 True（core 语义）
    r["mystery"]["resonance"]["共振口径"]["高位风控"] = "off"
    assert D.row_gates(r)["market_not_high"] is True


def test_old_payload_falls_back_to_hard():
    r = _row()
    del r["mystery"]["resonance"]["共振口径"]["高位风控"]
    assert D.effective_options([r])["high_position_gate"] == "hard"
    assert D.row_gates(r)["market_not_high"] is False


def test_effective_options_reads_gate():
    assert D.effective_options([_row(gate="off")])["high_position_gate"] == "off"


def test_diagnose_marks_relaxed_gate():
    diag = D.diagnose([_row(gate="soft")])
    g = {x["key"]: x for x in diag["gates"]}["market_not_high"]
    assert g["relaxed"] is True
    assert "已放宽" in g["why"]
    assert "W95" in diag["conclusion"]


def test_diagnose_hard_gate_not_marked():
    g = {x["key"]: x for x in D.diagnose([_row()])["gates"]}["market_not_high"]
    assert g["relaxed"] is False
    assert "已放宽" not in g["why"]


# ---------------------------------------------------------------------------
# 反事实对拍：hard / soft / off
# ---------------------------------------------------------------------------
def test_counterfactual_high_position_rows():
    rows = [_row(gate="hard", true=False) for _ in range(3)]
    cf = D.diagnose(rows)["counterfactual"]
    assert cf["hard_true"] == 0
    assert cf["soft_true"] == 3
    assert cf["off_true"] == 3


def test_counterfactual_off_can_pass_score_gate():
    # 70 分、阈值 80：hard/soft 都卡评分门闩；off 去掉 -15 → 85 达标
    cf = D.diagnose([_row(score=70.0, thr=80.0)])["counterfactual"]
    assert cf["hard_true"] == 0
    assert cf["soft_true"] == 0
    assert cf["off_true"] == 1


def test_counterfactual_non_high_identical():
    cf = D.diagnose([_row(pos="中位", true=True)])["counterfactual"]
    assert cf["hard_true"] == 1
    assert cf["soft_true"] == 1
    assert cf["off_true"] == 1


def test_counterfactual_hard_matches_actual_when_hard():
    rows = [_row(gate="hard", true=False) for _ in range(3)]
    diag = D.diagnose(rows)
    assert diag["counterfactual"]["hard_true"] == diag["n_true"]


# ---------------------------------------------------------------------------
# service 层接线 / 展示行
# ---------------------------------------------------------------------------
def test_service_default_and_wiring(monkeypatch):
    from mystery.services import analyze as A
    assert A.PRODUCTION_RESONANCE_OPTIONS.get("high_position_gate") == "hard"
    assert any(f[0] == "high_position_gate" for f in A.RESONANCE_FIELDS)
    assert any(env == "MYSTERY_RESONANCE_HIGH_POSITION_GATE"
               for env, _ in A._RESONANCE_ENV_KEYS)
    monkeypatch.setattr(A, "load_resonance_override", lambda: {})
    monkeypatch.setattr(A, "resonance_options_from_env", lambda: {})
    assert A.resonance_options_effective().get("high_position_gate") == "hard"


def test_service_env_gate_is_picked_up(monkeypatch):
    from mystery.services import analyze as A
    monkeypatch.setenv("MYSTERY_RESONANCE_HIGH_POSITION_GATE", "soft")
    monkeypatch.setattr(A, "load_resonance_override", lambda: {})
    eff = A.resonance_options_effective()
    assert eff.get("high_position_gate") == "soft"
    assert R.resolve_resonance_options(eff)["high_position_gate"] == "soft"
    monkeypatch.delenv("MYSTERY_RESONANCE_HIGH_POSITION_GATE")


def test_diag_sheet_has_gate_and_counterfactual_rows():
    rows = D.diag_sheet_rows(D.diagnose([_row()]))
    flat = [str(r[0]) if r else "" for r in rows]
    assert "大盘非高位风控" in flat
    assert "若放宽为 soft 的真三振数" in flat
    assert "若放宽为 off 的真三振数" in flat
