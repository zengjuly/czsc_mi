"""mystery.apps.reports.html_report — AnalysisResult 列表 → 自包含 HTML。

函数签名（002.md W1-A）::

    def write_html(results: list[dict], path: str) -> str

只用模板字符串，不引入新依赖。所有判断都带具体数值（WHY 信息）。
"""
from __future__ import annotations

import html as _html
import logging
from datetime import datetime
from typing import Any, Dict, List

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
footer { color: #aaa; font-size: .8em; text-align: center; margin-top: 20px; }
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
    if cyc.get("avg_turnover") is not None and float(cyc["avg_turnover"]) < 2.0:
        labels.append('<span class="signal-tag">筹码低位</span>')
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
        ("POC", _fmt(vap.get("POC"))),
        ("PE", _fmt(fin.get("PE"))),
        ("PB", _fmt(fin.get("PB"))),
        ("ROE", _fmt(fin.get("roe"))),
    ]
    metric_html = "".join(
        f'<div class="metric"><div class="label">{k}</div>'
        f'<div class="value">{v}</div></div>' for k, v in metrics
    )

    reasons: List[str] = []
    for line in (main_wave.get("判定依据") or [])[:4]:
        reasons.append(line)
    for line in (vap.get("详情") or [])[:3]:
        reasons.append(line)
    for line in (res.get("详情") or [])[:4]:
        reasons.append(line)
    reason_html = "".join(f"<li>{_esc(x)}</li>" for x in reasons)

    chan = d.get("chan", {}) or {}
    chan_html = ""
    if chan:
        c1 = chan.get("1d") or {}
        cw = chan.get("1w") or {}
        chan_html = (
            f"<p>缠论：日线末笔{'向上' if c1.get('last_bi_dir') == 'up' else '向下'}"
            f"{'（已确认）' if c1.get('last_bi_confirmed') else '（未确认）'} · "
            f"{'中枢内' if c1.get('in_zs') else '中枢外'} | "
            f"周线末笔{'向上' if cw.get('last_bi_dir') == 'up' else '向下'}"
            f"{'（已确认）' if cw.get('last_bi_confirmed') else '（未确认）'} · "
            f"czsc {_esc(d.get('czsc_ver', ''))}</p>"
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
  {chan_html}
  <details class="detail"><summary>判定依据（{len(reasons)} 条）</summary><ul>{reason_html}</ul></details>
</div>"""


def write_html(results: List[Dict[str, Any]], path: str) -> str:
    """汇总 HTML（按分降序，每只一张卡片）。返回写入路径。"""
    import os
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    ordered = sorted(results,
                     key=lambda d: (d.get("score") is not None,
                                    float(d.get("score") or -1)),
                     reverse=True)
    cards = "".join(_stock_card(d) for d in ordered)
    n_true = sum(1 for d in ordered if d.get("true_resonance"))
    doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>每日股票分析报告</title>{_CSS}</head>
<body><div class="container">
<div class="header"><h1>📈 每日股票分析报告</h1>
<p>{len(ordered)} 只 · 真三振 {n_true} 只 · 生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}</p></div>
{cards}
<footer>Mistery 趋势交易分析 · czsc_mi · 分数来自 mystery.services.analyze.analyze_one_stock</footer>
</div></body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    logger.info("✅ HTML 报告生成完成: %s（%d 只）", path, len(ordered))
    return path
