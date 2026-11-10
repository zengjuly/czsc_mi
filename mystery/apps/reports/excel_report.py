"""mystery.apps.reports.excel_report — AnalysisResult 列表 → Excel。

函数签名（002.md W1-A）::

    def write_excel(results: list[dict], path: str) -> str

results = ``[AnalysisResult.to_dict(), ...]``。生成器内禁止取数/调 analyze。

W14：取消个股详情 sheet，全部个股详情以列补充进「汇总报告」单页；
新增年线过滤系统各条件达成情况列（来自 ``mystery.signal['年线条件']``，
analyze 阶段由 basic_filter 逐项判定写入，报表只读不改判）。
"""
from __future__ import annotations

import io
import logging
from typing import Any, Dict, List, Optional

import pandas as pd

from mystery.core.bc_resonance import bc_resonance as _bc_resonance
from mystery.apps.reports import result_fields as _rf

logger = logging.getLogger(__name__)

SUMMARY_COLS = [
    # W76：用户要求展示列「名称在前，代码在后」（Web / CLI 同口径）。
    ("name", "名称"),
    ("symbol", "代码"),
    ("score", "综合评分"),
    ("board", "所属自选板块"),
    ("advice", "操作建议"),
    ("true_resonance", "真三振"),
    ("sector_name", "行业"),
    ("price", "最新价"),
    ("main_wave", "主升浪状态"),
    ("checklist_n", "主升浪满足"),
    ("platform", "平台状态"),
    ("vap_upper", "VAP-ATR上轨"),
    ("vap_break", "VAP-ATR突破"),
    # W80：日报字段对齐——固定包含 turnover_20 数值 + chip 相关标签，
    # 全部只读 to_dict()/payload 顶层字段（不重算、不取数）。
    ("turnover_20", "近20日均换手(%)"),
    ("chip_low", "筹码低位"),
    ("chip_tag", "筹码标签"),
    ("turnover_source", "换手来源"),
    ("turnover_tag", "换手分档"),
    ("style_tag", "风格"),
    ("pe", "PE"),
    ("pb", "PB"),
    ("trade_date", "分析日期"),
    # W69：三浪主升策略结论（合并到扫描结果；旧任务无字段显示 '-'）
    ("w3_verdict", "三浪结论"),
    ("w3_phase", "三浪阶段"),
    ("w3_position", "多周期位置"),
    ("w3_resonance", "周期共振"),
    ("w3_hard", "三浪铁律(硬)"),
    ("w3_stage", "三浪流程步"),
    ("w3_entry", "三浪买点"),
    ("w3_stop", "三浪止损"),
    ("w3_lots", "三浪建议(手)"),
    ("w3_scale", "组合缩放"),
    # W82：典型 K 线组合形态（日 K / 周 K 独立分析，只读 payload 附加字段）
    ("candle_daily", "日K形态"),
    ("candle_weekly", "周K形态"),
    ("candle_direction", "形态方向"),
    # W83：多方炮变体的「重点观察」量化结论（缺口支撑/缩量/低点抬高/待确认）
    ("candle_focus", "形态重点观察"),
]

# 年线过滤系统各条件（与 basic_filter._basic_filter_checks 键一一对应，
# 顺序即展示顺序；值为 ✅/❌/-）。
YEARLINE_COND_KEYS = [
    "收盘价>MA250", "收盘价>MA60", "均线多头排列",
    "MA5>MA250", "MA10>MA250", "MA20>MA250", "MA60>MA250",
]

# 个股详情补充列：与 SUMMARY_COLS 完全同义的项不重复展示（值不同义的保留）。
_DETAIL_SKIP = {
    "股票代码", "股票名称", "分析日期", "最新价", "综合评分", "操作建议",
    "真三振", "行业", "主升浪状态", "主升浪满足", "平台状态", "PE", "PB",
}
# section 前缀（避免「突破信号」等跨段同名冲突）
_SECTION_PREFIX = {
    "基础信息": "",
    "三振共振": "三振.",
    "主升浪": "主升浪.",
    "平台突破": "平台.",
    "自适应 VAP-ATR": "VAP.",
    "财务": "财务.",
    "缠论结构": "缠论.",
}


def _flag(v) -> str:
    """布尔 → ✅/❌（None/缺失 → '-'）。"""
    if v is None:
        return "-"
    return "✅" if v else "❌"


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
    row: Dict[str, Any] = {
        "symbol": d.get("symbol", ""),
        "name": d.get("name") or "未知",
        "score": d.get("score"),
        "advice": d.get("advice", ""),
        "true_resonance": _flag(d.get("true_resonance")),
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
        # W80：turnover_20 数值 + chip 三态标签 + 换手来源（只读 payload 顶层）
        "turnover_20": _rf.turnover_20_value(d),
        "chip_low": _rf.chip_low_text(d),
        "chip_tag": _rf.chip_tag_text(d),
        "turnover_source": _rf.turnover_source_text(d),
    }
    # W32b：换手分档 + 风格标签（只读 core 纯函数，缺 turn → 未知）
    from ...core.turnover_tag import tags_from_result
    row.update(tags_from_result(d))
    # 年线过滤系统各条件达成情况（W14）
    sig = m.get("signal", {}) or {}
    yl = sig.get("年线条件") or {}
    for key in YEARLINE_COND_KEYS:
        row[f"yl:{key}"] = _flag(yl.get(key))
    # W69：三浪主升（只读 payload 内附加字段，不重算；旧任务缺字段显示 '-'）
    w3 = d.get("wave3") or {}
    row.update({
        "w3_verdict": w3.get("verdict") or "-",
        "w3_phase": w3.get("phase") or "-",
        "w3_position": w3.get("position") or "-",
        "w3_resonance": w3.get("resonance") or "-",
        "w3_hard": w3.get("hard") or "-",
        "w3_stage": w3.get("stage") if w3.get("stage") is not None else "-",
        "w3_entry": w3.get("entry") if w3.get("entry") is not None else "-",
        "w3_stop": w3.get("stop") if w3.get("stop") is not None else "-",
        "w3_lots": w3.get("lots") if w3.get("lots") is not None else "-",
        "w3_scale": (w3.get("scale_down")
                     if w3.get("scale_down") is not None else "-"),
    })
    # W82：形态结论（只读 payload 顶层 candlestick；旧任务无字段显示 '—'）
    row.update({
        "candle_daily": _rf.candlestick_daily_text(d),
        "candle_weekly": _rf.candlestick_weekly_text(d),
        "candle_direction": _rf.candlestick_direction_text(d),
        "candle_focus": _rf.candlestick_focus_text(d),
    })
    # 个股详情补充列（W14：原个股详情 sheet 内容并入汇总页）
    row.update(_detail_flat(d))
    return row


def _detail_flat(d: Dict[str, Any]) -> Dict[str, Any]:
    """个股详情键值对 → 扁平列 dict（只读已有字段，不重算）。

    跳过 section 标题/空行/与 SUMMARY_COLS 同义重复项；列名带 section
    前缀防跨段同名冲突。
    """
    m = d.get("mystery", {}) or {}
    vap = m.get("vap_atr", {}) or {}
    plat = m.get("platform", {}) or {}
    main_wave = m.get("main_wave", {}) or {}
    res = m.get("resonance", {}) or {}
    sig = m.get("signal", {}) or {}
    fin = d.get("financial", {}) or {}
    sec = d.get("sector", {}) or {}
    rows: List[tuple] = []
    section = ""

    def add(title: str, val: Any):
        rows.append((section, title, val))

    add("股票代码", d.get("symbol", ""))
    add("股票名称", d.get("name") or "未知")
    add("分析日期", d.get("trade_date", ""))
    add("最新价", d.get("price"))
    add("综合评分", d.get("score"))
    add("操作建议", d.get("advice", ""))
    add("真三振", _flag(d.get("true_resonance")))
    add("行业", sec.get("行业名称", "未知"))
    add("行业趋势分", sec.get("行业趋势分"))

    def sec_rows(title: str, items: List[tuple]):
        nonlocal section
        section = title
        for k, v in items:
            rows.append((section, k, v))

    sec_rows("三振共振", [
        ("个股趋势", _flag(res.get("个股趋势"))),
        ("行业趋势", _flag(res.get("行业趋势"))),
        ("大盘趋势", _flag(res.get("大盘趋势"))),
        ("共振评分", res.get("共振评分")),
        ("共振级别", res.get("共振级别", "-")),
        ("年线滤网", _flag(sig.get("年线滤网"))),
    ])
    for i, line in enumerate((res.get("详情") or [])[:6], 1):
        rows.append((section, f"共振详情{i}", line))

    sec_rows("主升浪", [
        ("主升浪状态", main_wave.get("主升浪状态", "-")),
        ("持股状态", _flag(main_wave.get("持股状态"))),
        ("空中加油", _flag(main_wave.get("空中加油"))),
        ("MA5斜率", main_wave.get("MA5斜率")),
    ])
    cl = m.get("checklist8", {}) or {}
    rows.append((section, "主升浪满足", f"{cl.get('满足数量', 0)}/8" if cl else "-"))
    rows.append((section, "主升浪综合判断", cl.get("综合判断", "-")))
    for i, line in enumerate((main_wave.get("判定依据") or [])[:5], 1):
        rows.append((section, f"判定依据{i}", line))

    sec_rows("平台突破", [
        ("平台状态", plat.get("平台状态", "-")),
        ("突破信号", _flag(plat.get("突破信号"))),
        ("买横信号", _flag(plat.get("买横信号"))),
    ])
    pr = plat.get("平台范围") or {}
    if pr:
        rows.append((section, "平台箱体", f"{pr.get('下沿')} ~ {pr.get('上沿')}"))
    fixed = plat.get("固定箱体") or {}
    if fixed:
        rows.append((section, "固定箱体(近20日)", f"{fixed.get('下沿')} ~ {fixed.get('上沿')}"))
    rows.append((section, "多周期箱体状态", plat.get("多周期箱体状态", "-")))

    sec_rows("自适应 VAP-ATR", [
        ("POC(筹码控制点)", vap.get("POC")),
        ("自适应上轨", vap.get("自适应上轨")),
        ("自适应下轨", vap.get("自适应下轨")),
        ("ATR", vap.get("ATR")),
        ("突破信号", _flag(vap.get("突破信号"))),
    ])
    cyc = vap.get("自适应周期") or {}
    if cyc:
        rows.append((section, "自适应周期",
                     f"N={cyc.get('adaptive_n')} 快ATR={cyc.get('atr_m')} k={cyc.get('k')}"))
        if cyc.get("avg_turnover") is not None:
            rows.append((section, "近20日均换手", f"{cyc.get('avg_turnover')}%"))
    for i, line in enumerate((vap.get("详情") or [])[:4], 1):
        rows.append((section, f"VAP详情{i}", line))

    # G4：高级形态（强势反包阳/缺口/低位地量）—— 来自 mystery.patterns（仅展示/轻加权）
    _pat = m.get("patterns", {}) or {}
    _eng = _pat.get("强势反包阳") or {}
    _gap = _pat.get("缺口") or {}
    _low = _pat.get("低位地量") or {}
    _eng_det = _eng.get("detail", {}) or {}
    _gap_det = _gap.get("detail", {}) or {}
    _low_det = _low.get("detail", {}) or {}
    sec_rows("高级形态", [
        ("强势反包阳(信号)", _flag(_eng.get("hit"))),
        ("反包强度", _eng_det.get("强度", "-")),
        ("缺口(向上跳空)", _flag(_gap.get("gap_up"))),
        ("缺口类型", _gap.get("gap_type") or "-"),
        ("跳空幅度%", _gap.get("gap_magnitude_pct")),
        ("三日不补", _flag(_gap_det.get("三日不补"))),
        ("低位地量(信号)", _flag(_low.get("hit"))),
        ("回撤%", _low_det.get("回撤%")),
        ("量/60日均量", _low_det.get("量/60日均量")),
    ])

    # W82：典型组合形态明细（按周期分区，逐条铺列，便于对账）
    # W83：每条命中后追加其「重点观察」量化结论，便于逐票核对验证点
    _cs = d.get("candlestick") or {}
    if _cs:
        for _period, _label in (("daily", "日K形态"), ("weekly", "周K形态")):
            _hits = _rf.candlestick_hits(d, _period, limit=5)
            _prefix = "日K" if _period == "daily" else "周K"
            sec_rows(_label, [
                (f"{_prefix}命中{i + 1}",
                 f"{h.get('name', '-')}({h.get('date', '-')}"
                 f"·{h.get('direction', '-')})")
                for i, h in enumerate(_hits)
            ] or [(f"{_prefix}命中1", "—")])
            _focus_rows = [
                (f"{_prefix}观察{i + 1}·{h.get('name', '-')}",
                 (h.get('detail') or {}).get('重点观察', '—'))
                for i, h in enumerate(_hits)
                if (h.get('detail') or {}).get('重点观察')
            ]
            if _focus_rows:
                sec_rows(f"{_label}·重点观察", _focus_rows)

    sec_rows("财务", [
        ("PE", fin.get("PE")),
        ("PB", fin.get("PB")),
        ("ROE", fin.get("roe")),
        ("股息", fin.get("divid_cash")),
        ("报告期", fin.get("report_date", "")),
    ])

    # 缠论摘要列（W3-A：chan 关时为空，不改变 score）
    chan = d.get("chan", {}) or {}
    if chan:
        c1 = chan.get("1d") or {}
        cw = chan.get("1w") or {}
        sec_rows("缠论结构", [
            ("日线末笔方向", "向上" if c1.get("last_bi_dir") == "up" else (
                "向下" if c1.get("last_bi_dir") == "down" else "-")),
            ("日线末笔确认", _flag(c1.get("last_bi_confirmed"))),
            ("日线在中枢内", _flag(c1.get("in_zs"))),
            ("现价vs末中枢", {"above": "中枢上方", "below": "中枢下方",
                            "in": "中枢区间内"}.get(c1.get("zs_position") or "", "-")),
            ("离开中枢方向", {"up": "向上离开", "down": "向下离开"}.get(
                c1.get("leave_zs") or "", "-")),
            ("末笔/中枢高度比", c1.get("bi_stretch")
             if c1.get("bi_stretch") is not None else "-"),
            ("中枢数(日线)", c1.get("n_zs", "-")),
            ("周线现价vs末中枢", "上方" if cw.get("zs_position") == "above" else (
                "下方" if cw.get("zs_position") == "below" else
                ("区间内" if cw.get("zs_position") == "in" else "-"))),
            ("czsc 买卖点/背驰", " / ".join(filter(None, [
                c1.get("bs_flag"), c1.get("divergence")])) or "-（未进综合分）"),
            # W34：日/周背驰共振（只展示不进分；core 纯函数单一实现）
            ("背驰共振", _bc_resonance(chan) or "-"),
            ("周线末笔方向", "向上" if cw.get("last_bi_dir") == "up" else (
                "向下" if cw.get("last_bi_dir") == "down" else "-")),
            ("czsc 版本", d.get("czsc_ver", "")),
        ])

    out: Dict[str, Any] = {}
    for sec_name, item, val in rows:
        if item in _DETAIL_SKIP:
            continue
        col = f"{_SECTION_PREFIX.get(sec_name, '')}{item}"
        out[col] = val
    return out


def _build_buffer(results: List[Dict[str, Any]]) -> "io.BytesIO":
    """生成 Excel 到内存 buffer（单「汇总报告」页，含全部个股详情列）。

    W14：取消个股详情 sheet 与代码列超链接/导航（无目标 sheet）；
    汇总报告 = 核心列 + 年线条件列 + 个股详情补充列。
    write_excel 与 excel_bytes 共用，保证 web 下载与 daily 落盘格式一致。
    """
    rows = [_flat(d) for d in results]
    if rows:
        rows.sort(key=lambda r: (r["score"] is not None, float(r["score"] or -1)),
                  reverse=True)
    # 所属自选板块：从 tdx 板块快照反查（与 Web 扫描结果表一致）。无论是否同步
    # tdx 板块，本列都存在（未同步时为空白），避免 DataFrame 列重排出现 NaN。
    from mystery.services import watchlist as _wl
    _snap = _wl.load_tdx_boards()
    for r in rows:
        r["board"] = ("·".join(_wl.boards_of_symbol(r.get("symbol", ""), _snap))
                      if _snap else "")
    df = pd.DataFrame(rows)
    # 固定列顺序：核心列 + 年线条件列 + 详情列（按出现顺序）
    col_order = [k for k, _ in SUMMARY_COLS]
    col_order += [f"yl:{k}" for k in YEARLINE_COND_KEYS]
    for k in df.columns:
        if k not in col_order:
            col_order.append(k)
    df = df[col_order]
    rename = dict(SUMMARY_COLS)
    rename.update({f"yl:{k}": f"年线:{k}" for k in YEARLINE_COND_KEYS})
    df = df.rename(columns=rename)

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="汇总报告", index=False)
        ws = writer.sheets["汇总报告"]
        # 表头加粗 + 冻结首行，列宽取 表头/值 长度（上限 26，避免明细列无限撑宽）
        from openpyxl.styles import Font
        for cell in ws[1]:
            cell.font = Font(bold=True)
        ws.freeze_panes = "A2"
        for col_idx, col in enumerate(df.columns, 1):
            letter = ws.cell(row=1, column=col_idx).column_letter
            max_len = max([len(str(col))] +
                          [len(str(v)) for v in df[col].tolist() if v is not None])
            ws.column_dimensions[letter].width = min(max(max_len, 8), 26)
    buf.seek(0)
    return buf


def excel_bytes(results: List[Dict[str, Any]]) -> bytes:
    """Web 下载用：AnalysisResult 列表 → xlsx bytes（格式同 write_excel）。"""
    return _build_buffer(results).getvalue()


def write_excel(results: List[Dict[str, Any]], path: str,
                qa_line: Optional[str] = None,
                major_indexes: Optional[List[Dict[str, Any]]] = None) -> str:
    """单「汇总报告」页（含全部个股详情列 + 年线条件列）。返回写入路径。

    qa_line：W26b 换手覆盖率 QA，写入表格下方一行（只读观测，不加列、不改排序）。
    major_indexes：W84 各大指数研判列表（analyze_major_indexes 产出），
    非空时追加「各大指数」工作表（只读展示）。
    """
    import os
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    buf = _build_buffer(results)
    with open(path, "wb") as f:
        f.write(buf.getvalue())
    # W69：组合总风险摘要追加到 QA 行（与 CLI / HTML / Web 同口径）
    from mystery.services.wave3_scan import wave3_summary_line
    w3_line = wave3_summary_line(results)
    if w3_line:
        qa_line = ((qa_line + "\n") if qa_line else "") + w3_line
    if qa_line:
        _append_qa_row(path, qa_line)
    # W84：各大指数分析独立工作表（只读展示，不改个股排序/列）。
    if major_indexes:
        _append_index_sheet(path, major_indexes)
    logger.info("✅ Excel 报告生成完成: %s（%d 只，单汇总页）", path, len(results))
    return path


def _append_index_sheet(path: str, indexes: List[Dict[str, Any]]) -> None:
    """W84：在报告末尾追加「各大指数」工作表（openpyxl 追加，失败不影响主体）。"""
    try:
        from openpyxl import load_workbook
        from openpyxl.styles import Font
        wb = load_workbook(path)
        ws = wb.create_sheet("各大指数")
        cols = ["指数", "代码", "最新收盘", "涨跌幅5日(%)", "涨跌幅20日(%)",
                "年线位置", "MA排列", "日K形态", "周K形态"]
        ws.append(cols)
        for c in ws[1]:
            c.font = Font(bold=True)
        for d in indexes:
            above = d.get("above_year")
            dist = d.get("year_dist_pct")
            year = ("站上年线" if above else "跌破年线") if above is not None \
                else "未知"
            if dist is not None:
                year += f"({dist:+.1f}%)"
            ma = d.get("ma_aligned")
            ma_s = "多头排列" if ma else ("非多头" if ma is not None else "未知")
            ws.append([
                d.get("name", ""), d.get("code", ""),
                d.get("close"), d.get("chg_5d"), d.get("chg_20d"),
                year, ma_s,
                (d.get("candle_daily") or "—"),
                (d.get("candle_weekly") or "—"),
            ])
        # 列宽自适应（上限 40，日/周形态较长）
        for col_idx, _ in enumerate(cols, 1):
            letter = ws.cell(row=1, column=col_idx).column_letter
            vals = [len(str(cols[col_idx - 1]))] + [
                len(str(r.value)) for r in ws[ws.max_row]
                if r.column == col_idx and r.value is not None]
            ws.column_dimensions[letter].width = min(max(vals or [8]), 40)
        wb.save(path)
    except Exception as e:  # noqa: BLE001 指数表失败不阻塞报告
        logger.warning("Excel 各大指数表写入失败（不影响报告主体）: %s", e)


def _append_qa_row(path: str, qa_line: str) -> None:
    """在「汇总报告」sheet 表格末行下方空一行写 QA（openpyxl 追加）。"""
    try:
        from openpyxl import load_workbook
        wb = load_workbook(path)
        ws = wb["汇总报告"]
        row = ws.max_row + 2
        # W27a：qa_line 可含 \n 多行（自选/全市场双口径），逐行写
        for ln in qa_line.split("\n"):
            ws.cell(row=row, column=1, value=ln)
            row += 1
        wb.save(path)
    except Exception as e:  # noqa: BLE001 QA 观测失败不阻塞报告
        logger.warning("Excel QA 行写入失败（不影响报告主体）: %s", e)
