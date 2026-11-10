"""W83 回归：多方炮三种重点变体 + 蓄势待确认态 + 「重点观察」结论展示。

全部合成数据 + monkeypatch，零网络、零生产库/自选股改动。
用户点名的三个验证点必须落成**量化结论文本**（不只是形态名）：
- 跳空多方炮 → 突破缺口是否得到支撑；
- 两阳夹两阴 → 调整期间是否缩量；
- 叠叠多方炮 → 每次回调低点是否抬高；
- 一阳后连续两天缩量震荡小阴小阳 → 下周一能否放量阳线确认。
"""
from __future__ import annotations

import io
import sys
from collections import namedtuple
from pathlib import Path

import openpyxl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mystery.core import candlestick_patterns as cp  # noqa: E402
from mystery.services import runtime_settings as rts  # noqa: E402
from mystery.services.candlestick_scan import analyze_candlestick  # noqa: E402
from mystery.apps.reports import result_fields as rf  # noqa: E402
from mystery.apps.reports.excel_report import excel_bytes  # noqa: E402
from mystery.apps.reports.html_report import write_html  # noqa: E402

Bar = namedtuple("Bar", "dt open high low close volume")
_ROOT = Path(__file__).resolve().parents[1]


def bar(day, o, h, l, c, v=1000):
    return Bar(f"2026-01-{day:02d}", o, h, l, c, v)


def base(n=35, start=10.0, slope=0.02, vol=1000):
    out = []
    for i in range(n):
        o = start + slope * i
        c = o + 0.05
        out.append(bar(i + 1, o, c + 0.08, o - 0.08, c, vol))
    return out


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    """覆盖层隔离到临时文件，清空 W82/W83 env（不污染生产 data/）。"""
    monkeypatch.setattr(rts, "CANDLE_OVERRIDE_PATH",
                        tmp_path / "candlestick_override.json")
    for k in rts.CANDLE_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)


def _opts(**kw):
    o = {"enabled": True, "daily": True, "weekly": True,
         "lookback": 60, "patterns": list(cp.PATTERN_CODES)}
    o.update(kw)
    return o


def _hit(bars, code, **kw):
    rep = cp.detect_combo_patterns(bars, enabled=[code], **kw)
    got = [h for h in rep["hits"] if h["code"] == code]
    return got[0] if got else None


# ---------------------------------------------------------------- 形态命中
GAP_BARS = base(35) + [
    bar(36, 10.70, 10.90, 10.62, 10.80, 1000),   # 突破前基准 high=10.90
    bar(37, 11.00, 11.40, 10.95, 11.35, 1800),   # 跳空首阳 low=10.95 > 10.90
    bar(38, 11.30, 11.38, 10.96, 11.10, 900),    # 阴线回踩不补缺口
    bar(39, 11.12, 11.60, 11.05, 11.55, 2000),   # 再阳创新高
    bar(40, 11.50, 11.70, 11.20, 11.60, 1500),   # 后续守住缺口
    bar(41, 11.58, 11.80, 11.30, 11.72, 1400),
]

LIANGYANG_BARS = base(35) + [
    bar(36, 10.70, 11.20, 10.65, 11.15, 2000),   # 首阳
    bar(37, 11.10, 11.15, 10.80, 10.85, 700),    # 缩量阴 1
    bar(38, 10.88, 10.95, 10.75, 10.80, 600),    # 缩量阴 2
    bar(39, 10.82, 11.40, 10.78, 11.35, 2200),   # 末阳收复创新高
]

DIEDIE_BARS = base(35) + [
    bar(36, 10.50, 10.95, 10.45, 10.90, 1500),   # 阳 1
    bar(37, 10.88, 10.92, 10.60, 10.65, 800),    # 回调 1 low=10.60
    bar(38, 10.68, 11.20, 10.62, 11.15, 1600),   # 阳 2
    bar(39, 11.12, 11.18, 10.85, 10.92, 750),    # 回调 2 low=10.85（抬高）
    bar(40, 10.95, 11.50, 10.90, 11.45, 1800),   # 阳 3
]

PENDING_BARS = base(35) + [
    bar(36, 10.70, 11.10, 10.65, 11.05, 2000),   # 放量中阳
    bar(37, 11.00, 11.06, 10.88, 10.95, 700),    # 缩量小阴
    bar(38, 10.96, 11.04, 10.90, 11.00, 600),    # 缩量小阳（最新一根）
]

_W83_CASES = [
    ("gap_duofangpao", GAP_BARS, "突破缺口"),
    ("liangyang_liangyin", LIANGYANG_BARS, "调整期"),
    ("diedie_duofangpao", DIEDIE_BARS, "回调低点"),
    ("duofangpao_pending", PENDING_BARS, "下周一"),
]


def test_registry_has_4_duofangpao_variants():
    for code in cp.PATTERN_GROUPS["多方炮变体"]:
        assert code in cp.PATTERN_CODES
        assert cp.PATTERN_SPECS[code]["group"] == "多方炮变体"
    assert cp.PATTERN_SPECS["gap_duofangpao"]["name"] == "跳空多方炮"
    assert cp.PATTERN_SPECS["liangyang_liangyin"]["name"] == "两阳夹两阴"
    assert cp.PATTERN_SPECS["diedie_duofangpao"]["name"] == "叠叠多方炮"
    assert cp.PATTERN_SPECS["duofangpao_pending"]["direction"] == "中性"
    # 基础 10 + W83 4
    assert len(cp.PATTERN_CODES) == 14


@pytest.mark.parametrize("code,bars,kw", _W83_CASES,
                         ids=[c[0] for c in _W83_CASES])
def test_each_variant_detected_with_focus_conclusion(code, bars, kw):
    h = _hit(bars, code)
    assert h is not None, f"{code} 未命中"
    det = h["detail"]
    assert kw in det["重点观察"], det
    assert set(h) >= {"code", "name", "direction", "date", "detail"}


def test_gap_support_verdict_held_and_filled():
    held = _hit(GAP_BARS, "gap_duofangpao")
    assert held["detail"]["缺口支撑"] == "守住"
    assert "得到支撑" in held["detail"]["重点观察"]
    assert held["detail"]["后续根数"] == 2
    # 后续跌破缺口下沿 → 结论翻转为「已回补，支撑失效」
    filled = GAP_BARS + [bar(42, 11.60, 11.65, 10.50, 10.60, 3000)]
    h = _hit(filled, "gap_duofangpao")
    assert h["detail"]["缺口支撑"] == "回补"
    assert "已回补" in h["detail"]["重点观察"] and "支撑失效" in h["detail"]["重点观察"]


def test_liangyang_liangyin_shrunk_and_not_shrunk():
    h = _hit(LIANGYANG_BARS, "liangyang_liangyin")
    assert h["detail"]["调整缩量"] == "是"
    assert "缩量" in h["detail"]["重点观察"]
    # 把两根阴线改成放量 → 结论「未缩量，抛压仍存需警惕」
    noisy = LIANGYANG_BARS[:-2] + [
        bar(38, 10.88, 10.95, 10.75, 10.80, 2600),
        bar(39, 10.82, 11.40, 10.78, 11.35, 2200)]
    # 用完整四根替换（保留首阳，两阴放量）
    noisy = base(35) + [
        bar(36, 10.70, 11.20, 10.65, 11.15, 2000),
        bar(37, 11.10, 11.15, 10.80, 10.85, 2400),
        bar(38, 10.88, 10.95, 10.75, 10.80, 2600),
        bar(39, 10.82, 11.40, 10.78, 11.35, 2200)]
    h2 = _hit(noisy, "liangyang_liangyin")
    assert h2["detail"]["调整缩量"] == "否"
    assert "未缩量" in h2["detail"]["重点观察"]


def test_diedie_lows_rising_and_not_rising():
    h = _hit(DIEDIE_BARS, "diedie_duofangpao")
    assert h["detail"]["回调低点抬高"] == "是"
    assert "逐级抬高" in h["detail"]["重点观察"]
    assert h["detail"]["叠加组数"] >= 2
    # 第二次回调低点下移 → 结论「未逐级抬高」
    weak = base(35) + [
        bar(36, 10.50, 10.95, 10.45, 10.90, 1500),
        bar(37, 10.88, 10.92, 10.60, 10.65, 800),
        bar(38, 10.68, 11.20, 10.62, 11.15, 1600),
        bar(39, 11.12, 11.18, 10.30, 10.92, 750),   # low 10.30 < 10.60
        bar(40, 10.95, 11.50, 10.90, 11.45, 1800)]
    h2 = _hit(weak, "diedie_duofangpao")
    assert h2 is not None
    assert h2["detail"]["回调低点抬高"] == "否"
    assert "未逐级抬高" in h2["detail"]["重点观察"]


def test_pending_only_reports_on_latest_bar():
    h = _hit(PENDING_BARS, "duofangpao_pending")
    assert h["detail"]["状态"] == "待确认"
    assert h["detail"]["确认位"] > h["detail"]["失守位"]
    assert "确认多方炮" in h["detail"]["重点观察"]
    assert "蓄势失败" in h["detail"]["重点观察"]
    # 后面再接一根 K 线 → 原位置不再是最新一根，不再报待确认
    extended = PENDING_BARS + [bar(39, 11.00, 11.10, 10.95, 11.05, 700)]
    assert _hit(extended, "duofangpao_pending") is None or \
        all(x["date"] != h["date"] for x in
            cp.detect_combo_patterns(extended, enabled=["duofangpao_pending"])["hits"])


def test_format_focus_dedup_and_empty():
    rep = cp.detect_combo_patterns(GAP_BARS)
    assert rep["focus"] != "—" and "突破缺口" in rep["focus"]
    assert cp.format_focus([]) == "—"
    assert cp.format_focus([{"code": "x", "name": "X", "detail": {}}]) == "—"
    assert cp.detect_combo_patterns([])["focus"] == "—"


# ---------------------------------------------------------------- service
def test_service_exposes_focus_fields_and_is_json_serializable():
    import json
    res = analyze_candlestick(GAP_BARS, LIANGYANG_BARS, options=_opts())
    assert "突破缺口" in res["focus_daily"]
    assert "调整期" in res["focus_weekly"]
    assert res["focus"] == res["focus_daily"]
    assert res["n_hits"] == res["daily"]["n_hits"] + res["weekly"]["n_hits"]
    json.dumps(res, ensure_ascii=False)   # 不抛即通过
    off = analyze_candlestick(GAP_BARS, [], options=_opts(enabled=False))
    assert off["focus"] == "—" and off["focus_daily"] == "—"


def test_focus_only_patterns_via_config():
    codes = cp.PATTERN_GROUPS["多方炮变体"]
    rep = cp.detect_combo_patterns(GAP_BARS, enabled=codes)
    assert rep["enabled"] == codes
    assert all(h["code"] in codes for h in rep["hits"])
    # 缓存 tag 随勾选口径变化（W82 同款护栏，改配置必 miss）
    t0 = rts.candle_cache_tag()
    rts.save_candle_override({"MYSTERY_CANDLE_PATTERNS": codes})
    assert rts.candle_cache_tag() != t0


# ---------------------------------------------------------------- 报告层
def _mk_result(with_cs=True):
    d = {
        "symbol": "sh600519", "name": "贵州茅台", "trade_date": "2026-11-09",
        "score": 70.0, "advice": "持有", "true_resonance": True, "price": 11.0,
        "mystery": {"vap_atr": {}, "platform": {}, "main_wave": {},
                    "resonance": {}, "checklist8": {}, "signal": {},
                    "technical": {}, "patterns": {}},
        "financial": {}, "sector": {"行业名称": "测试"}, "chan": {},
        "czsc_ver": "",
    }
    if with_cs:
        d["candlestick"] = analyze_candlestick(
            GAP_BARS, LIANGYANG_BARS, options=_opts())
    return d


def test_result_fields_focus_text_reads_payload_only():
    d = _mk_result()
    assert "突破缺口" in rf.candlestick_focus_text(d)
    assert "调整期" in rf.candlestick_focus_text(d, "weekly")
    assert rf.candlestick_focus_text(_mk_result(False)) == "—"
    # 旧 payload（无 focus* 顶层字段）→ 从 hits 明细回退，不报错
    legacy = _mk_result()
    legacy["candlestick"].pop("focus", None)
    legacy["candlestick"].pop("focus_daily", None)
    assert "突破缺口" in rf.candlestick_focus_text(legacy)


def test_excel_has_focus_column_and_detail_rows():
    wb = openpyxl.load_workbook(io.BytesIO(excel_bytes([_mk_result()])))
    ws = wb["汇总报告"]
    headers = [c.value for c in ws[1]]
    assert "形态重点观察" in headers
    vals = dict(zip(headers, [c.value for c in ws[2]]))
    assert "突破缺口" in str(vals["形态重点观察"])
    assert any(str(h).startswith("日K观察") for h in headers)
    assert any(str(h).startswith("周K观察") for h in headers)


def test_html_has_focus_block(tmp_path):
    p = write_html([_mk_result()], str(tmp_path / "r.html"))
    html = Path(p).read_text(encoding="utf-8")
    assert "多方炮重点观察" in html
    assert "突破缺口" in html
    assert "调整期" in html


def test_web_views_reference_focus_and_group():
    scan_src = (_ROOT / "mystery/apps/web/views/scan.py").read_text(encoding="utf-8")
    stock_src = (_ROOT / "mystery/apps/web/views/stock.py").read_text(encoding="utf-8")
    sys_src = (_ROOT / "mystery/apps/web/views/system.py").read_text(encoding="utf-8")
    assert "形态重点观察" in scan_src
    assert "重点观察" in stock_src
    assert "PATTERN_GROUPS" in sys_src and "10 个形态" not in sys_src


def test_no_scoring_or_rule_ver_change():
    """W83 只加展示字段：core 与 service 不引用评分/RULE_VER。

    只检查真实赋值与模块引用（docstring 里「不改 RULE_VER」的说明不算）。
    """
    core = (_ROOT / "mystery/core/candlestick_patterns.py").read_text(encoding="utf-8")
    svc = (_ROOT / "mystery/services/candlestick_scan.py").read_text(encoding="utf-8")
    src = core + svc
    assert not any(k in src for k in
                   ("RULE_VER =", "RULE_VER=", "RULE_VER:", "RULE_VER +="))
    assert not any(k in src for k in
                   ("from mystery.core.scorer", "Scorer(", "score +=", "score ="))
    assert "score" not in core
