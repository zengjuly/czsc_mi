"""mystery.apps.reports.html_report — AnalysisResult 列表 → 自包含 HTML。

函数签名（002.md W1-A）::

    def write_html(results: list[dict], path: str) -> str

只用模板字符串，不引入新依赖。所有判断都带具体数值（WHY 信息）。
"""
from __future__ import annotations

import html as _html
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from mystery.core.bc_resonance import bc_resonance as _bc_resonance
from mystery.apps.reports import result_fields as _rf

logger = logging.getLogger(__name__)

_CSS = """
<style>
body { font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif; margin: 0;
       padding: 20px; background: #f5f6fa; color: #2f3542; }
.container { max-width: 1200px; margin: 0 auto; }
.header { background: linear-gradient(135deg, #667eea, #764ba2); color: #fff;
          padding: 18px 24px; border-radius: 10px; margin-bottom: 24px; }
.header h1 { margin: 0; font-size: 1.6em; }
.header p { margin: 6px 0 0; opacity: .9; }
.card { background: #fff; border-radius: 10px; padding: 18px 22px;
        margin-bottom: 18px; box-shadow: 0 2px 8px rgba(0,0,0,.08);
        border-left: 4px solid #667eea; }
.card.strong { border-left-color: #4caf50; }
.card.weak { border-left-color: #f44336; }
.stock-head { display: flex; justify-content: space-between; align-items: center;
              flex-wrap: wrap; gap: 8px; }
.stock-head h3 { margin: 0; }
.code { color: #888; font-size: .85em; }
.badge { padding: 6px 14px; border-radius: 20px; font-weight: bold; color: #fff; }
.badge.high { background: #4caf50; } .badge.mid { background: #ff9800; }
.badge.low { background: #f44336; }
.metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
           gap: 10px; margin: 14px 0; }
.metric { background: #f8f9fa; padding: 10px; border-radius: 8px; text-align: center; }
.metric .label { color: #888; font-size: .78em; }
.metric .value { font-size: 1.15em; font-weight: 600; margin-top: 2px; }
table { border-collapse: collapse; width: 100%; font-size: .9em; }
th, td { border: 1px solid #e3e6ee; padding: 6px 10px; text-align: left; }
th { background: #f0f2f9; }
.detail { margin-top: 12px; }
.detail summary { cursor: pointer; color: #667eea; font-weight: 600; }
.signal-tag { display: inline-block; background: #e8f5e9; color: #2e7d32;
              border-radius: 12px; padding: 2px 10px; margin-right: 6px; font-size: .82em; }
.signal-tag.pat { background: #e3f2fd; color: #1565c0; }
.signal-tag.warn { background: #ffebee; color: #c62828; }
footer { color: #aaa; font-size: .8em; text-align: center; margin-top: 20px; }
.qa { color: #667; font-size: .85em; background: #f4f5fb; border-radius: 6px; padding: 6px 10px; }
.idx-table { margin-top: 10px; }
.idx-table th { background: #eef1fb; }
.up { color: #2e7d32; font-weight: 600; }
.down { color: #c62828; font-weight: 600; }
</style>
"""


def _esc(v: Any) -> str:
    return _html.escape("" if v is None else str(v))


def _fmt(v: Any, nd: int = 2) -> str:
    if v is None:
        return "-"
    try:
        return f"{float(v):.{nd}f}"
    except Exception:
        return _esc(v)


def _badge(score: Any) -> str:
    try:
        s = float(score)
    except Exception:
        s = -1
    cls = "low" if s < 40 else ("mid" if s < 70 else "high")
    return f'<span class="badge {cls}">{_fmt(s)}</span>'


def _pattern_blocks(pat: Dict[str, Any]):
    """G4：从 mystery.patterns 生成 (tags, reasons)。

    tags = HTML 标签串列表（signal-tag 样式）；reasons = 判定依据行列表。
    """
    tags: List[str] = []
    reasons: List[str] = []
    if not pat:
        return tags, reasons
    eng = pat.get("强势反包阳") or {}
    gap = pat.get("缺口") or {}
    low = pat.get("低位地量") or {}
    if eng.get("hit"):
        tags.append('<span class="signal-tag pat">强势反包阳</span>')
        det = eng.get("detail", {}) or {}
        reasons.append(f"强势反包阳：昨阴 {det.get('昨阴', '')} → 今阳 "
                       f"{det.get('今阳', '')}（{det.get('强度', '')}）")
    gt = gap.get("gap_type")
    if gap.get("gap_up"):
        if gt == "衰竭":
            tags.append('<span class="signal-tag warn">缺口·衰竭(卖)</span>')
        else:
            tags.append(f'<span class="signal-tag pat">{_esc("缺口·" + str(gt or ""))}</span>')
        det = gap.get("detail", {}) or {}
        fill = "（三日不补✅）" if det.get("三日不补") else ""
        reasons.append(f"向上跳空缺口[{gt}]：幅度 {gap.get('gap_magnitude_pct')}%{fill}")
    if low.get("hit"):
        tags.append('<span class="signal-tag pat">低位地量</span>')
        det = low.get("detail", {}) or {}
        reasons.append(f"低位地量：回撤 {det.get('回撤%')}% · "
                       f"量/60日均量 {det.get('量/60日均量')}")
    return tags, reasons


def _stock_card(d: Dict[str, Any]) -> str:
    m = d.get("mystery", {}) or {}
    vap = m.get("vap_atr", {}) or {}
    plat = m.get("platform", {}) or {}
    main_wave = m.get("main_wave", {}) or {}
    res = m.get("resonance", {}) or {}
    cl = m.get("checklist8", {}) or {}
    fin = d.get("financial", {}) or {}
    sec = d.get("sector", {}) or {}
    pr = vap.get("平台范围") or {}
    tr = bool(d.get("true_resonance"))
    card_cls = "strong" if tr else ""
    labels = []
    if tr:
        labels.append('<span class="signal-tag">真三振</span>')
    if vap.get("突破信号") or plat.get("突破信号"):
        labels.append('<span class="signal-tag">VAP-ATR突破</span>')
    cyc = vap.get("自适应周期") or {}
    # W80：筹码低位标签改用 payload 的 chip_low 判定（与 Excel/扫描同源），
    # 不再用 cyc.avg_turnover<2 的旧口径；换手缺失单独标注，不当低位。
    if d.get("chip_low"):
        labels.append('<span class="signal-tag">筹码低位</span>')
    elif d.get("chip_low_unknown"):
        labels.append('<span class="signal-tag">换手缺失</span>')
    elif d.get("chip_quiet"):
        labels.append('<span class="signal-tag">高位缩量</span>')
    # W32b：换手分档 + 风格标签（只读 core 纯函数，缺 turn → 未知）
    from ...core.turnover_tag import tags_from_result
    _tg = tags_from_result(d)
    # G4：高级形态（强势反包阳/缺口/低位地量）标签 —— 来自 mystery.patterns
    _pat = m.get("patterns", {}) or {}
    _pat_tags, _pat_reasons = _pattern_blocks(_pat)
    labels.extend(_pat_tags)
    # W69：三浪主升候选用标签直接标出（只读 payload 附加字段）
    if (d.get("wave3") or {}).get("verdict") == "候选":
        labels.append('<span class="signal-tag">三浪候选</span>')
    # W82：典型 K 线组合形态标签（日 K / 周 K 分开，只看 payload 不重算）
    if d.get("candlestick"):
        for _per, _plabel in (("daily", "日K"), ("weekly", "周K")):
            for _h in _rf.candlestick_hits(d, _per, limit=2):
                _cls = "warn" if _h.get("direction") == "看跌" else "pat"
                labels.append(
                    f'<span class="signal-tag {_cls}">'
                    f'{_esc(_plabel + "·" + str(_h.get("name", "")))}</span>')
    tags = "".join(labels)

    metrics = [
        ("综合评分", _fmt(d.get("score"))),
        ("操作建议", _esc(d.get("advice", "-"))),
        ("最新价", _fmt(d.get("price"))),
        ("行业", _esc(sec.get("行业名称", "-"))),
        ("主升浪", _esc(main_wave.get("主升浪状态", "-"))),
        ("主升浪满足", f"{cl.get('满足数量', 0)}/8"),
        ("平台", _esc(plat.get("平台状态", "-"))),
        ("VAP-ATR上轨", _fmt(vap.get("自适应上轨") or pr.get("上沿"))),
        # W80：turnover_20 数值 + chip 三态标签 + 换手来源（只读 payload 顶层）
        ("近20日均换手(%)", _fmt(_rf.turnover_20_value(d))),
        ("筹码低位", _esc(_rf.chip_low_text(d))),
        ("筹码标签", _esc(_rf.chip_tag_text(d))),
        ("换手来源", _esc(_rf.turnover_source_text(d))),
        ("换手分档", _esc(_tg["turnover_tag"])),
        ("风格", _esc(_tg["style_tag"])),
        # W82：日 K / 周 K 形态（只读 payload 附加字段）
        ("日K形态", _esc(_rf.candlestick_daily_text(d))),
        ("周K形态", _esc(_rf.candlestick_weekly_text(d))),
        ("形态方向", _esc(_rf.candlestick_direction_text(d))),
        # W83：多方炮重点观察结论汇总（日 K 优先，见下方独立区块）
        ("形态重点观察", _esc(_rf.candlestick_focus_text(d))),
        # W86：主力建仓/吸筹研判（等级 + 阶段，见下方独立证据区块）
        ("主力吸筹", _esc(_rf.accumulation_text(d))),
        ("吸筹阶段", _esc(_rf.accumulation_stage_text(d))),
        ("POC", _fmt(vap.get("POC"))),
        ("PE", _fmt(fin.get("PE"))),
        ("PB", _fmt(fin.get("PB"))),
        ("ROE", _fmt(fin.get("roe"))),
    ]
    # W69：三浪主升逐票结论（旧任务无字段不追加，保持原卡片不变）
    w3 = d.get("wave3") or {}
    if w3:
        metrics.extend([
            ("三浪结论", _esc(str(w3.get("verdict") or "-"))),
            ("三浪阶段", _esc(str(w3.get("phase") or "-"))),
            ("多周期位置", _esc(str(w3.get("position") or "-"))),
            ("三浪铁律", _esc(str(w3.get("hard") or "-"))),
            ("三浪流程", _esc(f"第{w3['stage']}步" if w3.get("stage") else "-")),
            ("三浪买点", _fmt(w3.get("entry"))),
            ("三浪止损", _fmt(w3.get("stop"))),
            ("三浪建议(手)", _fmt(w3.get("lots"))),
        ])
    metric_html = "".join(
        f'<div class="metric"><div class="label">{k}</div>'
        f'<div class="value">{v}</div></div>' for k, v in metrics
    )

    # W83：多方炮变体「重点观察」独立区块（日 K / 周 K 分开，长文本完整展示）
    _fd = _rf.candlestick_focus_text(d, "daily")
    _fw = _rf.candlestick_focus_text(d, "weekly")
    _fl: List[str] = []
    if _fd != "—":
        _fl.append(f"<b>日 K</b>　{_esc(_fd)}")
    if _fw != "—" and _fw != _fd:
        _fl.append(f"<b>周 K</b>　{_esc(_fw)}")
    focus_html = ""
    if _fl:
        focus_html = (
            '<div style="margin:8px 0;padding:8px 12px;'
            'border-left:3px solid #c0392b;background:rgba(192,57,43,.06);'
            'font-size:13px;line-height:1.75">'
            '<div style="font-weight:600;margin-bottom:4px">'
            '多方炮重点观察（验证点量化结论，仅展示不进评分）</div>'
            + "<br>".join(_fl) + "</div>")

    # W86：主力建仓/吸筹四层证据链独立区块（低位/量能/阶段/年线 + 结论）
    _acc = _rf.accumulation_of(d)
    acc_html = ""
    if _acc and _acc.get("level") != "unknown":
        lv = _esc(_acc.get("level_cn") or "—")
        _ev = "；".join(_esc(x) for x in (_acc.get("evidence") or [])) or "—"
        _cc = _esc(_acc.get("conclusion") or "—")
        _ac = "#2e7d5b" if _acc.get("level") in ("strong", "medium") else \
              ("#c0392b" if _acc.get("level") == "none" else "#b8860b")
        acc_html = (
            '<div style="margin:8px 0;padding:8px 12px;'
            f'border-left:3px solid {_ac};background:rgba(46,125,91,.06);'
            'font-size:13px;line-height:1.75">'
            '<div style="font-weight:600;margin-bottom:4px">'
            f'主力建仓 / 吸筹研判：<span style="color:{_ac}">{lv}</span>'
            f'（阶段 {_esc(_acc.get("stage") or "—")}，仅展示不进评分）</div>'
            f'<div>证据：{_ev}</div><div>结论：{_cc}</div></div>')

    reasons: List[str] = []
    for line in (main_wave.get("判定依据") or [])[:4]:
        reasons.append(line)
    for line in (vap.get("详情") or [])[:3]:
        reasons.append(line)
    for line in (res.get("详情") or [])[:4]:
        reasons.append(line)
    # G4：高级形态判定依据（展示，不进综合分；轻加权开关另计）
    reasons.extend(_pat_reasons)
    reason_html = "".join(f"<li>{_esc(x)}</li>" for x in reasons)

    chan = d.get("chan", {}) or {}
    chan_html = ""
    if chan:
        c1 = chan.get("1d") or {}
        cw = chan.get("1w") or {}
        _pos_cn = {"above": "中枢上方", "below": "中枢下方",
                   "in": "中枢区间内"}.get(c1.get("zs_position") or "", "")
        _bs = " · ".join(filter(None, [c1.get("bs_flag"), c1.get("divergence")]))
        # W34：日/周背驰共振（只展示不进分）
        _bcr = _bc_resonance(chan)
        chan_html = (
            f"<p>缠论：日线末笔{'向上' if c1.get('last_bi_dir') == 'up' else '向下'}"
            f"{'（已确认）' if c1.get('last_bi_confirmed') else '（未确认）'} · "
            f"{'中枢内' if c1.get('in_zs') else '中枢外'}"
            f"{(' · ' + _pos_cn) if _pos_cn else ''}"
            f"{(' · 离开' + ('上' if c1.get('leave_zs') == 'up' else '下')) if c1.get('leave_zs') else ''}"
            f"{(' · 拉伸比' + str(c1.get('bi_stretch'))) if c1.get('bi_stretch') is not None else ''}"
            f"{(' · ' + _bs) if _bs else ''}"
            f"{(' · <b>' + _bcr + '</b>') if _bcr else ''}"
            f" | 周线末笔{'向上' if cw.get('last_bi_dir') == 'up' else '向下'}"
            f"{'（已确认）' if cw.get('last_bi_confirmed') else '（未确认）'} · "
            f"czsc {_esc(d.get('czsc_ver', ''))}"
            f"{'（结构摘要加深仅供展示，未进综合分）' if (_pos_cn or _bs) else ''}</p>"
        )

    return f"""
<div class="card {card_cls}">
  <div class="stock-head">
    <div>
      <h3>{_esc(d.get('name') or '未知')} <span class="code">{_esc(d.get('symbol', ''))}
        （{_esc(d.get('trade_date', ''))}）</span></h3>
      <div style="margin-top:6px">{tags}</div>
    </div>
    {_badge(d.get('score'))}
  </div>
  <div class="metrics">{metric_html}</div>
  {focus_html}
  {acc_html}
  {chan_html}
  <details class="detail"><summary>判定依据（{len(reasons)} 条）</summary><ul>{reason_html}</ul></details>
</div>"""


def write_html(results: List[Dict[str, Any]], path: str,
               qa_line: Optional[str] = None,
               major_indexes: Optional[List[Dict[str, Any]]] = None,
               focus_sectors: Optional[List[Dict[str, Any]]] = None,
               resonance_diag: Optional[Dict[str, Any]] = None) -> str:
    """汇总 HTML（按分降序，每只一张卡片）。返回写入路径。

    qa_line：W26b 换手覆盖率 QA，展示在头部统计行（只读观测）。
    major_indexes：W84 各大指数研判列表，非空时在个股卡片前渲染指数区块。
    focus_sectors：W85 关注行业板块研判列表，非空时渲染板块研判区块
    （含板块吸筹结论 + 强相关个股），位于指数区块之后、个股卡片之前。
    resonance_diag：W94/B 六门闩诊断 dict，非空时渲染「为什么没有三振」区块。
    """
    import os
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    ordered = sorted(results,
                     key=lambda d: (d.get("score") is not None,
                                    float(d.get("score") or -1)),
                     reverse=True)
    cards = "".join(_stock_card(d) for d in ordered)
    n_true = sum(1 for d in ordered if d.get("true_resonance"))
    qa_html = (f'<p class="qa">{qa_line.replace(chr(10), "<br>")}</p>'
               if qa_line else "")
    # W69：组合总风险摘要（与 CLI / Web 同口径；旧任务无字段则不显示）
    from mystery.services.wave3_scan import wave3_summary_line
    w3_line = wave3_summary_line(ordered)
    w3_html = f'<p class="qa">{_esc(w3_line)}</p>' if w3_line else ""
    # W84：各大指数区块（只读展示，位于个股卡片之前）
    idx_html = _index_block(major_indexes) if major_indexes else ""
    # W85：行业板块区块（板块研判 + 强相关个股）
    sec_html = _sector_block(focus_sectors) if focus_sectors else ""
    # W94/B：六门闩诊断区块（位于指数/板块之后、个股卡片之前）
    diag_html = _diag_block(resonance_diag) if resonance_diag else ""
    doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>每日股票分析报告</title>{_CSS}</head>
<body><div class="container">
<div class="header"><h1>📈 每日股票分析报告</h1>
<p>{len(ordered)} 只 · 真三振 {n_true} 只 · 生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
{qa_html}{w3_html}</div>
{idx_html}{sec_html}{diag_html}{cards}
<footer>Mistery 趋势交易分析 · czsc_mi · 分数来自 mystery.services.analyze.analyze_one_stock</footer>
</div></body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    logger.info("✅ HTML 报告生成完成: %s（%d 只）", path, len(ordered))
    return path


def _diag_block(diag: Dict[str, Any]) -> str:
    """W94/B：「为什么没有三振」六门闩诊断区块（只读展示，data 来自 diagnose）。"""
    try:
        mleg = diag.get("market_leg") or {}
        opts = diag.get("options") or {}
        tag = f"（历史回放 {_esc(diag['as_of'])}）" if diag.get("as_of") else ""
        rows = ""
        for g in diag.get("gates", []):
            ok = "up" if g["failed"] == 0 else "down"
            rows += (
                f"<tr><td>{_esc(g['name'])}</td>"
                f"<td class='up'>{g['passed']}</td>"
                f"<td class='{ok}'>{g['failed']}</td>"
                f"<td>{g['unknown']}</td>"
                f"<td>{g['pass_rate']}%</td>"
                f"<td>{_esc(g['why'])}</td></tr>")
        idx_rows = ""
        for i in diag.get("indexes", []):
            cls = "up" if i["year"] == "站上年线" else "down"
            idx_rows += (
                f"<tr><td>{_esc(i['name'])}</td><td>{_esc(str(i['code']))}</td>"
                f"<td>{i['close']}</td><td class='{cls}'>{i['year']}"
                f"{'' if i['year_dist_pct'] is None else format(i['year_dist_pct'], '+.1f') + '%'}</td>"
                f"<td>{i['ma']}</td></tr>")
        idx_tbl = (
            f"<p class='qa'>8 大指数：站上年线 "
            f"{diag.get('n_index_above')}/{diag.get('n_index_total')} · "
            f"多头排列 {diag.get('n_index_aligned')}/{diag.get('n_index_total')}</p>"
            f"<table class='idx-table'><thead><tr><th>指数</th><th>代码</th>"
            f"<th>最新</th><th>年线位置</th><th>MA排列</th></tr></thead>"
            f"<tbody>{idx_rows}</tbody></table>") if idx_rows else ""
        return f"""<div class="card">
<h3>🔍 为什么没有三振？六门闩诊断 {tag}</h3>
<p><b>{_esc(diag.get('conclusion') or '')}</b></p>
<p class="qa">大盘腿口径：{_esc(str(mleg.get('market_mode') or '—'))} ·
趋势向上={mleg.get('market_up')} · 位置={_esc(str(mleg.get('position') or '—'))} ·
生效阈值={opts.get('true_threshold')} · 全池最高共振评分={diag.get('score_max')}
（距阈值还差 {diag.get('gap_to_threshold')} 分） ·
三腿全过仅被大盘否决 {diag.get('three_legs_but_market')} 只</p>
{idx_tbl}
<table class="idx-table"><thead><tr><th>门闩</th><th>通过</th><th>否决</th>
<th>未知</th><th>通过率</th><th>判据</th></tr></thead>
<tbody>{rows}</tbody></table>
<p class="qa">{_esc(diag.get('stage_note') or '')}</p>
</div>"""
    except Exception as e:  # noqa: BLE001 诊断区块失败不影响报告主体
        return f"<div class='card'><p class='qa'>三振诊断区块渲染失败：{_esc(str(e)[:120])}</p></div>"


def _index_block(indexes: List[Dict[str, Any]]) -> str:
    """W84：各大指数研判区块 HTML（只读展示）。"""
    rows = ""
    for d in indexes:
        above = d.get("above_year")
        dist = d.get("year_dist_pct")
        ma = d.get("ma_aligned")
        year_cls = "up" if above else "down"
        year_txt = "站上年线" if above else "跌破年线"
        if dist is not None:
            year_txt += f"({dist:+.1f}%)"
        ma_txt = "多头排列" if ma else ("非多头" if ma is not None else "未知")
        acc_cls = "up" if (d.get("accumulation") or {}).get("level") in (
            "strong", "medium") else "down"
        rows += (
            f"<tr><td>{_esc(d.get('name', ''))}</td>"
            f"<td>{_esc(str(d.get('code', '')))}</td>"
            f"<td>{d.get('close')}</td>"
            f"<td>{d.get('chg_20d')}</td>"
            f"<td class='{year_cls}'>{year_txt}</td>"
            f"<td>{ma_txt}</td>"
            f"<td>{_esc(d.get('candle_daily') or '—')}</td>"
            f"<td>{_esc(d.get('candle_weekly') or '—')}</td>"
            f"<td class='{acc_cls}'>{_esc(d.get('accumulation_level') or '—')}</td></tr>"
        )
    return f"""<div class="card">
<h3>📊 各大指数研判（{len(indexes)}）</h3>
<table class="idx-table">
<thead><tr><th>指数</th><th>代码</th><th>最新</th><th>20日%</th>
<th>年线位置</th><th>MA排列</th><th>日K形态</th><th>周K形态</th>
<th>主力吸筹(W86)</th></tr></thead>
<tbody>{rows}</tbody></table></div>"""


def _sector_block(sectors: List[Dict[str, Any]]) -> str:
    """W85：关注行业板块研判区块 + 强相关个股（只读展示）。"""
    rows = ""
    detail = ""
    for d in sectors:
        above = d.get("above_year")
        dist = d.get("year_dist_pct")
        ma = d.get("ma_aligned")
        st = d.get("strength_score")
        year_cls = "up" if above else "down"
        year_txt = "站上年线" if above else "跌破年线"
        if dist is not None:
            year_txt += f"({dist:+.1f}%)"
        ma_txt = "多头" if ma else ("非多头" if ma is not None else "未知")
        st_txt = f"{st:.1f}" if st is not None else "—"
        acc = d.get("accumulation_level") or "—"
        acc_cls = "up" if (d.get("accumulation") or {}).get("level") in (
            "strong", "medium") else "down"
        rel = d.get("related") or []
        rel_txt = "、".join(
            f"{_esc(r.get('name') or '?')}({r.get('corr')})"
            for r in rel[:5]) or "—"
        rows += (
            f"<tr><td>{_esc(d.get('name', ''))}</td>"
            f"<td>{_esc(str(d.get('code', '')))}</td>"
            f"<td>{d.get('close')}</td>"
            f"<td>{d.get('chg_20d')}</td>"
            f"<td class='{year_cls}'>{year_txt}</td>"
            f"<td>{ma_txt}</td>"
            f"<td>{st_txt}</td>"
            f"<td class='{acc_cls}'>{_esc(acc)}</td>"
            f"<td>{len(rel)}</td>"
            f"<td>{rel_txt}</td></tr>"
        )
        ev = "；".join(_esc(x) for x in (
            (d.get("accumulation") or {}).get("evidence") or [])) or "—"
        cc = _esc((d.get("accumulation") or {}).get("conclusion") or "—")
        if rel:
            rel_rows = "".join(
                f"<tr><td>{_esc(r.get('name') or '')}</td>"
                f"<td>{_esc(str(r.get('symbol') or ''))}</td>"
                f"<td>{r.get('corr')}</td>"
                f"<td>{'是' if r.get('analyzed') else '否'}</td>"
                f"<td>{r.get('score') if r.get('score') is not None else '—'}</td>"
                f"<td>{_esc(r.get('advice') or '—')}</td>"
                f"<td>{_esc(r.get('accumulation_level') or '—')}</td></tr>"
                for r in rel)
            detail += (
                f"<details class='detail'><summary>{_esc(d.get('name', ''))} "
                f"· 吸筹证据与 {len(rel)} 只强相关个股</summary>"
                f"<p><b>吸筹证据</b>：{ev}<br><b>结论</b>：{cc}</p>"
                f"<table class='idx-table'><thead><tr><th>个股</th><th>代码</th>"
                f"<th>相关系数</th><th>本期已分析</th><th>评分</th>"
                f"<th>操作建议</th><th>吸筹</th></tr></thead>"
                f"<tbody>{rel_rows}</tbody></table></details>")
    return f"""<div class="card">
<h3>🏭 行业板块研判（关注 {len(sectors)} 个 · 真实板块指数K线）</h3>
<table class="idx-table">
<thead><tr><th>板块</th><th>代码</th><th>最新</th><th>20日%</th>
<th>年线位置</th><th>MA排列</th><th>强度分</th>
<th>主力吸筹(W86)</th><th>强相关数</th><th>强相关个股(前5)</th></tr></thead>
<tbody>{rows}</tbody></table>
{detail}</div>"""
