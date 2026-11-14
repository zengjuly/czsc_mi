from __future__ import annotations
import logging
from datetime import datetime

import streamlit as st
from .components import *
# W64：拆分后跨 view 复用函数需显式导入（原单文件 app.py 同命名空间可直接调）。
from .stock import _add_watchlist_widget
# W69：三浪主升组合摘要（与 CLI / 日报同一口径）；绝对导入避免悬空全局名。
from mystery.services.wave3_scan import wave3_summary_line
# W83：形态「重点观察」结论只读 payload（与 Excel/HTML 报告同一口径）
from mystery.apps.reports import result_fields as _rf

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# W92：大盘指数 / 行业板块的完整分析区块（纯展示层）
#
# 背景：W84 把 8 大指数以 ``is_index=True`` 并入 ``scan_results``，但它们被
# 当成个股行塞进 130+ 列的个股大表 —— 除「指数研判 / 主力吸筹」外上百列全
# 空；名称上的详情链接又指向个股详情页，指数取不到个股 K 线 → 一片空白。
# W89 阶段② 则把板块研判写进 JSONL 分片、明确不进 ``scan_results``，Web 结果
# 页从不读分片，于是「没有板块的分析」。
#
# 解法：两类对象各自摊开成独立、完整、可读的区块；个股大表只保留个股行；
# Web 下载的 Excel 同步追加「各大指数」「行业板块」两个工作表。
# 不取数重算个股评分、不改信号 / 规则 / RULE_VER / 缓存键。
# ---------------------------------------------------------------------------

def split_scan_rows(rows: list) -> tuple:
    """W92：把指数行（``is_index=True``）与个股行分离（纯函数，输入不变）。"""
    stock_rows: list = []
    index_rows: list = []
    for r in rows or []:
        if isinstance(r, dict) and r.get("is_index"):
            index_rows.append(r)
        else:
            stock_rows.append(r)
    return stock_rows, index_rows


def _fmt_market_num(v, suffix: str = "") -> str:
    """W92：展示层数值格式化（统一 str，避免 pyarrow 数值/字符串混列告警）。"""
    if v is None:
        return "—"
    try:
        return f"{float(v):.2f}{suffix}"
    except (TypeError, ValueError):
        return str(v)


def market_index_view(indexes: list) -> list:
    """W92：各大指数研判 → Web 展示行（完整字段，不再淹没在个股大表里）。"""
    from mystery.apps.reports.excel_report import index_payloads
    out = []
    for d in index_payloads(indexes):
        above, dist = d.get("above_year"), d.get("year_dist_pct")
        year = "未知" if above is None else ("站上年线" if above else "跌破年线")
        if above is not None and dist is not None:
            try:
                year += f"({float(dist):+.1f}%)"
            except (TypeError, ValueError):
                pass
        ma = d.get("ma_aligned")
        ma_s = "未知" if ma is None else ("多头排列" if ma else "非多头")
        acc = d.get("accumulation") or {}
        out.append({
            "指数": str(d.get("name") or "—"),
            "代码": str(d.get("code") or "—"),
            "数据日期": str(d.get("date") or "—"),
            "最新收盘": _fmt_market_num(d.get("close")),
            "近5日": _fmt_market_num(d.get("chg_5d"), "%"),
            "近20日": _fmt_market_num(d.get("chg_20d"), "%"),
            "年线位置": year,
            "MA排列": ma_s,
            "日K形态": str(d.get("candle_daily") or "—"),
            "周K形态": str(d.get("candle_weekly") or "—"),
            "主力吸筹": str(d.get("accumulation_level") or "—"),
            "吸筹结论": str(acc.get("conclusion") or "—"),
        })
    return out


def market_sector_view(sectors: list) -> list:
    """W92：行业板块研判 → Web 展示行（复用 W87 字段映射，与日报同口径）。"""
    from .sector_fields import sector_drilldown_view
    out = []
    for d in sectors or []:
        if not isinstance(d, dict):
            continue
        v = sector_drilldown_view(d)
        out.append({
            "板块": v["name"], "代码": v["code"], "日期": v["date"],
            "最新": v["close"], "近5日": v["chg_5d"], "近20日": v["chg_20d"],
            "年线位置": v["year"], "MA排列": v["ma"],
            "板块强度": v["strength"],
            "日K形态": v["candle_daily"], "周K形态": v["candle_weekly"],
            "主力吸筹": v["acc_level"], "吸筹阶段": v["acc_stage"],
            "强相关个股数": len(d.get("related") or []),
        })
    return out


def _sector_opts() -> dict:
    """W92：读取生效的板块配置（异常降级到生产默认，不阻塞页面）。"""
    try:
        from mystery.services import runtime_settings as rts
        return rts.sector_options_normalized()
    except Exception:  # noqa: BLE001
        return {"enabled": True, "focus": "auto", "top_n": 8,
                "corr_window": 60, "auto_max": 8}


def _sector_cache_key(opts: dict) -> tuple:
    """W92：把板块配置拍平成 ``st.cache_data`` 可哈希的 key。

    ``focus`` 手动多选时是 list（不可哈希），编码成 ``list:a,b`` 字符串，
    缓存内部再还原，保证配置改动能立即让缓存失效。
    """
    f = opts.get("focus", "auto")
    fk = ("list:" + ",".join(str(x) for x in f)) \
        if isinstance(f, (list, tuple)) else str(f)
    try:
        top_n = int(opts.get("top_n", 8))
        corr = int(opts.get("corr_window", 60))
        amax = int(opts.get("auto_max", 8))
    except (TypeError, ValueError):
        top_n, corr, amax = 8, 60, 8
    return fk, bool(opts.get("enabled", True)), top_n, corr, amax


@st.cache_data(ttl=600, show_spinner=False)
def _market_context_cached(trade_date: str, focus_key: str, enabled: bool,
                           top_n: int, corr_window: int,
                           auto_max: int) -> dict:
    """W92：各大指数 + 关注板块研判（只读，10 分钟缓存，异常硬隔离）。

    与日报 ``_cmd_daily`` / W87 板块钻取同一口径：``analyze_major_indexes``
    + ``analyze_focus_sectors``。参数全部可哈希，避免每次 rerun 重取指数 K
    线、重算板块 Pearson 相关矩阵。指数/板块失败都不阻塞个股结果展示。
    """
    ctx = {"indexes": [], "sectors": []}
    try:
        from mystery.services.index_analysis import analyze_major_indexes
        ctx["indexes"] = analyze_major_indexes(_service().market)
    except Exception as exc:  # noqa: BLE001 指数失败不阻塞板块与个股
        logger.warning("W92 各大指数研判失败: %s", str(exc)[:120])
    if not enabled:
        return ctx
    try:
        from mystery.services.sector_analysis import analyze_focus_sectors
        focus = (focus_key[5:].split(",") if focus_key.startswith("list:")
                 else focus_key)
        opts = {"enabled": True, "focus": focus, "top_n": top_n,
                "corr_window": corr_window, "auto_max": auto_max}
        ctx["sectors"] = analyze_focus_sectors(_service().market.db, opts=opts)
    except Exception as exc:  # noqa: BLE001 板块失败不阻塞个股结果
        logger.warning("W92 行业板块研判失败: %s", str(exc)[:120])
    return ctx


def _market_context(rows: list) -> dict:
    """W92：按当前 rows 的交易日 + 生效配置取大盘/板块上下文（命中缓存）。"""
    td = ""
    for r in rows or []:
        if isinstance(r, dict) and r.get("trade_date"):
            td = str(r["trade_date"])[:10]
            break
    return _market_context_cached(td, *_sector_cache_key(_sector_opts()))


def _render_market_overview(rows: list, key: str = "scan") -> dict:
    """W92：结果表上方「各大指数研判」「行业板块研判」两个完整区块。

    返回 ``{'stock_rows', 'indexes', 'sectors', 'index_rows'}``，供结果表
    与 Excel 下载复用同一份上下文（一次取数、两处展示）。
    """
    from mystery.apps.reports.excel_report import index_payloads
    stock_rows, index_rows = split_scan_rows(rows)
    ctx = _market_context(rows)
    # 指数优先用本 job 已落库的快照（与所选任务一致），缓存现算兜底：
    # 信号过滤后 rows 可能不含指数行，此时仍展示完整的各大指数研判。
    indexes = index_payloads(index_rows) or list(ctx.get("indexes") or [])
    sectors = list(ctx.get("sectors") or [])

    st.subheader("📊 各大指数研判")
    iv = market_index_view(indexes)
    if iv:
        st.caption(f"共 {len(iv)} 只宽基指数 · 年线位置 / MA 排列 / 日周 K 形态 / "
                   "主力吸筹（与 Excel「各大指数」工作表同口径）")
        st.dataframe(iv, use_container_width=True, hide_index=True,
                     key=f"{key}_index_view")
    else:
        st.info("暂无指数研判数据（指数 K 线未同步），个股结果不受影响。")

    st.subheader("🏭 行业板块研判")
    sv = market_sector_view(sectors)
    if sv:
        o = _sector_opts()
        st.caption(f"关注 {len(sv)} 个板块 · 真实板块指数 K 线 · "
                   f"相关窗口 {o.get('corr_window', 60)} 日 / "
                   f"Top {o.get('top_n', 8)}（与 Excel「行业板块」工作表同口径）")
        st.dataframe(sv, use_container_width=True, hide_index=True,
                     key=f"{key}_sector_view")
        from .sector_fields import related_stock_rows
        details = []
        for d in sectors:
            for r in related_stock_rows(d.get("related"), stock_rows):
                details.append({"板块": str(d.get("name") or "—"), **r})
        if details:
            with st.expander(f"展开 {len(details)} 条板块强相关个股明细",
                             expanded=False):
                st.dataframe(details, use_container_width=True,
                             hide_index=True, key=f"{key}_sector_rel")
    else:
        st.info("暂无板块研判数据（板块关注配置关闭或 sector_kline 未同步），"
                "个股结果不受影响。")

    # W94/B：「为什么没有三振」六门闩诊断（只读 payload，不取数不改判）
    diag = _render_resonance_diag(rows, indexes, key=key)

    return {"stock_rows": stock_rows, "indexes": indexes,
            "sectors": sectors, "index_rows": index_rows,
            "resonance_diag": diag}


def _render_resonance_diag(rows: list, indexes: list, key: str = "scan") -> dict:
    """W94/B：结果表上方「为什么没有三振」六门闩诊断区块。

    纯展示层：``resonance_diag.diagnose()`` 只读 payload 里已落库的
    ``mystery.resonance`` 六门闩并聚合，不取数、不重算评分、不改判信号。
    返回 diag dict，供本页 Excel 下载复用（一次聚合、两处展示）。
    """
    from mystery.apps.reports.resonance_diag import diagnose
    try:
        diag = diagnose(rows, indexes=indexes)
    except Exception as e:  # noqa: BLE001 诊断区块失败不影响结果表
        logger.warning("W94 三振诊断失败: %s", str(e)[:120])
        return {}
    tag = "（历史回放）" if diag.get("as_of") else ""
    with st.expander("🔍 为什么没有三振？六门闩诊断 " + tag,
                     expanded=(diag.get("n_true", 0) == 0)):
        st.caption(f"本期 {diag.get('n_total', 0)} 只个股 · 真三振 "
                   f"{diag.get('n_true', 0)} 只 · 与 Excel「三振诊断」/"
                   "日报 HTML 同口径（只读已落库结果）")
        st.info(diag.get("conclusion", ""))
        mleg = diag.get("market_leg") or {}
        opts = diag.get("options") or {}
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("大盘腿口径", str(mleg.get("market_mode") or "—"))
        c2.metric("大盘趋势向上", "是" if mleg.get("market_up") else "否")
        c3.metric("大盘位置", str(mleg.get("position") or "—"))
        c4.metric("三腿全过·仅卡大盘",
                  diag.get("three_legs_but_market", 0))
        if diag.get("indexes"):
            st.caption(f"8 大指数：站上年线 "
                       f"{diag.get('n_index_above')}/{diag.get('n_index_total')} · "
                       f"多头排列 {diag.get('n_index_aligned')}/"
                       f"{diag.get('n_index_total')}")
            st.dataframe(diag["indexes"], use_container_width=True,
                         hide_index=True, key=f"{key}_diag_idx")
        gv = [{"门闩": g["name"], "通过": g["passed"], "否决": g["failed"],
               "未知": g["unknown"], "通过率(%)": g["pass_rate"],
               "判据": g["why"]} for g in diag.get("gates", [])]
        st.dataframe(gv, use_container_width=True, hide_index=True,
                     key=f"{key}_diag_gates")
        st.caption("生效阈值 " + str(opts.get("true_threshold")) +
                   " · 全池最高共振评分 " + str(diag.get("score_max")) +
                   " · 距阈值还差 " + str(diag.get("gap_to_threshold")) + " 分")
        cf = diag.get("counterfactual") or {}
        if cf:
            st.caption(
                "W95 反事实对拍（等价推导，非重扫）："
                f"hard 一票否决 {cf.get('hard_true')} 只 · "
                f"soft 放宽 {cf.get('soft_true')} 只 · "
                f"off 放宽 {cf.get('off_true')} 只 · "
                "生效高位风控 "
                + str(opts.get("high_position_gate") or "hard"))
        with st.expander("口径阶段差说明：系统测什么 / 人工看什么", expanded=False):
            st.write(diag.get("stage_note", ""))
            st.code("czsc-mi scan --as-of 2026-01-14 --market --report",
                    language="bash")
    return diag


def _wave3_cell(row: dict, key: str):
    """W69：取扫描行内三浪字段；旧 job 缺字段统一显示 '-'。"""
    w = row.get('wave3') or {}
    v = w.get(key)
    return '-' if v is None or v == '' else v


def _index_cell(row: dict):
    """W84：指数研判单元格（年线位置 + MA 排列）；非指数行返回 '—'。"""
    ia = row.get('index_analysis')
    if not ia:
        return '—'
    above = ia.get('above_year')
    dist = ia.get('year_dist_pct')
    ma = ia.get('ma_aligned')
    y = ('站上年线' if above else '跌破年线') if above is not None else '未知'
    if dist is not None:
        y += f'({dist:+.1f}%)'
    m = '多头' if ma else ('非多头' if ma is not None else '未知')
    return f'{y}·MA{m}'


def _acc_cell(row: dict) -> str:
    """W86：主力吸筹等级单元格；统一返回 str（避免 pyarrow int/str 混合告警）。"""
    if row.get('is_index'):
        ia = row.get('index_analysis') or {}
        return str(ia.get('accumulation_level') or '—')
    return str(_rf.accumulation_text(row))


def _acc_stage_cell(row: dict) -> str:
    """W86：吸筹阶段单元格；指数取 index_analysis.accumulation.stage。"""
    if row.get('is_index'):
        acc = (row.get('index_analysis') or {}).get('accumulation') or {}
        return str(acc.get('stage') or '—')
    return str(_rf.accumulation_stage_text(row))

# 年线系统 7 项具体条件（src 键 → 展示列名）[P2-3：原 app.py 顶层常量，随 _scan_table_data 迁入]
_YEARLINE_DISP = [
    ('收盘价>MA250', '价>MA250'),
    ('收盘价>MA60', '价>MA60'),
    ('均线多头排列', '均线多头'),
    ('MA5>MA250', 'MA5>250'),
    ('MA10>MA250', 'MA10>250'),
    ('MA20>MA250', 'MA20>250'),
    ('MA60>MA250', 'MA60>250'),
]
# 主升浪 8 项具体指标（checklist8 键 → 展示列名）
_MAINWAVE_DISP = [
    ('长期横盘3个月以上', '长期横盘'),
    ('60日均线开始向上', 'MA60向上'),
    ('股价突破平台', '突破平台'),
    ('放量超20日均量2倍', '放量2倍'),
    ('回踩不破+MACD零轴金叉', '回踩+金叉'),
    ('RSI>50继续走强', 'RSI>50'),
    ('主力资金连续流入', '资金流入'),
    ('行业板块同步走强', '板块走强'),
]


def view_scan():
    st.header("全市场扫描")
    st.caption("后台扫描在独立进程中执行（脱离 Web 生命周期，刷新/重启页面都不会中断），"
               "结果按 chunk 增量落库（scan_jobs），实时进度见下方后台任务台账。")
    c1, c2, c3 = st.columns(3)
    with c1:
        limit = st.number_input("前台快速扫描只数", 1, 5000, 100)
    with c2:
        min_score = st.number_input("最低分", 0.0, 100.0, 0.0)
    if c3.button("开始前台扫描", type="primary"):
        with st.spinner("扫描中（单票失败自动跳过）..."):
            try:
                # W25：与 cron/后台扫描互斥，并发时被拒绝（不排队）
                rows = scan_market(limit=int(limit), include_detail=True,
                                   min_score=min_score or None)
            except RuntimeError as e:
                st.error(str(e))
            else:
                st.session_state['scan_results'] = rows
                st.session_state['scan_ts'] = len(rows)

    st.divider()
    st.subheader("后台扫描任务")
    b1, b2 = st.columns(2)
    if b1.button("🚀 后台扫描全部股票", type="primary", use_container_width=True):
        # W88：不再在 Web 进程内起 daemon 线程；拉起独立 CLI（--market
        # --resume），全 5579 只逐票增量落库，三浪全量评估也不占 Web 内存。
        _bg_launch("全部股票", scan_kind="market", scan_type="market")
        st.rerun()
    if b2.button("🚀 后台扫描全部自选股", use_container_width=True):
        wl = _wl.load_watchlist()
        if not wl:
            st.warning("自选股为空，先到「自选股」页添加")
        else:
            _bg_launch(f"全部自选股({len(wl)})", scan_kind="watchlist",
                       scan_type="watchlist")
            st.rerun()
    _render_bg_tasks()

    st.divider()
    st.subheader("最近扫描任务与结果")
    # W77：任务完成后 fragment 会自动触发全页刷新；此按钮供手动兜底。
    if st.button("🔄 刷新记录", key="scan_job_refresh"):
        st.rerun()
    jobs = _recent_jobs(limit=50)
    if not jobs:
        st.caption("暂无扫描记录（前台或后台扫描后出现）")
        return
    st.caption(f"共 {len(jobs)} 条历史扫描 job（每种类型保留最近若干份）；"
               f"后台任务的启动/失败/中断记录见上方「后台任务台账」。")
    opts = {_job_label(j): j[0] for j in jobs}
    pick = st.selectbox("选择扫描任务查看结果", list(opts.keys()), key="scan_job_pick")
    job_id = opts[pick]
    # W79：显示该 job 的扫描范围/耗时/三浪口径，避免自选扫描被误当成全市场。
    _jm = scan_job_meta(job_id)
    if _jm.get("label"):
        st.caption(f"📐 job #{job_id}：{_jm['label']}")
    sig = st.radio("信号过滤", ["全部", "真三振", "VAP-ATR突破", "筹码低位", "回踩确认突破", "假突破"],
                   horizontal=True, key="scan_job_sig")
    sig_map = {"全部": None, "真三振": "true_resonance", "回踩确认突破": "breakout_confirm",
               "假突破": "false_breakout",
               "VAP-ATR突破": "vap_atr", "筹码低位": "chip_low"}
    rows = scan_results_of(job_id, signal=sig_map[sig])
    st.caption(f"job #{job_id}（{sig}）共 {len(rows)} 只")
    if rows:
        # W92：先展示完整的大盘指数 / 行业板块研判，再把纯个股结果交给大表
        # （指数行不再混进 130+ 列个股表造成「分析内容都是空的」）。
        ctx = _render_market_overview(rows, key="scanjob")
        _render_scan_table(ctx["stock_rows"], key="scanjob", market_ctx=ctx)
    else:
        st.info("该任务无满足条件的标的")

    rows = st.session_state.get('scan_results')
    if rows:
        st.divider()
        st.subheader("前台扫描结果")
        ctx = _render_market_overview(rows, key="scan")
        _render_scan_table(ctx["stock_rows"], key="scan", market_ctx=ctx)

def _scan_table_data(rows: list, nav_key: str = "") -> list:
    """扫描结果表格数据（纯函数，渲染与计算分离）。

    判定列：年线滤网/周线锚定/破五反五 来自 mystery.signal；
    主升浪8项 来自 checklist8.满足数量（只展示不改判）。
    nav_key 为进程级导航缓存 key（_render_scan_table 生成），
    链接用 ?stock=..&nav_key=..&nav_idx=.. 供新标签页恢复导航。
    """
    table_data = []
    # W42 v0.10.16: 「详情」LinkColumn 新标签 = 全新 session；鉴权启用时链接
    # 内嵌跨标签 token（?at=），新页 require_login 验签自动放行，免重登。
    from mystery.apps.web.auth import make_auth_token
    at_tok = make_auth_token()
    at_qs = f"&at={at_tok}" if at_tok else ""
    # 所属自选板块：一次加载 tdx 板块快照，逐行反查（与 Excel 报告一致）
    _boards_snap = _wl.load_tdx_boards()
    for idx, row in enumerate(rows):
        item = {
            '序号': idx + 1,
            # W76：用户要求「名称在前，代码在后」（与 Excel 汇总/日报同口径）。
            '名称': row.get('name') or '未知',
            '代码': row['symbol'],
            # W84：指数行标记「指数」，与个股区分（位于代码之后，不影响 W76 列序）
            '类型': '指数' if row.get('is_index') else '个股',
            '所属板块': ('·'.join(_wl.boards_of_symbol(row['symbol'], _boards_snap))
                        if _boards_snap else ''),
            '评分': row.get('score'),
            '建议': row.get('advice', ''),
            '年线滤网': _sig_flag(row, '年线滤网'),
            '周线锚定': _sig_flag(row, '周线锚定'),
            '破五反五': _sig_flag(row, '破五反五'),
            '主升浪8项': _main_wave_count(row),
            '筹码低位': '是' if row.get('chip_low') else '否',
            '高位缩量': '是' if row.get('chip_quiet') else '否',
            # W32b：换手分档 + 风格（只读 core 纯函数，缺 turn → 未知）
            **_turn_tags(row),
            '回撤%': (None if row.get('price_pos') is None
                      else round(float(row['price_pos']) * 100, 1)),
            '日期': row.get('trade_date', ''),
            # W33：K线指纹前8位/来源（与详情页头部对照，不同 = 快照不同）
            '指纹': f"{row.get('bars_fingerprint') or '-'}"
                    f"/{row.get('data_source') or '-'}",
            # W34：日/周背驰共振（core 纯函数单一实现，只展示不进分）
            '背驰共振': _bc_resonance(row.get('chan') or {}) or '-',
            # W50：日线背驰单列（用户点名要求扫描结果直接可见；只读 payload
            # chan.1d.divergence，不重算。旧 job 无值显示 -，重扫后填充）
            '日线背驰': ((row.get('chan') or {}).get('1d') or {}).get(
                'divergence') or '-',
            # W69：三浪主升策略结论（随 payload_json 落库；旧 job 显示 '-'）
            '三浪结论': _wave3_cell(row, 'verdict'),
            '三浪阶段': _wave3_cell(row, 'phase'),
            '多周期位置': _wave3_cell(row, 'position'),
            '周期共振': _wave3_cell(row, 'resonance'),
            '三浪铁律': _wave3_cell(row, 'hard'),
            '三浪流程': _wave3_cell(row, 'stage'),
            '三浪买点': _wave3_cell(row, 'entry'),
            '三浪止损': _wave3_cell(row, 'stop'),
            '三浪建议(手)': _wave3_cell(row, 'lots'),
            '组合缩放': _wave3_cell(row, 'scale_down'),
            # W82：典型 K 线组合形态（随 payload_json 落库；旧 job 显示 '—'）
            '日K形态': ((row.get('candlestick') or {}).get('summary_daily')
                       or '—'),
            '周K形态': ((row.get('candlestick') or {}).get('summary_weekly')
                       or '—'),
            # W83：多方炮变体的重点观察量化结论（旧 job 显示 '—'）
            '形态重点观察': _rf.candlestick_focus_text(row),
            # W84：各大指数研判（仅指数行有值；个股行显示 '—'）
            '指数研判': _index_cell(row),
            # W86：主力建仓/吸筹研判（指数/个股统一口径，只读 payload）
            '主力吸筹': _acc_cell(row),
            '吸筹阶段': _acc_stage_cell(row),
            '详情': f"?stock={row['symbol']}&nav_key={nav_key}&nav_idx={idx}{at_qs}",
        }
        # W16：年线系统 7 项具体条件 + 主升浪 8 项具体指标（只展示不改判）
        for src, disp in _YEARLINE_DISP:
            item[f'年线·{disp}'] = _yl_flag(row, src)
        for src, disp in _MAINWAVE_DISP:
            item[f'主升浪·{disp}'] = _mainwave_flag(row, src)
        table_data.append(item)
    return table_data

import re as _re
from urllib.parse import quote as _quote

# W90：名称列详情链接的 display_text 正则。链接形如
# "?stock=<symbol>&nav_key=<k>&nav_idx=<i>&at=<tok>&_n=<名称>"，用捕获组 1
# 把股票名回显为链接文字（LinkColumn 的 display_text 支持正则，取首个捕获组）。
# 仅用于展示，_n 不影响详情页 query 直达（stock.py 只读 stock/nav_key/nav_idx）。
_NAME_DISPLAY_RE = r"[?&]_n=([^&]+)"
# 只编码会破坏 URL 结构/正则的字符；中文原样保留，供 display_text 原样回显
# （若整体 quote 中文，链接文字会变成 %XX 乱码）。
_NAME_STRUCT_RE = _re.compile(r"[&#?=%\s]")


def _name_link_rows(table_data: list) -> list:
    """W90：把「详情」链接挂到「名称」列，移除独立「详情」列（纯函数）。

    - 「名称」单元格值改为详情 URL，末尾附 ``&_n=<股票名>``；渲染时用
      ``LinkColumn(display_text=_NAME_DISPLAY_RE)`` 把股票名回显为可点击文字，
      实现「详情链接加到股票名称上」。
    - 原「详情」列整体移除（不再单列显示），导航/鉴权参数原样保留在 URL。
    - 名称里的中文**原样保留**（display_text 正则按原始字符串匹配回显），
      仅对 ``& # ? = % 空白`` 等结构性字符做 URL 编码，不破坏 query 解析。
    - 输入 ``table_data`` 不被修改（逐行浅拷贝），既有纯函数口径零改动。
    """
    out = []
    for row in table_data:
        d = dict(row)
        url = d.pop('详情', None)
        if url:
            name = str(d.get('名称') or '未知')
            label = _NAME_STRUCT_RE.sub(
                lambda m: _quote(m.group(0), safe=''), name)
            d['名称'] = f"{url}&_n={label}"
        out.append(d)
    return out


def _scan_table_column_config() -> dict:
    """W90：扫描结果表列配置（纯函数，便于单测，不触发任何 UI）。

    - glide-data-grid 冻结的是「左侧连续前缀列」，名称是第 2 列，故把
      「序号」一并 ``pinned``，保证名称稳定落在冻结区且保持既有列序。
    - 「名称」为 ``LinkColumn``：详情 URL 挂在名称上，``display_text`` 正则
      把股票名回显为可点击文字；``pinned=True`` 为 Streamlit ≥1.40 原生冻结。
    - 不再配置独立「详情」列（该列已在 ``_name_link_rows`` 中移除）。
    """
    return {
        '序号': st.column_config.Column('序号', pinned=True),
        '名称': st.column_config.LinkColumn(
            '名称',
            pinned=True,
            help='点击股票名称查看个股分析详情（本列已冻结）',
            display_text=_NAME_DISPLAY_RE,
        ),
    }


def _render_scan_table(rows: list, key: str = "scan", *, market_ctx=None):
    """扫描结果表格（股票名称可点击进入详情，名称列冻结）+ 加入自选。

    W92：``market_ctx`` 由 ``_render_market_overview`` 一次性取数后透传给
    Excel 下载，避免重复计算指数 / 板块研判；不传时下载按钮自行取缓存。

    W90：「名称」列为超链接（详情 URL 挂在名称上），并用 ``pinned=True``
    冻结在最左侧（Streamlit ≥1.40 原生列冻结，横向滚动始终可见）；
    独立「详情」列不再显示。LinkColumn 默认新标签页打开（全新 session，
    session_state 不共享），因此导航列表存进程级缓存 _nav_cache_store，
    nav_key 为该缓存的随机 key，进入详情页按 nav_key 恢复导航。
    """
    st.caption(f"共 {len(rows)} 只（按分降序）")
    _w3_line = wave3_summary_line(rows)
    st.caption(_w3_line or "三浪主升：本任务未含该策略（重新扫描后自动合并）")
    st.caption(_market_env_line_cached())

    # 存当前列表到进程级缓存（跨标签页/会话），供详情页恢复导航
    nav_key = _nav_cache_put(rows)

    # 构建表格数据（纯函数字段不变）→ W90 渲染层把详情链接挂到名称列
    table_data = _name_link_rows(_scan_table_data(rows, nav_key=nav_key))

    # 显示表格：名称列 = 冻结 + 详情链接（display_text 正则回显股票名）
    df = st.dataframe(
        table_data,
        use_container_width=True,
        hide_index=True,
        key=f"{key}_table",
        column_config=_scan_table_column_config(),
    )
    
    # 为表格下方添加代码输入框，用于跳转详情
    st.markdown("---")
    st.markdown("**查看详情**：输入序号或代码")
    
    col1, col2 = st.columns([1, 2])
    with col1:
        selected_idx = st.number_input(
            "序号",
            min_value=1,
            max_value=len(rows),
            value=1,
            key=f"{key}_select_idx"
        )
    
    with col2:
        # 显示选中股票的代码供确认
        if selected_idx and 1 <= selected_idx <= len(rows):
            selected_row = rows[selected_idx - 1]
            st.text_input(
                "股票代码",
                value=selected_row['symbol'],
                disabled=True,
                key=f"{key}_show_code"
            )
    
    # 查看详情按钮（同一 session 内跳转，直接恢复导航上下文）
    if st.button("查看该股票详情", key=f"{key}_view_detail"):
        if selected_idx and 1 <= selected_idx <= len(rows):
            selected_row = rows[selected_idx - 1]
            # 设置导航上下文
            st.session_state['stock_nav_list'] = rows
            st.session_state['stock_nav_idx'] = selected_idx - 1
            st.session_state['_pending_symbol'] = selected_row['symbol']
            st.session_state['_jump_to_page'] = '个股分析'
            st.rerun()
    
    _render_excel_download(rows, key=key, market_ctx=market_ctx)
    _add_watchlist_widget(rows, key=key)

def _enrich_scan_rows(rows: list) -> list:
    """补齐扫描结果详情（include_detail=True 重算），保证下载 Excel 与
    daily 报告一致。已有详情的行直接复用，单只失败保留原行不中断。"""
    need = [r for r in rows if _needs_detail(r)]
    if not need:
        return rows
    svc = _service()
    enriched = {}
    for r in need:
        try:
            d = svc.analyze_one_stock(r['symbol'], include_detail=True).to_dict()
            d.update(classify(d))
            enriched[r['symbol']] = d
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[web] {r.get('symbol')} 补详情失败，保留原行: {str(e)[:80]}")
    return [enriched.get(r['symbol'], r) for r in rows]

def _render_excel_download(rows: list, key: str = "scan", *,
                           market_ctx: dict | None = None):
    """扫描结果表下方「下载 Excel 报告」按钮（同 daily 格式）。

    W92：除「汇总报告 + 每只个股详情」外，追加「各大指数」「行业板块」两个
    工作表（与 CLI 日报 ``write_excel`` 同款填充函数）。此前 Web 只调
    ``excel_bytes(enriched)`` 生成单个汇总表，指数行还被当成个股混进汇总表
    造成上百列空白 —— 现在个股行与指数行分离，指数/板块各自成表。

    首次点击补齐个股详情（web 扫描落库缺明细）并生成 xlsx bytes 存 session；
    再次点击直接用已生成文件下载。session key 绑定 rows / 上下文签名，
    切换任务或信号后自动作废旧文件。
    """
    if not rows:
        return
    # 不传上下文时（如共振池页）自行取缓存，保证任何入口的 Excel 都带大盘/板块
    if market_ctx is None:
        market_ctx = _market_context(rows)
    stock_rows, _ = split_scan_rows(rows)
    indexes = list(market_ctx.get("indexes") or []) or None
    sectors = list(market_ctx.get("sectors") or []) or None
    diag = market_ctx.get("resonance_diag") or None
    first = stock_rows[0] if stock_rows else {}
    last = stock_rows[-1] if stock_rows else {}
    sig = (f"{len(stock_rows)}:{first.get('symbol', '')}:"
           f"{last.get('symbol', '')}:{first.get('trade_date', '')}:"
           f"{len(indexes or [])}:{len(sectors or [])}:{bool(diag)}")
    gen_key, data_key = f"{key}_xlsx_gen", f"{key}_xlsx_data_{sig}"
    if st.button(f"📥 下载 Excel 报告（{len(stock_rows)} 只）", key=gen_key):
        with st.spinner(f"补齐 {len(stock_rows)} 只个股详情并生成 Excel..."):
            # 只对个股补详情：指数行没有个股 K 线，不做 analyze_one_stock
            enriched = _enrich_scan_rows(stock_rows)
            try:
                st.session_state[data_key] = excel_bytes(
                    enriched, major_indexes=indexes, focus_sectors=sectors,
                    resonance_diag=diag)
            except Exception as e:  # noqa: BLE001
                st.error(f"生成 Excel 失败: {e}")
                st.session_state.pop(data_key, None)
    if data_key in st.session_state:
        fname = f"扫描报告_{datetime.now().strftime('%Y%m%d')}.xlsx"
        st.download_button("💾 保存 Excel 报告", data=st.session_state[data_key],
                           file_name=fname,
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           key=f"{key}_xlsx_dl")
        st.caption("包含：汇总报告 + 各大指数 + 行业板块 + 三振诊断 + "
                   "每只个股详情（与每日报告同格式）")

def _turn_tags(row: dict) -> dict:
    """W32b：换手分档/风格两列（只读 core.turnover_tag，不重算规则）。
    注意：app.py 以顶层脚本运行，只能绝对导入（文件头 sys.path 已兜底）。"""
    from mystery.core.turnover_tag import tags_from_result
    tg = tags_from_result(row)
    return {'换手分档': tg['turnover_tag'], '风格': tg['style_tag']}

def _sig_flag(row: dict, key: str):
    """从扫描行取 mystery.signal 判定字段：返回 '✅'/'❌'/''（缺失时 ''）。"""
    m = row.get('mystery') or {}
    sig = m.get('signal') or {}
    v = sig.get(key)
    if v is True:
        return '✅'
    if v is False:
        return '❌'
    return ''

def _yl_flag(row: dict, key: str):
    """年线系统具体条件（mystery.signal['年线条件']）：✅/❌/''。"""
    m = row.get('mystery') or {}
    yl = (m.get('signal') or {}).get('年线条件') or {}
    v = yl.get(key)
    if v is True:
        return '✅'
    if v is False:
        return '❌'
    return ''

def _mainwave_flag(row: dict, key: str):
    """主升浪 8 项具体指标（mystery.checklist8）：✅/❌/''。"""
    cl = (row.get('mystery') or {}).get('checklist8') or {}
    v = cl.get(key)
    if v is True:
        return '✅'
    if v is False:
        return '❌'
    return ''

def _main_wave_count(row: dict):
    """主升浪8项满足数量：返回 int；checklist8 缺失时返回 None（数值列可过滤/排序）。"""
    cl = (row.get('mystery') or {}).get('checklist8') or {}
    n = cl.get('满足数量')
    if n is None:
        return None
    try:
        return int(n)
    except (TypeError, ValueError):
        return None

__all__ = [
    'view_scan',
    '_scan_table_data',
    '_render_scan_table',
    '_enrich_scan_rows',
    '_render_excel_download',
    # W92：大盘指数 / 行业板块完整分析区块
    'split_scan_rows',
    'market_index_view',
    'market_sector_view',
    '_sector_cache_key',
    '_market_context',
    '_render_market_overview',
    # W94/B：为什么没有三振 六门闩诊断
    '_render_resonance_diag',
    '_turn_tags',
    '_wave3_cell',
    '_sig_flag',
    '_yl_flag',
    '_mainwave_flag',
    '_main_wave_count',
]
