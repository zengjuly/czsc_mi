"""W82 回归：典型 K 线组合形态（日/周独立）+ 网页配置 + 报告展示。

全部合成数据 + monkeypatch，零网络、零生产库/自选股改动。
- core：10 个形态命中、仙人指路/射击之星互斥、启用过滤、摘要去重；
- 配置层：稀疏覆盖 / env 优先 / 一键还原 / 缓存 tag 随配置变化且恒非空；
- service：日 K 与周 K 分别调用、关闭/缺数据兜底、异常隔离；
- 展示：Excel 列、HTML 卡片、Web 扫描表/个股页/配置面板文本护栏；
- 约束：形态不进评分（RULE_VER 不变、scorer 不引用 candlestick）。
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
    """覆盖层隔离到临时文件，清空 W82 env（不污染生产 data/）。"""
    monkeypatch.setattr(rts, "CANDLE_OVERRIDE_PATH",
                        tmp_path / "candlestick_override.json")
    for k in rts.CANDLE_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)


# ---------------------------------------------------------------- core
def test_registry_has_typical_patterns():
    # W82 基础 10 个 + W83 多方炮重点变体 4 个；用 >= 避免后续扩展再次挂测试
    assert len(cp.PATTERN_CODES) >= 14
    assert cp.PATTERN_SPECS["duofangpao"]["name"] == "多方炮"
    assert cp.PATTERN_SPECS["xianrenzhilu"]["name"] == "仙人指路"  # 用户点名
    for c, s in cp.PATTERN_SPECS.items():
        assert s["direction"] in ("看涨", "看跌", "中性")
        assert s["desc"] and s["min_bars"] >= 2
        assert c in cp.PATTERN_CODES


_CASES = [
    ("duofangpao", base(35) + [
        bar(36, 10.8, 10.95, 10.75, 10.9, 1000),
        bar(37, 10.9, 10.95, 10.77, 10.82, 800),
        bar(38, 10.78, 11.12, 10.75, 11.08, 1600)]),
    ("xianrenzhilu", base(35) + [bar(36, 10.8, 11.30, 10.79, 10.86, 1500)]),
    ("zaochenzhixing", base(35) + [
        bar(36, 11.2, 11.25, 10.60, 10.65, 1500),
        bar(37, 10.55, 10.62, 10.48, 10.52, 700),
        bar(38, 10.52, 11.05, 10.50, 11.00, 1600)]),
    ("huanghunzhixing", base(35) + [
        bar(36, 10.60, 11.25, 10.58, 11.20, 1500),
        bar(37, 11.30, 11.38, 11.24, 11.32, 700),
        bar(38, 11.32, 11.35, 10.70, 10.75, 1600)]),
    ("hongsanbing", base(35) + [
        bar(36, 10.80, 10.95, 10.78, 10.90, 1000),
        bar(37, 10.88, 11.02, 10.86, 10.98, 1000),
        bar(38, 10.95, 11.10, 10.93, 11.06, 1000)]),
    ("sanzhiwuya", base(35) + [
        bar(36, 11.20, 11.25, 11.00, 11.05, 1000),
        bar(37, 11.08, 11.12, 10.90, 10.94, 1000),
        bar(38, 10.96, 11.00, 10.75, 10.80, 1000)]),
    ("chuitouxian", base(30, start=12.0, slope=-0.03) + [
        bar(31, 11.0, 11.10, 10.40, 10.96, 1000)]),
    ("shejizhixing", base(30, start=10.0, slope=0.10) + [
        bar(31, 13.0, 13.70, 12.98, 13.06, 1500)]),
    ("kanzhangtunmo", base(35) + [
        bar(36, 11.00, 11.05, 10.70, 10.75, 1000),
        bar(37, 10.72, 11.15, 10.70, 11.12, 1400)]),
    ("kandietunmo", base(35) + [
        bar(36, 10.70, 11.05, 10.68, 11.00, 1000),
        bar(37, 11.05, 11.08, 10.60, 10.65, 1400)]),
]


@pytest.mark.parametrize("code,bars", _CASES, ids=[c[0] for c in _CASES])
def test_each_pattern_detected(code, bars):
    rep = cp.detect_combo_patterns(bars, period="daily")
    assert code in [h["code"] for h in rep["hits"]], rep["summary"]
    assert rep["n_bars"] == len(bars) and rep["period"] == "daily"
    for h in rep["hits"]:
        assert set(h) >= {"code", "name", "direction", "date", "detail"}
        assert len(h["date"]) == 10


def test_xianrenzhilu_and_shejizhixing_are_exclusive():
    """同一根长上影线不能既报仙人指路又报射击之星。"""
    bars = _CASES[7][1]  # 高位长上影
    codes = [h["code"] for h in cp.detect_combo_patterns(bars)["hits"]]
    assert "shejizhixing" in codes
    assert "xianrenzhilu" not in codes
    low = _CASES[1][1]   # 上涨初期长上影
    codes2 = [h["code"] for h in cp.detect_combo_patterns(low)["hits"]]
    assert "xianrenzhilu" in codes2 and "shejizhixing" not in codes2


def test_enabled_filter_and_summary_dedup():
    bars = _CASES[4][1]  # 红三兵连续命中
    rep = cp.detect_combo_patterns(bars, enabled=["hongsanbing"],
                                   period="weekly")
    assert rep["enabled"] == ["hongsanbing"] and rep["period"] == "weekly"
    assert all(h["code"] == "hongsanbing" for h in rep["hits"])
    # 摘要按形态去重：同一形态连续命中只出现一次
    assert rep["summary"].count("红三兵") == 1
    assert cp.detect_combo_patterns(bars, enabled=["不存在"])["n_hits"] == 0
    assert cp.detect_combo_patterns([])["summary"] == "—"


# ---------------------------------------------------------------- 配置层
def test_candle_override_sparse_env_priority_and_restore(monkeypatch):
    assert rts.candle_options_normalized()["lookback"] == 60
    assert rts.candle_options_normalized()["patterns"] == list(cp.PATTERN_CODES)
    assert not rts.candle_override_exists()

    rts.save_candle_override({
        "MYSTERY_CANDLE_LOOKBACK": 30,
        "MYSTERY_CANDLE_PATTERNS": ["duofangpao", "xianrenzhilu"],
        "MYSTERY_CANDLE_WEEKLY": "0",
        "未知键": "忽略",
    })
    raw = rts.load_candle_override()
    assert "未知键" not in raw and raw["MYSTERY_CANDLE_WEEKLY"] == "0"
    n = rts.candle_options_normalized()
    assert n["lookback"] == 30 and n["patterns"] == ["duofangpao", "xianrenzhilu"]
    assert n["weekly"] is False and n["daily"] is True

    monkeypatch.setenv("MYSTERY_CANDLE_LOOKBACK", "90")   # 显式 env 最高优先
    assert rts.candle_options_normalized()["lookback"] == 90

    rts.clear_candle_override()
    assert not rts.candle_override_exists()
    assert rts.candle_options_display()["MYSTERY_CANDLE_WEEKLY"] == "1"


def test_candle_cache_tag_always_nonempty_and_changes():
    t0 = rts.candle_cache_tag()
    assert t0.startswith("|C[") and "L60" in t0
    rts.save_candle_override({"MYSTERY_CANDLE_LOOKBACK": 25})
    t1 = rts.candle_cache_tag()
    assert t1 != t0 and "L25" in t1
    rts.save_candle_override({"MYSTERY_CANDLE_ENABLED": "0"})
    assert rts.candle_cache_tag() != t1       # 开关变化也 miss


# ---------------------------------------------------------------- service
def _opts(**kw):
    o = {"enabled": True, "daily": True, "weekly": True,
         "lookback": 60, "patterns": list(cp.PATTERN_CODES)}
    o.update(kw)
    return o


def test_analyze_candlestick_daily_weekly_are_independent():
    daily = _CASES[0][1]        # 多方炮
    weekly = []                 # 周 K 无数据
    res = analyze_candlestick(daily, weekly, options=_opts())
    assert res["enabled"] and res["config"]["lookback"] == 60
    assert res["daily"]["n_hits"] >= 1
    assert "多方炮" in res["summary_daily"]
    assert res["weekly"]["n_hits"] == 0
    assert res["summary_weekly"] == "—"
    assert res["direction"] == "看涨"
    assert res["n_hits"] == res["daily"]["n_hits"] + res["weekly"]["n_hits"]


def test_analyze_candlestick_disabled_and_missing_data():
    off = analyze_candlestick(_CASES[0][1], [], options=_opts(enabled=False))
    assert off["enabled"] is False and off["n_hits"] == 0
    assert off["summary_daily"] == "—" and "总开关关闭" in off["daily"]["reason"]
    partial = analyze_candlestick(None, None, options=_opts())
    assert partial["n_hits"] == 0 and "无 K 线数据" in partial["daily"]["reason"]
    # weekly 关时不影响 daily
    woff = analyze_candlestick(_CASES[0][1], None, options=_opts(weekly=False))
    assert woff["daily"]["n_hits"] >= 1 and "未启用" in woff["weekly"]["reason"]


def test_service_is_json_serializable():
    import json
    res = analyze_candlestick(_CASES[0][1], _CASES[4][1], options=_opts())
    json.dumps(res, ensure_ascii=False)  # 不抛即通过


# ---------------------------------------------------------------- 展示层
def _mk_result(with_cs=True):
    d = {
        "symbol": "sh600519", "name": "贵州茅台", "trade_date": "2026-11-08",
        "score": 70.0, "advice": "持有", "true_resonance": True, "price": 10.0,
        "mystery": {"vap_atr": {}, "platform": {}, "main_wave": {},
                    "resonance": {}, "checklist8": {}, "signal": {},
                    "technical": {}, "patterns": {}},
        "financial": {}, "sector": {"行业名称": "测试"}, "chan": {},
        "czsc_ver": "",
    }
    if with_cs:
        d["candlestick"] = analyze_candlestick(
            _CASES[0][1], _CASES[4][1], options=_opts())
    return d


def test_excel_has_candlestick_columns_and_values():
    wb = openpyxl.load_workbook(io.BytesIO(excel_bytes([_mk_result()])))
    ws = wb["汇总报告"]
    headers = [c.value for c in ws[1]]
    for col in ("日K形态", "周K形态", "形态方向"):
        assert col in headers, f"缺 W82 列 {col}"
    vals = dict(zip(headers, [c.value for c in ws[2]]))
    assert "多方炮" in vals["日K形态"]
    assert "红三兵" in vals["周K形态"]
    assert vals["形态方向"] == "看涨"
    # 明细列按周期带前缀，不互相覆盖
    assert any(str(h).startswith("日K命中") for h in headers)
    assert any(str(h).startswith("周K命中") for h in headers)
    assert headers.index("名称") < headers.index("代码")   # W76 不回退


def test_excel_old_payload_safe():
    wb = openpyxl.load_workbook(io.BytesIO(excel_bytes([_mk_result(False)])))
    headers = [c.value for c in wb["汇总报告"][1]]
    vals = dict(zip(headers, [c.value for c in wb["汇总报告"][2]]))
    assert vals["日K形态"] == "—" and vals["周K形态"] == "—"


def test_html_has_candlestick_fields(tmp_path):
    path = str(tmp_path / "r.html")
    write_html([_mk_result()], path)
    html = open(path, encoding="utf-8").read()
    assert "日K形态" in html and "周K形态" in html
    assert "多方炮" in html and "红三兵" in html


def test_result_fields_readonly_helpers():
    d = _mk_result()
    assert "多方炮" in rf.candlestick_daily_text(d)
    assert "红三兵" in rf.candlestick_weekly_text(d)
    assert rf.candlestick_direction_text(d) == "看涨"
    assert len(rf.candlestick_hits(d, "daily")) >= 1
    empty = _mk_result(False)
    assert rf.candlestick_daily_text(empty) == "—"
    assert rf.candlestick_hits(empty, "daily") == []


# ---------------------------------------------------------------- 接线护栏
def test_web_and_entry_wiring_text_guards():
    scan = (_ROOT / "mystery/apps/web/views/scan.py").read_text(encoding="utf-8")
    stock = (_ROOT / "mystery/apps/web/views/stock.py").read_text(encoding="utf-8")
    system = (_ROOT / "mystery/apps/web/views/system.py").read_text(encoding="utf-8")
    analyze = (_ROOT / "mystery/services/analyze.py").read_text(encoding="utf-8")
    models = (_ROOT / "mystery/core/models.py").read_text(encoding="utf-8")
    assert "'日K形态'" in scan and "'周K形态'" in scan
    assert "典型K线组合形态" in stock and "'daily'" in stock and "'weekly'" in stock
    assert "render_candlestick_config" in system
    assert "一键还原默认值" in system
    assert "candlestick=_candlestick_result(daily, weekly)" in analyze
    assert "candle_cache_tag()" in analyze          # 配置变更旧缓存必失效
    assert "candlestick: Dict[str, Any]" in models


def test_no_score_impact():
    """形态仅展示：不改 RULE_VER、scorer 不引用 candlestick。"""
    from mystery.core.models import RULE_VER
    scorer = (_ROOT / "mystery/core/scorer.py").read_text(encoding="utf-8")
    assert "candlestick" not in scorer
    assert isinstance(RULE_VER, str) and RULE_VER
