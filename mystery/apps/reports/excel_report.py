"""mystery.apps.reports.excel_report — AnalysisResult 列表 → Excel。

函数签名（002.md W1-A）::

    def write_excel(results: list[dict], path: str) -> str

results = ``[AnalysisResult.to_dict(), ...]``。生成器内禁止取数/调 analyze。
"""
from __future__ import annotations

import io
import logging
from typing import Any, Dict, List

import pandas as pd

logger = logging.getLogger(__name__)

SUMMARY_COLS = [
    ("symbol", "代码"),
    ("name", "名称"),
    ("score", "综合评分"),
    ("advice", "操作建议"),
    ("true_resonance", "真三振"),
    ("sector_name", "行业"),
    ("price", "最新价"),
    ("main_wave", "主升浪状态"),
    ("checklist_n", "主升浪满足"),
    ("platform", "平台状态"),
    ("vap_upper", "VAP-ATR上轨"),
    ("vap_break", "VAP-ATR突破"),
    ("pe", "PE"),
    ("pb", "PB"),
    ("trade_date", "分析日期"),
]


def _flat(d: Dict[str, Any]) -> Dict[str, Any]:
    """把嵌套 to_dict() 拍平成汇总行（只读已有字段，不重算）。"""
    m = d.get("mystery", {}) or {}
    vap = m.get("vap_atr", {}) or {}
    plat = m.get("platform", {}) or {}
    main_wave = m.get("main_wave", {}) or {}
    cl = m.get("checklist8", {}) or {}
    fin = d.get("financial", {}) or {}
    sec = d.get("sector", {}) or {}
    pr = vap.get("平台范围") or {}
    return {
        "symbol": d.get("symbol", ""),
        "name": d.get("name") or "未知",
        "score": d.get("score"),
        "advice": d.get("advice", ""),
        "true_resonance": "✅" if d.get("true_resonance") else "❌",
        "sector_name": sec.get("行业名称", "未知"),
        "price": d.get("price"),
        "main_wave": main_wave.get("主升浪状态", "-"),
        "checklist_n": f"{cl.get('满足数量', 0)}/8" if cl else "-",
        "platform": plat.get("平台状态", "-"),
        "vap_upper": vap.get("自适应上轨") or pr.get("上沿"),
        "vap_break": "✅" if vap.get("突破信号") or plat.get("突破信号") else "❌",
        "pe": fin.get("PE"),
        "pb": fin.get("PB"),
        "trade_date": d.get("trade_date", ""),
    }


def _detail_rows(d: Dict[str, Any]) -> List[List[Any]]:
    """个股明细键值对（002.md：主升浪/平台/VAP-ATR/财务，无则空）。"""
    m = d.get("mystery", {}) or {}
    vap = m.get("vap_atr", {}) or {}
    plat = m.get("platform", {}) or {}
    main_wave = m.get("main_wave", {}) or {}
    res = m.get("resonance", {}) or {}
    sig = m.get("signal", {}) or {}
    fin = d.get("financial", {}) or {}
    sec = d.get("sector", {}) or {}
    rows: List[List[Any]] = []

    def sec_row(title: str):
        rows.append([title, "", ""])
        rows.append(["", "", ""])

    sec_row("基础信息")
    rows.append(["股票代码", d.get("symbol", ""), ""])
    rows.append(["股票名称", d.get("name") or "未知", ""])
    rows.append(["分析日期", d.get("trade_date", ""), ""])
    rows.append(["最新价", d.get("price"), ""])
    rows.append(["综合评分", d.get("score"), ""])
    rows.append(["操作建议", d.get("advice", ""), ""])
    rows.append(["真三振", "✅" if d.get("true_resonance") else "❌", ""])
    rows.append(["行业", sec.get("行业名称", "未知"), ""])
    rows.append(["行业趋势分", sec.get("行业趋势分"), ""])

    sec_row("三振共振")
    rows.append(["个股趋势", "✅" if res.get("个股趋势") else "❌", ""])
    rows.append(["行业趋势", "✅" if res.get("行业趋势") else "❌", ""])
    rows.append(["大盘趋势", "✅" if res.get("大盘趋势") else "❌", ""])
    rows.append(["共振评分", res.get("共振评分"), ""])
    rows.append(["共振级别", res.get("共振级别", "-"), ""])
    rows.append(["年线滤网", "✅" if sig.get("年线滤网") else "❌", ""])
    for i, line in enumerate((res.get("详情") or [])[:6], 1):
        rows.append([f"共振详情{i}", line, ""])

    sec_row("主升浪")
    rows.append(["主升浪状态", main_wave.get("主升浪状态", "-"), ""])
    rows.append(["持股状态", "✅" if main_wave.get("持股状态") else "❌", ""])
    rows.append(["空中加油", "✅" if main_wave.get("空中加油") else "❌", ""])
    rows.append(["MA5斜率", main_wave.get("MA5斜率"), ""])
    cl = m.get("checklist8", {}) or {}
    rows.append(["主升浪满足", f"{cl.get('满足数量', 0)}/8", ""])
    rows.append(["主升浪综合判断", cl.get("综合判断", "-"), ""])
    for i, line in enumerate((main_wave.get("判定依据") or [])[:5], 1):
        rows.append([f"判定依据{i}", line, ""])

    sec_row("平台突破")
    rows.append(["平台状态", plat.get("平台状态", "-"), ""])
    rows.append(["突破信号", "✅" if plat.get("突破信号") else "❌", ""])
    rows.append(["买横信号", "✅" if plat.get("买横信号") else "❌", ""])
    pr = plat.get("平台范围") or {}
    if pr:
        rows.append(["平台箱体", f"{pr.get('下沿')} ~ {pr.get('上沿')}", ""])
    fixed = plat.get("固定箱体") or {}
    if fixed:
        rows.append(["固定箱体(近20日)", f"{fixed.get('下沿')} ~ {fixed.get('上沿')}", ""])
    rows.append(["多周期箱体状态", plat.get("多周期箱体状态", "-"), ""])

    sec_row("自适应 VAP-ATR")
    rows.append(["POC(筹码控制点)", vap.get("POC"), ""])
    rows.append(["自适应上轨", vap.get("自适应上轨"), ""])
    rows.append(["自适应下轨", vap.get("自适应下轨"), ""])
    rows.append(["ATR", vap.get("ATR"), ""])
    rows.append(["突破信号", "✅" if vap.get("突破信号") else "❌", ""])
    cyc = vap.get("自适应周期") or {}
    if cyc:
        rows.append(["自适应周期", f"N={cyc.get('adaptive_n')} 快ATR={cyc.get('atr_m')} k={cyc.get('k')}", ""])
        if cyc.get("avg_turnover") is not None:
            rows.append(["近20日均换手", f"{cyc.get('avg_turnover')}%", ""])
    for i, line in enumerate((vap.get("详情") or [])[:4], 1):
        rows.append([f"VAP详情{i}", line, ""])

    sec_row("财务")
    rows.append(["PE", fin.get("PE"), ""])
    rows.append(["PB", fin.get("PB"), ""])
    rows.append(["ROE", fin.get("roe"), ""])
    rows.append(["股息", fin.get("divid_cash"), ""])
    rows.append(["报告期", fin.get("report_date", ""), ""])

    # 缠论摘要列（W3-A：chan 关时为空，不改变 score）
    chan = d.get("chan", {}) or {}
    if chan:
        sec_row("缠论结构")
        c1 = chan.get("1d") or {}
        cw = chan.get("1w") or {}
        rows.append(["日线末笔方向", "向上" if c1.get("last_bi_dir") == "up" else (
            "向下" if c1.get("last_bi_dir") == "down" else "-"), ""])
        rows.append(["日线末笔确认", "✅" if c1.get("last_bi_confirmed") else "❌", ""])
        rows.append(["日线在中枢内", "✅" if c1.get("in_zs") else "❌", ""])
        rows.append(["周线末笔方向", "向上" if cw.get("last_bi_dir") == "up" else (
            "向下" if cw.get("last_bi_dir") == "down" else "-"), ""])
        rows.append(["czsc 版本", d.get("czsc_ver", ""), ""])
    return rows


def _sheet_name(d: Dict[str, Any]) -> str:
    name = str(d.get("name", "") or "未知")
    code = str(d.get("symbol", "")).replace(".", "")
    s = f"个股{name}_{code}"
    return s[:31]


def _build_buffer(results: List[Dict[str, Any]]) -> "io.BytesIO":
    """生成 Excel 到内存 buffer（汇总 + 每只个股明细）。write_excel 与
    excel_bytes 共用，保证 web 下载与 daily 落盘格式完全一致。"""
    import io
    rows = [_flat(d) for d in results]
    if rows:
        rows.sort(key=lambda r: (r["score"] is not None, float(r["score"] or -1)),
                  reverse=True)
    summary = pd.DataFrame(rows).rename(columns=dict(SUMMARY_COLS))

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="汇总报告", index=False)
        ws = writer.sheets["汇总报告"]
        for i, col in enumerate(summary.columns, 1):
            width = max(len(str(col)), 10)
            ws.column_dimensions[chr(64 + i)].width = width
        for d in results:
            det = pd.DataFrame(_detail_rows(d), columns=["项目", "结果", "备注"])
            det.to_excel(writer, sheet_name=_sheet_name(d), index=False)
            ws2 = writer.sheets[_sheet_name(d)]
            ws2.column_dimensions["A"].width = 20
            ws2.column_dimensions["B"].width = 26
            ws2.column_dimensions["C"].width = 40
    buf.seek(0)
    return buf


def excel_bytes(results: List[Dict[str, Any]]) -> bytes:
    """Web 下载用：AnalysisResult 列表 → xlsx bytes（格式同 write_excel）。"""
    return _build_buffer(results).getvalue()


def write_excel(results: List[Dict[str, Any]], path: str) -> str:
    """汇总 + 每只个股明细。返回写入路径。"""
    import os
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    buf = _build_buffer(results)
    with open(path, "wb") as f:
        f.write(buf.getvalue())
    logger.info("✅ Excel 报告生成完成: %s（%d 只）", path, len(results))
    return path
