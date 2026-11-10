# -*- coding: utf-8 -*-
"""W87：板块钻取页「板块分析内容」字段映射与接入守卫。

纯函数测试，不启动 Streamlit runtime；接入守卫直接读源码文本，
避免 Web 视图层对测试环境的依赖。
"""
from __future__ import annotations

from pathlib import Path

from mystery.apps.web.views import sector_fields as sf

REPO = Path(__file__).resolve().parents[1]
VIEWS = REPO / "mystery" / "apps" / "web" / "views"
SECTOR_SRC = (VIEWS / "sector.py").read_text(encoding="utf-8")
FIELDS_SRC = (VIEWS / "sector_fields.py").read_text(encoding="utf-8")


def _sector():
    return {
        "name": "电力", "code": "ths_881145", "date": "2026-11-10",
        "close": 1815.04, "chg_5d": 3.15, "chg_20d": -4.9,
        "above_year": False, "year_dist_pct": -7.43, "ma_aligned": False,
        "strength_score": 15.44, "strength_up": True,
        "candle_daily": "锤头线(10-23·多)",
        "candle_weekly": "三只乌鸦(10-30·空)",
        "accumulation_level": "疑似吸筹",
        "accumulation": {
            "level": "medium", "level_cn": "疑似吸筹", "stage": "震荡",
            "retrace_pct": 23.69, "vol_state": "常量",
            "evidence": ["低位：较近120根高点回撤23.7%", "常量：量比1.077"],
            "conclusion": "疑似吸筹：观察区间，非买点",
            "above_year": False, "chg_20d": -4.9,
        },
        "related": [
            {"symbol": "sz.002616", "name": "长青集团", "corr": 0.482,
             "analyzed": False, "score": None, "advice": "—",
             "accumulation_level": "—"},
        ],
        "has_data": True,
    }


class TestSectorDrilldownView:
    def test_full_fields(self):
        v = sf.sector_drilldown_view(_sector())
        assert v["has_data"] is True
        assert v["name"] == "电力"
        assert v["code"] == "ths_881145"
        assert v["date"] == "2026-11-10"
        assert v["close"] == "1815.04"
        assert v["chg_5d"] == "+3.15%"
        assert v["chg_20d"] == "-4.90%"
        assert v["year"] == "跌破年线(-7.4%)"
        assert v["ma"] == "非多头"
        assert v["strength"] == "15.4（偏强）"
        assert v["candle_daily"] == "锤头线(10-23·多)"
        assert v["candle_weekly"] == "三只乌鸦(10-30·空)"
        assert v["acc_level"] == "疑似吸筹"
        assert v["acc_stage"] == "震荡"
        assert v["acc_retrace"] == "23.7%"
        assert v["acc_vol"] == "常量"
        assert v["acc_evidence"] == ["低位：较近120根高点回撤23.7%",
                                      "常量：量比1.077"]
        assert v["acc_conclusion"] == "疑似吸筹：观察区间，非买点"

    def test_above_year_and_bull_ma(self):
        d = _sector()
        d.update({"above_year": True, "year_dist_pct": 5.25,
                  "ma_aligned": True, "strength_up": False})
        v = sf.sector_drilldown_view(d)
        assert v["year"] == "站上年线(+5.2%)"
        assert v["ma"] == "多头"
        assert v["strength"] == "15.4（偏弱）"

    def test_missing_data_degrades_to_dash(self):
        v = sf.sector_drilldown_view({"has_data": False})
        assert v["has_data"] is False
        for k in ("close", "chg_5d", "chg_20d", "year", "ma", "strength",
                  "candle_daily", "candle_weekly", "acc_level", "acc_stage",
                  "acc_retrace", "acc_vol", "acc_conclusion"):
            assert v[k] == "—", k
        assert v["acc_evidence"] == []

    def test_none_and_wrong_type_input_safe(self):
        assert sf.sector_drilldown_view(None)["has_data"] is False
        assert sf.sector_drilldown_view("bad")["has_data"] is False
        assert sf.related_stock_rows(None) == []

    def test_accumulation_may_be_missing(self):
        d = _sector()
        d["accumulation"] = None
        v = sf.sector_drilldown_view(d)
        assert v["acc_stage"] == "—"
        assert v["acc_evidence"] == []
        assert v["acc_level"] == "疑似吸筹"  # 顶层等级仍可展示


class TestRelatedStockRows:
    def test_basic_without_results(self):
        rows = sf.related_stock_rows(_sector()["related"])
        assert len(rows) == 1
        r = rows[0]
        assert r["名称"] == "长青集团"
        assert r["代码"] == "sz.002616"
        assert r["相关系数"] == 0.482
        assert r["本期已分析"] == "否"
        assert r["评分"] == "—"
        assert r["主力吸筹"] == "—"

    def test_enriched_by_current_results(self):
        results = [{"symbol": "002616.SZ", "name": "长青集团",
                    "score": 72, "advice": "观察",
                    "stage": "吸筹", "chip_low": True}]
        rows = sf.related_stock_rows(_sector()["related"], results)
        r = rows[0]
        assert r["本期已分析"] == "是"
        assert r["评分"] == "72"
        assert r["操作建议"] == "观察"
        assert isinstance(r["主力吸筹"], str)

    def test_symbol_normalized_across_formats(self):
        rel = [{"symbol": "sz.000001", "name": "平安银行", "corr": 0.71}]
        results = [{"symbol": "000001.SZ", "score": 66, "advice": "持有"}]
        rows = sf.related_stock_rows(rel, results)
        assert rows[0]["本期已分析"] == "是"
        assert rows[0]["评分"] == "66"

    def test_empty_and_bad_items(self):
        assert sf.related_stock_rows([]) == []
        assert sf.related_stock_rows([None, 1, {}])[0]["名称"] == "—"

    def test_column_types_are_str_except_corr(self):
        rows = sf.related_stock_rows(_sector()["related"])
        for k, v in rows[0].items():
            if k == "相关系数":
                assert isinstance(v, float)
            else:
                assert isinstance(v, str), k


class TestWiringGuards:
    def test_view_sector_calls_render(self):
        assert "_render_sector_drilldown(s_name, s_code, ind)" in SECTOR_SRC

    def test_uses_shared_runtime_config(self):
        assert "sector_options_normalized" in SECTOR_SRC
        assert "analyze_sector" in SECTOR_SRC

    def test_cached_readonly(self):
        assert "@st.cache_data" in SECTOR_SRC
        assert "_sector_drilldown_cached" in SECTOR_SRC

    def test_original_scan_features_kept(self):
        for token in ("scan_market(universe=stocks", "_bg_launch(",
                      "_render_scan_table(rows, key=\"sector\")",
                      "view_sector_strength", "_sector_strength_table"):
            assert token in SECTOR_SRC, token

    def test_no_recompute_of_stock_analysis(self):
        # 只检查实际调用/赋值，忽略中文提示与 docstring 里的说明文字
        # （如「走 analyze_one_stock」「不改 RULE_VER」）。
        assert "analyze_one_stock(" not in SECTOR_SRC
        assert "RULE_VER =" not in SECTOR_SRC

    def test_fields_module_is_streamlit_free(self):
        assert "import streamlit" not in FIELDS_SRC
        assert "st.session_state" not in FIELDS_SRC
        assert "sector_drilldown_view" in FIELDS_SRC
        assert "related_stock_rows" in FIELDS_SRC
