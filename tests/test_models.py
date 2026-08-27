"""test_models — P0：数据模型与 JSON 可序列化。"""
import datetime as dt
import json

from mystery.core.models import (
    AnalysisResult,
    Bar,
    BarSeries,
    ChanBi,
    ChanStructure,
    ChanZs,
    MysteryBreakdown,
)


def _sample_result() -> AnalysisResult:
    bars = [
        Bar(dt=dt.datetime(2026, 8, 26), open=10.0, high=10.5, low=9.9, close=10.2,
            volume=10000.0, amount=1.02e8),
        Bar(dt="2026-08-27", open=10.2, high=10.8, low=10.1, close=10.7),
    ]
    chan = ChanStructure(
        freq="1d",
        n_fx=3,
        bis=[ChanBi("up", "2026-08-01", "2026-08-10", 10.8, 9.8, True)],
        zss=[ChanZs(zg=10.5, zd=10.0, gg=10.8, dd=9.8, sdt="2026-08-01",
                    edt="2026-08-15", n_bi=5, finished=True)],
        last_bi_dir="up",
        last_bi_confirmed=True,
        in_zs=True,
        engine="czsc",
        engine_ver="1.0.1",
    )
    return AnalysisResult(
        symbol="600519.SH",
        name="贵州茅台",
        trade_date="2026-08-27",
        price=10.7,
        score=85.0,
        advice="买入",
        true_resonance=True,
        mystery=MysteryBreakdown(),
        chan={"1d": chan},
        sector={"行业": "白酒"},
        financial={"pe": 30.5},
    )


def test_bar_series_roundtrip():
    s = BarSeries(symbol="600519.SH", freq="1d", adjust="qfq", source="db")
    d = s.to_dict()
    assert d["symbol"] == "600519.SH"
    assert d["freq"] == "1d"
    assert d["adjust"] == "qfq"


def test_analysis_result_to_dict_json_serializable():
    r = _sample_result()
    d = r.to_dict()
    # datetime → iso
    assert d["mystery"]["platform"] == {}
    assert d["chan"]["1d"]["bis"][0]["sdt"] == "2026-08-01"
    assert d["chan"]["1d"]["bis"][0]["direction"] == "up"
    # 关键字段
    assert d["score"] == 85.0
    assert d["rule_ver"] == "mystery-1.22.30-compat"
    # 整体 JSON 可序列化
    s = json.dumps(d, ensure_ascii=False)
    assert '"贵州茅台"' in s
    assert '"2026-08-27"' in s


def test_chan_structure_defaults():
    c = ChanStructure(freq="1w")
    assert c.n_fx == 0
    assert c.bis == []
    assert c.zss == []
    assert c.last_bi_confirmed is False
    assert c.in_zs is False
    assert c.engine == "czsc"


def test_codes_normalization():
    from mystery.adapters.codes import (
        exchange_of,
        normalize_freq,
        normalize_symbol,
        to_tdx_api,
        to_tdx_local,
        to_ths,
    )

    for raw in ("sh600519", "600519.SH", "SH600519", "sh.600519", "600519"):
        assert normalize_symbol(raw) == "600519.SH", raw
    assert normalize_symbol("sz000001") == "000001.SZ"
    assert exchange_of("600519") == "SH"
    assert to_ths("sh600519") == "600519.SH"
    assert to_tdx_api("sh600519") == "SH600519"
    assert to_tdx_local("sh600519") == "sh600519"
    assert normalize_freq("日线") == "1d"
    assert normalize_freq("weekly") == "1w"
    assert normalize_freq("月线") == "1M"
