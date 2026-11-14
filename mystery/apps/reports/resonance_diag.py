"""mystery.apps.reports.resonance_diag — 「为什么没有三振」六门闩诊断（W94/B）。

纯展示层：只读 ``scan_results.payload_json`` 里已有的 ``mystery.resonance``
中文键，还原 core ``calculate_resonance_score`` 的六道硬门闩并做全池聚合。
**不取数、不重算评分、不改判信号、不动 RULE_VER / 缓存键**。

背景（2026-11-14 定位）：真三振生产库全历史 0 次，根因不是 bug，而是
六门闩里的「大盘向上」要求 `all(gates)`；系统严格腿测「趋势确认后」
（`close>MA250 且 MA5>MA20>MA60>MA250`），而人工/社媒说的「三振」是
「大资金进场那一刻」的底部放量启动（1/14 主升、10/23 反转首日），
两者是同一现象的不同阶段 —— 即口径阶段差。本模块把这个差异摊开成
逐条门闩 + 大盘腿快照，供 Web / Excel / HTML 三处共用同一口径。

payload ``mystery.resonance`` 字段与六门闩的对应（见 core/mystery_rules.py
``three_resonance_analysis`` + core/resonance.py ``calculate_resonance_score``）::

    共振评分 >= 共振口径.真三振阈值   → 门闩「共振评分达标」
    资金活跃                          → 门闩「资金活跃」
    大盘趋势 (True=向上)              → 门闩「大盘向上」
    行业趋势 (True=向上)              → 门闩「行业向上」
    个股严格腿                        → 门闩「个股严格腿」
    大盘位置 != '高位'                → 门闩「大盘非高位」
    真三振                            → 交叉校验（all 六门闩）

旧 job / 异常 payload 无 resonance 字段时，门闩一律返回 ``None``（未知），
绝不把 unknown 当成 False 伪造否决。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# 纯函数模块不引入 logging / 不做 IO，异常由调用方（Web / CLI）兜底降级

# 六道硬门闩的稳定顺序 + 中文标签 + 人话解释（Web/Excel/HTML 共用列定义）
GATES: tuple = (
    ("score_ok", "共振评分达标", "四维共振评分 ≥ 生效阈值（个股30+大盘25+行业25+资金，含高位惩罚）"),
    ("capital_active", "资金活跃", "量比/成交额/换手达标 → 大资金进场的量能证据"),
    ("market_up", "大盘向上", "大盘腿判「向上」：严格口径需站上年线MA250 且 MA5>MA20>MA60>MA250"),
    ("industry_up", "行业向上", "所属行业板块同步走强"),
    ("stock_strict", "个股严格腿", "个股基础过滤 且 均线多头排列（评分里的个股共振=30）"),
    ("market_not_high", "大盘非高位", "大盘近120日价格分位 < 85%（高位追涨风控，一票否决）"),
)

GATE_KEYS: tuple = tuple(g[0] for g in GATES)


def resonance_of(d: Dict[str, Any]) -> Dict[str, Any]:
    """取单只 payload 的 ``mystery.resonance``（缺字段 → 空 dict）。"""
    if not isinstance(d, dict):
        return {}
    m = d.get("mystery")
    if not isinstance(m, dict):
        return {}
    r = m.get("resonance")
    return r if isinstance(r, dict) else {}


def row_gates(d: Dict[str, Any]) -> Dict[str, Optional[bool]]:
    """单只 payload → 六门闩 ``{key: True/False/None}``（None=数据缺失未知）。

    纯展示推导，字段全部来自已落库的 resonance，不重算评分不改判。
    ``score_ok`` 用 payload 自身记录的阈值（共振口径.真三振阈值），
    与当次扫描生效口径严格一致，不硬编码 85/80/70。
    """
    r = resonance_of(d)
    if not r:
        return {k: None for k in GATE_KEYS}
    thr = None
    kj = r.get("共振口径")
    if isinstance(kj, dict):
        try:
            thr = float(kj.get("真三振阈值"))
        except (TypeError, ValueError):
            thr = None
    score = r.get("共振评分")
    try:
        score = float(score) if score is not None else None
    except (TypeError, ValueError):
        score = None
    pos = r.get("大盘位置")

    def _b(v):
        # 缺失字段保持 None（unknown），不把 None 当 False
        if v is None:
            return None
        return bool(v)

    return {
        "score_ok": (score >= thr) if (score is not None and thr is not None) else None,
        "capital_active": _b(r.get("资金活跃")),
        "market_up": _b(r.get("大盘趋势")),
        "industry_up": _b(r.get("行业趋势")),
        "stock_strict": _b(r.get("个股严格腿")),
        "market_not_high": (pos != "高位") if pos is not None else None,
    }


def effective_options(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """从 results 里取本次扫描生效的共振口径（任一含 resonance 的行即可）。

    返回 ``{market_mode, true_threshold, capital_min_score,
    capital_min_volume_ratio, index_max_lag_days, sample_n}``；无数据返回空。
    """
    for d in rows or []:
        r = resonance_of(d)
        kj = r.get("共振口径")
        if isinstance(kj, dict):
            def _f(key, default=None):
                try:
                    v = kj.get(key)
                    return float(v) if v is not None else default
                except (TypeError, ValueError):
                    return default
            return {
                "market_mode": kj.get("大盘口径"),
                "true_threshold": _f("真三振阈值"),
                "capital_min_score": _f("资金门槛分"),
                "capital_min_volume_ratio": _f("资金量比门槛"),
                "index_max_lag_days": _f("指数滞后上限"),
            }
    return {}


def market_leg_snapshot(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """从全池结果聚合「大盘腿」快照（全池共用同一指数截面）。

    ``大盘趋势`` / ``大盘位置`` 在整个 job 内一致（都来自同一根指数 K 线），
    取第一条有效共振结果即可；同时统计降级/滞后诊断字段。
    """
    for d in rows or []:
        r = resonance_of(d)
        if not r:
            continue
        return {
            "market_up": bool(r.get("大盘趋势")),
            "position": r.get("大盘位置") or "未知",
            "lag_days": r.get("指数滞后天数"),
            "downgraded": bool(r.get("大盘腿降级")),
            "market_mode": (r.get("共振口径") or {}).get("大盘口径")
            if isinstance(r.get("共振口径"), dict) else None,
            "detail": r.get("详情") or [],
        }
    return {"market_up": False, "position": "未知", "lag_days": None,
            "downgraded": False, "market_mode": None, "detail": []}


def diagnose(rows: List[Dict[str, Any]], *,
             indexes: Optional[List[Dict[str, Any]]] = None,
             as_of: str = "") -> Dict[str, Any]:
    """全池六门闩聚合诊断（Web / Excel / HTML 三处共用的唯一取数口径）。

    :param rows: 个股 ``scan_results`` payload 列表（可含指数行，会自动剔除）
    :param indexes: W84/W92 的 8 大指数研判裸 dict 列表（可选，用于大盘腿
           可解释性展示：站上年线/多头排列家数、距年线百分比）
    :param as_of: 历史回放日期（非空时标注为回放口径）
    :return: dict，含 n_total / n_true / gates 统计 / three_legs_but_market /
            market_leg / options / indexes_view / conclusion / stage_note
    """
    stocks = [d for d in (rows or [])
              if isinstance(d, dict) and not d.get("is_index")]
    n = len(stocks)
    n_true = sum(1 for d in stocks if d.get("true_resonance"))

    # 逐门闩：通过 / 否决 / 未知
    passed = {k: 0 for k in GATE_KEYS}
    failed = {k: 0 for k in GATE_KEYS}
    unknown = {k: 0 for k in GATE_KEYS}
    three_legs_but_market = 0       # 个股+行业+资金 三腿全过，被大盘腿否决
    all_but_market = 0              # 除大盘向上外五门闩全过
    score_max = None
    for d in stocks:
        g = row_gates(d)
        for k in GATE_KEYS:
            v = g.get(k)
            if v is None:
                unknown[k] += 1
            elif v:
                passed[k] += 1
            else:
                failed[k] += 1
        legs = (g.get("stock_strict") and g.get("industry_up")
                and g.get("capital_active"))
        if legs and g.get("market_up") is False:
            three_legs_but_market += 1
        others = all(g.get(k) is True for k in GATE_KEYS if k != "market_up")
        if others and g.get("market_up") is False:
            all_but_market += 1
        r = resonance_of(d)
        try:
            sc = float(r.get("共振评分")) if r.get("共振评分") is not None else None
        except (TypeError, ValueError):
            sc = None
        if sc is not None and (score_max is None or sc > score_max):
            score_max = sc

    gates_view = []
    for key, label, why in GATES:
        gates_view.append({
            "key": key, "name": label, "why": why,
            "passed": passed[key], "failed": failed[key],
            "unknown": unknown[key],
            "pass_rate": (round(passed[key] / n * 100, 1) if n else 0.0),
        })

    opts = effective_options(stocks)
    mleg = market_leg_snapshot(stocks)

    # 8 大指数可解释性快照（来自 index_payloads 裸 dict）
    indexes_view = []
    for d in indexes or []:
        indexes_view.append({
            "name": d.get("name") or d.get("指数") or "—",
            "code": d.get("code") or "—",
            "close": d.get("close"),
            "year": ("站上年线" if d.get("above_year") else "跌破年线")
            if d.get("above_year") is not None else "未知",
            "year_dist_pct": d.get("year_dist_pct"),
            "ma": ("多头排列" if d.get("ma_aligned") else "非多头")
            if d.get("ma_aligned") is not None else "未知",
        })
    n_above = sum(1 for d in (indexes or []) if d.get("above_year"))
    n_aligned = sum(1 for d in (indexes or []) if d.get("ma_aligned"))

    thr = opts.get("true_threshold")
    gap = (round(thr - score_max, 1)
           if (thr is not None and score_max is not None) else None)

    # 结论（人话，讲清"系统测什么 / 人工看什么"的阶段差）
    mode = mleg.get("market_mode") or opts.get("market_mode") or "yearline"
    mode_cn = "严格年线腿(MA250+多头顺次排列)" if mode == "yearline" \
        else "MA20/MA60 腿"
    tag = f"【历史回放 {as_of}】" if as_of else ""
    if n_true:
        conclusion = (f"{tag}本期 {n} 只个股中真三振 {n_true} 只。"
                      "六门闩全部成立，大级别共振窗口出现。")
    else:
        mkt_failed = failed.get("market_up", 0)
        conclusion = (
            f"{tag}本期 {n} 只个股，真三振 0 只。"
            f"大盘腿「{mode_cn}」判 {mleg.get('position')}"
            f"（向上={mleg.get('market_up')}），"
            f"「大盘向上」门闩否决 {mkt_failed} 只；"
            f"其中个股+行业+资金三腿全过、仅被大盘否决的有 "
            f"{three_legs_but_market} 只。")

    stage_note = (
        "系统严格腿测「趋势确认后」：需指数站上年线 MA250 且均线多头顺次排列，"
        "在底部放量启动/反转首日数学上不会满足；人工所说的『三振』是"
        "「大资金进场那一刻」的集体上涨起点（如 1/14 主升、10/23 反转首日）。"
        "两者是同一行情的不同阶段——启动日大盘腿仍在年线下，趋势确认时行情已走一段。"
        "用 `czsc-mi scan --as-of YYYY-MM-DD` 可把当日截面完整回放自证。")

    return {
        "n_total": n, "n_true": n_true,
        "gates": gates_view,
        "three_legs_but_market": three_legs_but_market,
        "all_but_market": all_but_market,
        "market_leg": mleg,
        "options": opts,
        "score_max": score_max,
        "gap_to_threshold": gap,
        "indexes": indexes_view,
        "n_index_above": n_above,
        "n_index_aligned": n_aligned,
        "n_index_total": len(indexes or []),
        "conclusion": conclusion,
        "stage_note": stage_note,
        "as_of": as_of,
    }


def diag_sheet_rows(diag: Dict[str, Any]) -> List[List[Any]]:
    """诊断结论 → Excel「三振诊断」工作表的二维行（表头 + 区块）。

    与 Web 表格 / HTML 区块共用 ``diagnose()`` 的口径，避免三处漂移。
    """
    out: List[List[Any]] = [
        ["项目", "值", "说明"],
        ["扫描个股数", diag.get("n_total"), ""],
        ["真三振数", diag.get("n_true"), "六门闩 all() 成立"],
        ["回放日期", diag.get("as_of") or "（实时）", "--as-of 时按历史截面回放"],
        ["大盘腿口径", (diag.get("market_leg") or {}).get("market_mode") or "—",
         "yearline=严格年线腿 / ma=MA20/MA60"],
        ["大盘趋势向上", (diag.get("market_leg") or {}).get("market_up"), ""],
        ["大盘位置", (diag.get("market_leg") or {}).get("position"), "近120日价格分位"],
        ["生效真三振阈值", (diag.get("options") or {}).get("true_threshold"), ""],
        ["全池最高共振评分", diag.get("score_max"),
         f"距阈值还差 {diag.get('gap_to_threshold')} 分"
         if diag.get("gap_to_threshold") is not None else ""],
        ["三腿全过·仅被大盘否决", diag.get("three_legs_but_market"),
         "个股严格腿+行业向上+资金活跃，但大盘向上=False"],
        ["8大指数站上年线", f"{diag.get('n_index_above')}/{diag.get('n_index_total')}", ""],
        ["8大指数多头排列", f"{diag.get('n_index_aligned')}/{diag.get('n_index_total')}", ""],
        [],
        ["结论", diag.get("conclusion"), ""],
        ["口径阶段差", diag.get("stage_note"), ""],
        [],
        ["门闩", "通过数", "否决数", "未知数", "通过率(%)", "判据说明"],
    ]
    for g in diag.get("gates", []):
        out.append([g["name"], g["passed"], g["failed"], g["unknown"],
                    g["pass_rate"], g["why"]])
    if diag.get("indexes"):
        out.append([])
        out.append(["指数", "代码", "最新收盘", "年线位置", "距年线(%)", "MA排列"])
        for i in diag["indexes"]:
            out.append([i["name"], i["code"], i["close"], i["year"],
                        i["year_dist_pct"], i["ma"]])
    return out
