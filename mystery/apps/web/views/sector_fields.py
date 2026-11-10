"""W87：板块钻取页字段映射（纯函数，零 Streamlit / 零 IO）。

只把 ``services.sector_analysis.analyze_sector`` 的结果转换成展示值，
供 Web 视图层渲染；与每日报告「行业板块」区块（html_report._sector_block）
同口径。不取数、不重算个股评分、不改信号与 RULE_VER。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

__all__ = ["sector_drilldown_view", "related_stock_rows"]


def _sym_keys(s: Any) -> set:
    """个股代码归一：同时保留原始写法与纯数字。

    兼容 ``sz.002616`` / ``002616.SZ`` / ``002616`` 等写法，用于把板块
    强相关个股与本期扫描结果做只读关联。
    """
    t = str(s or "").strip()
    digits = "".join(ch for ch in t if ch.isdigit())
    return {k for k in (t, digits) if k}


def _fmt_num(v: Any, nd: int = 2) -> str:
    try:
        return f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_pct(v: Any, nd: int = 2) -> str:
    try:
        return f"{float(v):+.{nd}f}%"
    except (TypeError, ValueError):
        return "—"


def sector_drilldown_view(d: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """把板块研判 dict 映射为钻取页展示字段。

    任何字段缺失一律降级为 ``'—'``；``has_data=False`` 时视图层只提示，
    不影响原有成分股扫描功能。
    """
    d = d if isinstance(d, dict) else {}
    acc = d.get("accumulation") if isinstance(d.get("accumulation"), dict) else {}

    above, dist = d.get("above_year"), d.get("year_dist_pct")
    year = "—" if above is None else ("站上年线" if above else "跌破年线")
    if above is not None and dist is not None:
        try:
            year += f"({float(dist):+.1f}%)"
        except (TypeError, ValueError):
            pass

    ma = d.get("ma_aligned")
    ma_txt = "—" if ma is None else ("多头" if ma else "非多头")

    st_score = d.get("strength_score")
    st_txt = "—"
    if st_score is not None:
        try:
            arrow = "偏强" if d.get("strength_up") else "偏弱"
            st_txt = f"{float(st_score):.1f}（{arrow}）"
        except (TypeError, ValueError):
            st_txt = "—"

    return {
        "has_data": bool(d.get("has_data")),
        "name": str(d.get("name") or "—"),
        "code": str(d.get("code") or "—"),
        "date": str(d.get("date") or "—"),
        "close": _fmt_num(d.get("close")),
        "chg_5d": _fmt_pct(d.get("chg_5d")),
        "chg_20d": _fmt_pct(d.get("chg_20d")),
        "year": year,
        "ma": ma_txt,
        "strength": st_txt,
        "candle_daily": str(d.get("candle_daily") or "—"),
        "candle_weekly": str(d.get("candle_weekly") or "—"),
        "acc_level": str(d.get("accumulation_level") or "—"),
        "acc_stage": str(acc.get("stage") or "—"),
        "acc_retrace": (
            f"{_fmt_num(acc.get('retrace_pct'), nd=1)}%"
            if acc.get("retrace_pct") is not None else "—"),
        "acc_vol": str(acc.get("vol_state") or "—"),
        "acc_evidence": [str(x) for x in (acc.get("evidence") or [])],
        "acc_conclusion": str(acc.get("conclusion") or "—"),
    }


def related_stock_rows(
    rel: Optional[Sequence[Dict[str, Any]]],
    results: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """强相关个股明细（只读）。

    传入本期个股扫描结果时，把命中的个股评分 / 操作建议 / 吸筹等级补上；
    不重算个股分析，也不改板块研判缓存。返回值全部为 str / float，
    避免 Streamlit 表格 int/str 混列告警。
    """
    by: Dict[str, Dict[str, Any]] = {}
    for r in (results or []):
        if not isinstance(r, dict):
            continue
        for k in _sym_keys(r.get("symbol")):
            by.setdefault(k, r)

    out: List[Dict[str, Any]] = []
    for item in (rel or []):
        if not isinstance(item, dict):
            continue
        d = dict(item)
        hit = None
        for k in _sym_keys(d.get("symbol")):
            if k in by:
                hit = by[k]
                break
        if hit:
            d["analyzed"] = True
            d["score"] = hit.get("score")
            d["advice"] = hit.get("advice") or "—"
            try:
                from mystery.core.accumulation import (
                    accumulation_from_result,
                    accumulation_label,
                )
                d["accumulation_level"] = accumulation_label(
                    accumulation_from_result(hit, name=d.get("name") or "")
                )
            except Exception:  # noqa: BLE001 展示层缺字段时静默降级
                pass

        sc = d.get("score")
        corr = d.get("corr")
        try:
            corr_out = float(corr) if corr is not None else None
        except (TypeError, ValueError):
            corr_out = None
        out.append({
            "名称": str(d.get("name") or "—"),
            "代码": str(d.get("symbol") or "—"),
            "相关系数": corr_out,
            "本期已分析": "是" if d.get("analyzed") else "否",
            "评分": str(sc) if sc is not None else "—",
            "操作建议": str(d.get("advice") or "—"),
            "主力吸筹": str(d.get("accumulation_level") or "—"),
        })
    return out
