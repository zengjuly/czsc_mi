"""mystery.apps.web.app — Streamlit 前端（P3：只调 Service，session 只存结果 dict）。

三视图：个股分析（支持代码/名称搜索 + 自选股）/ 全市场扫描 / 板块钻取。
渲染与计算分离：结果存 session_state，按钮后 fall-through 到展示区（不用 st.stop）。
运行：streamlit run mystery/apps/web/app.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import streamlit as st  # noqa: E402

st.set_page_config(page_title="Mistery 趋势交易分析", layout="wide")

from mystery.adapters.codes import normalize_symbol  # noqa: E402
from mystery.config import load_config, output_dir  # noqa: E402
from mystery.services.analyze import (AnalysisService, chan_enabled,  # noqa: E402
                                      chan_score_enabled)
from mystery.services.scan import (scan_market, latest_scan_job,  # noqa: E402
                                   scan_results_of)
from mystery.services import watchlist as _wl  # noqa: E402


@st.cache_resource
def _service():
    return AnalysisService(load_config())


def _fmt(v, nd=2):
    if v is None:
        return "-"
    try:
        return f"{float(v):.{nd}f}"
    except Exception:
        return str(v)


def _resolve_code(text: str):
    """输入代码或名称 → (code, name) 或 None。"""
    t = text.strip()
    if not t:
        return None
    try:
        return normalize_symbol(t), ""
    except Exception:
        pass
    hits = _search_cached(t)
    if hits:
        h = hits[0]
        return h['code'], h['name']
    return None


@st.cache_data(ttl=600, show_spinner=False)
def _search_cached(keyword: str) -> list:
    """名称搜索（缓存 10 分钟，避免每次 rerun 在线拉全市场列表）。"""
    return _wl.search_stock(keyword, limit=8)


@st.cache_data(show_spinner=False)
def _plot_cache(symbol: str, freq: str):
    """缠论图 Figure（plotly 自绘；czsc 已装且数据可用时返回，否则 None）。

    周期 1d/1w/1M 由 service 拉取（周/月=日K重采样），czsc 结构在 adapter 内
    重建；结果按 (symbol, freq) 缓存，切换周期不重新分析。
    """
    try:
        from mystery.adapters.czsc_adapter import CzscAdapter
        series = _service().market.fetch_bars(symbol, freq)
        return CzscAdapter().plot_figure(series)
    except Exception as e:  # noqa: BLE001
        st.warning(f"缠论图生成失败，降级文本展示: {str(e)[:80]}")
        return None


@st.cache_data(show_spinner=False)
def _lightweight_cache(symbol: str, freq: str):
    """官方 lightweight 校验图 HTML（plot_czsc(c, output='html')）；失败返回 ''。

    仅作校验/降级对照，主图始终用 plotly 自绘中枢盒（_plot_cache）。
    """
    try:
        from mystery.adapters.czsc_adapter import CzscAdapter
        series = _service().market.fetch_bars(symbol, freq)
        return CzscAdapter().plot_lightweight_html(series)
    except Exception:  # noqa: BLE001
        return ""


# ================= 展示函数（模块级，先定义后调用） =================
def _sig_text(v) -> str:
    """±1/0 信号 → 文本（1=金叉/突破，-1=死叉/破位，0=无）。"""
    try:
        n = int(v)
    except Exception:
        return str(v)
    return {1: '✅ 金叉/突破', -1: '❌ 死叉/破位', 0: '—'}.get(n, str(v))


def _vp_text(v) -> str:
    """量价配合度 1/-1/0 → 文本。"""
    try:
        n = int(v)
    except Exception:
        return str(v)
    return {1: '✅ 量升价升', -1: '⚠️ 量价背离', 0: '—'}.get(n, str(v))


def _render_technical(tech: dict):
    """分析明细：均线排列 / 破五反五 / 量价 / 筹码 / 换手率 / 多周期。"""
    ma = tech.get('ma', {}) or {}
    if ma:
        st.markdown("**均线排列分析**")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("排列状态", ma.get('排列状态', '-'))
        c2.metric("多头排列强度", f"{_fmt(ma.get('多头排列强度'))}/3")
        c3.metric("MA5斜率", _fmt(ma.get('MA5斜率')))
        c4.metric("均线信号", _sig_text(ma.get('均线信号')))
        st.markdown(
            f"- MA5 {_fmt(ma.get('MA5'))} | MA10 {_fmt(ma.get('MA10'))} | "
            f"MA20 {_fmt(ma.get('MA20'))} | MA60 {_fmt(ma.get('MA60'))} | "
            f"MA250 {_fmt(ma.get('MA250'))}"
            f"（最新交易日 {tech.get('latest_date', '')}）")
        if ma.get('突破信号'):
            st.markdown(f"- 突破MA20信号: {_sig_text(ma.get('突破信号'))}")

    po5 = tech.get('po5', {}) or {}
    if po5:
        st.markdown("**破五反五**")
        c1, c2, c3 = st.columns(3)
        c1.metric("状态", "✅ 成立" if po5.get('破五反五') else "❌ 未成立")
        c2.metric("破五天数", _fmt(po5.get('破五天数'), 0))
        c3.metric("MA20斜率", _fmt(po5.get('MA20斜率')))
        if po5.get('原因'):
            st.markdown(f"- {po5['原因']}")

    vp = tech.get('volume_price', {}) or {}
    if vp:
        st.markdown("**量价分析**")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("量比", _fmt(vp.get('量比')))
        c2.metric("量价配合度", _vp_text(vp.get('量价配合度')))
        c3.metric("OBV信号", _sig_text(vp.get('OBV信号')))
        c4.metric("动能状态", vp.get('动能状态', '-'))
        st.markdown(
            f"- 成交量信号 {_sig_text(vp.get('成交量信号'))} | "
            f"成交量突破 {_sig_text(vp.get('成交量突破信号'))} | "
            f"5日涨跌 {_fmt(vp.get('价格变化率5日'))}% | "
            f"20日涨跌 {_fmt(vp.get('价格变化率20日'))}%")

    chip = tech.get('chip', {}) or {}
    if chip:
        st.markdown("**筹码分析**")
        c1, c2, c3 = st.columns(3)
        c1.metric("筹码集中度", chip.get('筹码集中度', '-'))
        c2.metric("集中度数值(近20日换手%)", _fmt(chip.get('筹码集中度数值')))
        c3.metric("筹码趋势", chip.get('筹码趋势', '-'))
        for line in chip.get('详情', []):
            st.markdown(f"- {line}")

    trn = tech.get('turnover', {}) or {}
    if trn:
        st.markdown("**换手率**")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("最新换手率%", _fmt(trn.get('换手率')))
        c2.metric("区域", trn.get('换手率区域', '-'))
        c3.metric("换手率MA5", _fmt(trn.get('换手率MA5')))
        c4.metric("换手率MA20", _fmt(trn.get('换手率MA20')))
        st.markdown(
            f"- 相对20日位置: {_fmt(trn.get('换手率相对位置'))}%"
            f"（>0 放量 / <0 缩量）")
        if float(trn.get('换手率') or 0) == 0:
            st.caption("⚠️ 换手率全为 0：数据源未提供换手率（ths/tdx 常缺 turn），"
                       "上方区域判定不可信，建议先 sync 补换手率")

    mp = tech.get('multi_period', {}) or {}
    if mp:
        st.markdown("**多周期分析**")
        w, mo = mp.get('周线', {}) or {}, mp.get('月线', {}) or {}
        if w:
            st.markdown(
                f"- 周线: **{w.get('趋势', '-')}**（最新 {_fmt(w.get('最新价'))}，"
                f"MA20 {_fmt(w.get('MA20'))}）")
        if mo:
            st.markdown(
                f"- 月线: **{mo.get('趋势', '-')}**（最新 {_fmt(mo.get('最新价'))}，"
                f"MA10 {_fmt(mo.get('MA10'))}）")
        wa = mp.get('周线锚定', {}) or {}
        if wa:
            st.markdown(
                f"- 周线锚定: {'✅ 锚定' if wa.get('锚定') else '❌ 未锚定'}"
                f"（{wa.get('原因', '')}）")
        for key, name in [('周线箱体', '周线箱体'), ('月线箱体', '月线箱体')]:
            box = mp.get(key) or {}
            if box and box.get('状态'):
                st.markdown(
                    f"- {name}: {box.get('状态', '-')}（当前 "
                    f"{_fmt(box.get('当前价'))}，区间 "
                    f"{_fmt(box.get('下沿'))}~{_fmt(box.get('上沿'))}）")


def render_stock(d: dict):
    """个股页：指标卡 + 缠论卡 + 明细（只读 result dict，不再计算）。"""
    name = d.get('name') or '未知'
    st.subheader(f"{name}（{d.get('symbol', '')}）  {d.get('trade_date', '')}")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("综合评分", _fmt(d.get('score')))
    c2.metric("操作建议", d.get('advice', '-'))
    c3.metric("最新价", _fmt(d.get('price')))
    c4.metric("真三振", "✅" if d.get('true_resonance') else "❌")
    c5.metric("行业", d.get('sector', {}).get('行业名称', '-'))

    # 缠论卡（只读 chan；W3-A：czsc 已装且 chan 非空 → plotly 缠论图）
    chan = d.get('chan', {}) or {}
    if chan:
        with st.expander("缠论结构（czsc，仅展示）", expanded=True):
            freq_label = st.radio(
                "K线周期", ["日线", "周线", "月线"], horizontal=True,
                key="stock_freq")
            freq = {"日线": "1d", "周线": "1w", "月线": "1M"}[freq_label]
            fig = _plot_cache(d.get('symbol', ''), freq)
            if fig is not None:
                st.plotly_chart(fig, height=720, width="stretch")
            else:
                st.info("缠论图不可用（czsc 未装或数据不足），见下方结构文本")
            with st.expander("官方校验图（czsc lightweight，仅对照）", expanded=False):
                lw_html = _lightweight_cache(d.get('symbol', ''), freq)
                if lw_html:
                    st.iframe(lw_html, height=640)
                else:
                    st.info("lightweight 校验图不可用（czsc 未装或数据不足）")
            for freq, cs in chan.items():
                bis = cs.get('bis', [])
                zss = cs.get('zss', [])
                last_bi = f"{'向上' if cs.get('last_bi_dir') == 'up' else '向下'}" \
                    if cs.get('last_bi_dir') else "-"
                st.markdown(
                    f"**{freq}**：分型 {cs.get('n_fx')} 个 · 笔 {len(bis)} 条 · "
                    f"中枢 {len(zss)} 个 · 最新笔 {last_bi}"
                    f"{'（已确认）' if cs.get('last_bi_confirmed') else '（未确认）'} · "
                    f"当前{'在中枢内' if cs.get('in_zs') else '不在中枢内'}"
                    f" · {cs.get('engine')} {cs.get('engine_ver')}")
                for zs in zss[-3:]:
                    st.markdown(
                        f"　中枢 {zs['sdt']}~{zs['edt']}：ZG {_fmt(zs['zg'])} / "
                        f"ZD {_fmt(zs['zd'])} / GG {_fmt(zs['gg'])} / DD {_fmt(zs['dd'])} · "
                        f"{zs['n_bi']}笔")
    else:
        st.info("缠论未开启（MYSTERY_CHAN_ENABLED=0）")

    m = d.get('mystery', {})
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("主升浪状态", m.get('main_wave', {}).get('主升浪状态', '-'))
    c2.metric("平台状态", m.get('platform', {}).get('平台状态', '-'))
    plat = m.get('platform', {}).get('平台范围') or {}
    c3.metric("平台箱体", f"{_fmt(plat.get('下沿'))}~{_fmt(plat.get('上沿'))}"
              if plat else "-")
    c4.metric("主升浪8项满足", m.get('checklist8', {}).get('满足数量', '-'))

    with st.expander("主升浪 8 项指标", expanded=False):
        cl = m.get('checklist8', {})
        items = {k: v for k, v in cl.items()
                 if k not in ('详情', '满足数量', '满足占比', '综合判断', '平台范围')}
        for k, v in items.items():
            st.markdown(f"- {'✅' if v else '❌'} {k}")
        st.caption(" | ".join(cl.get('详情', [])))

    with st.expander("判定依据", expanded=False):
        for line in m.get('main_wave', {}).get('判定依据', []):
            st.markdown(f"- {line}")
        for line in m.get('resonance', {}).get('详情', []):
            st.markdown(f"- {line}")

    # ---------- W5 技术面明细（仅展示，数据来自 mystery.technical 快照） ----------
    tech = m.get('technical', {}) or {}
    if tech:
        with st.expander("分析明细（均线/破五反五/量价/筹码/换手率/多周期）",
                         expanded=False):
            _render_technical(tech)

    fin = d.get('financial', {}) or {}
    if fin:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("PE", _fmt(fin.get('PE')))
        c2.metric("PB", _fmt(fin.get('PB')))
        c3.metric("ROE", _fmt(fin.get('roe')))
        c4.metric("报告期", str(fin.get('report_date', '-') or '-'))
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("EPS(TTM)", _fmt(fin.get('eps_ttm')))
        c2.metric("股息率%", _fmt(fin.get('divid_cash')))
        c3.metric("净利率%", _fmt(fin.get('np_margin')))
        c4.metric("毛利率%", _fmt(fin.get('gp_margin')))


# ================= 侧栏自选股 =================
# 004.md：自选只留子页（subview=watchlist），侧栏不再渲染自选列表。
def _add_watchlist_widget(rows: list, key: str = "wl"):
    """扫描/真三振/板块钻取表下方：选一只加入自选（source=scan）。"""
    if not rows:
        return
    opts = {f"{r.get('name') or '未知'}（{r['symbol']}）": r['symbol'] for r in rows}
    pick = st.selectbox("选择加入自选", list(opts.keys()), key=f"{key}_wl_pick")
    if st.button("加入自选", key=f"{key}_wl_add"):
        sym = opts[pick]
        nm = next((r.get('name') or '' for r in rows if r['symbol'] == sym), '')
        _wl.add_to_watchlist(sym, name=nm, source='scan')
        st.rerun()


def view_watchlist_subpage():
    """自选管理页（主导航「自选股」直达；个股页「管理自选」作为子视图进入）。

    禁止对全部自选跑分析：只提供单只「分析」按钮。
    """
    st.header("自选管理")
    if st.session_state.get('subview') == 'watchlist':
        # 经个股页「管理自选」进入（子视图）：显示返回；主导航直达不显示。
        if st.button("← 返回"):
            st.session_state['subview'] = None
            st.rerun()

    c_imp, c_imp_btn = st.columns([3, 1])
    c_imp.caption("通达信自选：本地 T0002/blocknew/zxg.blk（只读导入，合并不覆盖）")
    if c_imp_btn.button("从通达信导入"):
        with st.spinner("从通达信导入中..."):
            r = _wl.import_from_tdx(load_config())
        if r.get('path'):
            st.success(f"导入 {r['imported']} 只（已存在跳过 {r['skipped']}）"
                       f" ← {r['path']}")
            st.rerun()
        else:
            st.warning("未找到通达信自选文件 zxg.blk（请设置 TDX_VIPDOC_DIR / "
                       "TDX_BLOCKNEW_DIR，默认查 /mnt/c/new_tdx、/mnt/new_tdx 的 "
                       "T0002/blocknew）")

    kw_col, add_col = st.columns([3, 1])
    kw = kw_col.text_input("添加自选（代码或名称）", key="wl_add_input")
    if add_col.button("添加", type="primary"):
        add_text = kw.strip()
        if add_text:
            hits = _wl.search_stock(add_text, limit=8)
            if hits:
                h = hits[0]
                _wl.add_to_watchlist(h['code'], name=h['name'], source='manual')
                st.rerun()
            else:
                try:
                    sym = normalize_symbol(add_text)
                    _wl.add_to_watchlist(sym, name='', source='manual')
                    st.rerun()
                except Exception:
                    st.error(f"未找到股票：{add_text}")

    items = _wl.load_watchlist_items()
    if not items:
        st.info("自选股为空")
        return
    st.caption(f"共 {len(items)} 只")
    for it in items:
        sym = it['symbol']
        name = it['name'] or '未知'
        c1, c2, c3, c4, c5 = st.columns([1.1, 1.4, 1.4, 0.7, 0.7])
        c1.markdown(f"`{sym}`")
        c2.write(name)
        c3.write(_wl.source_label(it['source'], it.get('source_file') or ''))
        if c4.button("分析", key=f"an_{sym}"):
            st.session_state['_pending_symbol'] = sym
            st.session_state['subview'] = None
            st.rerun()
        if c5.button("删除", key=f"del_{sym}"):
            _wl.remove_from_watchlist(sym)
            st.rerun()


# ================= 视图 =================
def view_stock():
    st.header("个股分析（支持代码或股票名搜索）")
    pending = st.session_state.pop('_pending_symbol', None)
    c1, c2 = st.columns([3, 1])
    text = c1.text_input("输入代码或名称", pending or "sh600519").strip()
    do_analyze = c2.button("分析", type="primary") or bool(pending)
    # 名称搜索提示（输入非代码时展示匹配候选）
    candidates = None
    if text:
        try:
            normalize_symbol(text)
        except Exception:
            candidates = _search_cached(text)
            if candidates:
                opts = {f"{h['name']}（{h['code']}）": h['code'] for h in candidates}
                pick = st.radio("匹配到以下股票，选择后点分析：",
                                list(opts.keys()), horizontal=True)
                text = opts[pick]
    if do_analyze:
        resolved = _resolve_code(text)
        if resolved is None:
            st.error(f"未找到股票：{text}")
        else:
            code, _ = resolved
            with st.spinner("分析中..."):
                try:
                    r = _service().analyze_one_stock(code)
                    d = r.to_dict()
                    st.session_state['stock_analysis'] = d
                    st.session_state['stock_analysis']['_input'] = text
                except Exception as e:
                    st.error(f"分析失败: {e}")
                    st.session_state.pop('stock_analysis', None)
    d = st.session_state.get('stock_analysis')
    if d:
        render_stock(d)
        cur = d.get('symbol', '')
        items = _wl.load_watchlist_items()
        cur_item = next((it for it in items if it['symbol'] == cur), None)
        if cur_item:
            st.caption(f"自选来源：{_wl.source_label(cur_item['source'], cur_item.get('source_file') or '')}")
            if st.button("从自选股移除", use_container_width=True):
                _wl.remove_from_watchlist(cur)
                st.rerun()
        else:
            if st.button("加入自选股", use_container_width=True):
                _wl.add_to_watchlist(cur, name=d.get('name') or '', source='manual')
                st.rerun()
        if st.button("管理自选", use_container_width=True):
            st.session_state['subview'] = 'watchlist'
            st.rerun()


def view_scan():
    st.header("全市场扫描")
    c1, c2 = st.columns(2)
    limit = c1.number_input("最多分析只数", 1, 5000, 100)
    min_score = c2.number_input("最低分", 0.0, 100.0, 0.0)
    if st.button("开始扫描", type="primary"):
        with st.spinner("扫描中（单票失败自动跳过）..."):
            rows = scan_market(limit=int(limit), include_detail=False,
                               min_score=min_score or None)
            st.session_state['scan_results'] = rows
            st.session_state['scan_ts'] = len(rows)
    rows = st.session_state.get('scan_results')
    if rows:
        st.caption(f"共 {len(rows)} 只（按分降序）")
        st.dataframe([{'代码': r['symbol'], '名称': r.get('name') or '未知',
                       '评分': r.get('score'), '建议': r.get('advice', ''),
                       '筹码低位': '是' if r.get('chip_low') else '否',
                       '换手未知': '未知' if r.get('chip_low_unknown') else '',
                       '高位缩量': '是' if r.get('chip_quiet') else '否',
                       '20日换手': r.get('turnover_20'),
                       '回撤%': (None if r.get('price_pos') is None
                                 else round(float(r['price_pos']) * 100, 1)),
                       '日期': r.get('trade_date', '')} for r in rows],
                     use_container_width=True, hide_index=True)
        _add_watchlist_widget(rows, key="scan")


def view_sector():
    st.header("板块钻取（真实指数，非成分股抽样）")
    svc = _service()
    meta = svc.market.db.get_sector_meta(active_only=True)
    names = sorted({f"{m[1]}（{m[0]}）" for m in meta if m[1]})
    pick = st.selectbox("选择板块", names)
    if not pick:
        return
    s_code = pick.split("（")[-1][:-1]
    ind = svc.sector.get_sector(s_code)
    st.metric("行业强度分（0~25）", _fmt(ind.get('score')),
              delta="向上" if ind.get("up") else "向下")
    if st.button("分析板块成分股 Top10", type="primary"):
        stocks = svc.market.db.get_sector_stocks(s_code)[:10]
        if not stocks:
            st.warning("该板块暂无成分股数据（stock_sector_rel 未同步该板块）。"
                       "可先执行板块成分同步，或改用板块强度表/全市场扫描。")
        else:
            with st.spinner(f"分析 {len(stocks)} 只成分股..."):
                rows = scan_market(universe=stocks, include_detail=False)
                st.session_state['sector_results'] = rows
    rows = st.session_state.get('sector_results')
    if rows:
        st.dataframe([{'代码': r['symbol'], '名称': r.get('name') or '未知',
                       '评分': r.get('score'), '建议': r.get('advice', ''),
                       '行业': r.get('sector', {}).get('行业名称', '-')}
                      for r in rows], use_container_width=True, hide_index=True)
        _add_watchlist_widget(rows, key="sector")


# ================= W2-B 新视图 =================
def _sector_change_pct(kline_df, days: int):
    """板块指数近 N 日涨跌 %（从 sector_kline 真实指数算，禁止成分股抽样）。"""
    if kline_df is None or len(kline_df) < 2:
        return None
    df = kline_df.tail(days + 1)
    if len(df) < 2:
        return None
    try:
        c0 = float(df['收盘价'].iloc[0])
        c1 = float(df['收盘价'].iloc[-1])
        return round((c1 / c0 - 1) * 100, 2) if c0 else None
    except Exception:
        return None


@st.cache_data(ttl=600, show_spinner=False)
def _sector_strength_table() -> list:
    """板块强度表：板块名/代码/得分/近5/10/20日涨跌（只读 sector_kline）。"""
    svc = _service()
    meta = svc.market.db.get_sector_meta(active_only=True)
    rows = []
    for code, name, parent in meta:
        if not name:
            continue
        kline = svc.market.db.get_sector_kline(code)
        ind = svc.sector.get_sector(code)
        rows.append({
            '板块': name, '代码': code,
            '强度分': ind.get('score'),
            '近5日%': _sector_change_pct(kline, 5),
            '近10日%': _sector_change_pct(kline, 10),
            '近20日%': _sector_change_pct(kline, 20),
        })
    rows.sort(key=lambda r: (r['强度分'] is not None, float(r['强度分'] or -1)),
              reverse=True)
    return rows


def view_true_resonance():
    st.header("真三振池（最近一次全市场扫描结果）")
    job = latest_scan_job(load_config())
    if not job:
        st.info("暂无全市场扫描记录。请运行 `czsc-mi scan --limit 100` 生成真三振池。")
        return
    rows = scan_results_of(job, signal="true_resonance")
    st.caption(f"来自 scan job #{job}，共 {len(rows)} 只真三振")
    if rows:
        st.dataframe([{'代码': r['symbol'], '名称': r.get('name') or '未知',
                       '评分': r.get('score'), '建议': r.get('advice', ''),
                       '日期': r.get('trade_date', '')} for r in rows],
                     use_container_width=True, hide_index=True)
        _add_watchlist_widget(rows, key="resonance")
    else:
        st.info("最近一次扫描无真三振标的")


def view_system():
    st.header("系统状态（只读）")
    cfg = load_config()
    svc = _service()
    db = svc.market.db
    try:
        import importlib.util
        czsc_ok = importlib.util.find_spec("czsc") is not None
        czsc_ver = ""
        if czsc_ok:
            from mystery.adapters.czsc_adapter import czsc_version
            czsc_ver = czsc_version()
    except Exception:
        czsc_ok, czsc_ver = False, ""
    kline_latest = None
    try:
        conn = db._connect()
        row = conn.execute(
            "SELECT MAX(date) FROM stock_kline_data").fetchone()
        kline_latest = row[0] if row else None
        conn.close()
    except Exception:
        pass
    st.json({
        "MYSTERY_DB_PATH": db.db_path,
        "报表输出目录": output_dir(cfg),
        "chan 开关": "开" if chan_enabled() else "关",
        "混合分开关": "开" if chan_score_enabled() else "关（默认）",
        "czsc 可导入": "✅" if czsc_ok else "❌（pip install -e '.[chan]'）",
        "czsc 版本": czsc_ver or "-",
        "rule_ver": "mystery-1.22.30-compat",
        "库内 kline 最新日": kline_latest or "空",
        "data_source.primary": (cfg.get("data_source") or {}).get("primary", "-"),
        "data_source.fallback": (cfg.get("data_source") or {}).get("fallback", []),
        "行情降级顺序": "db → ths_official → tdx_api → tdx_local",
    }, expanded=True)
    st.subheader("最近扫描任务")
    try:
        conn = db._connect()
        rows = conn.execute(
            "SELECT id, trade_date, started_at, n_ok, n_fail "
            "FROM scan_jobs ORDER BY id DESC LIMIT 5").fetchall()
        conn.close()
        if rows:
            st.dataframe([{'job_id': r[0], 'trade_date': r[1],
                           'started_at': r[2], '成功': r[3], '失败': r[4]}
                          for r in rows], use_container_width=True,
                         hide_index=True)
        else:
            st.caption("尚无扫描记录（czsc-mi scan 后出现）")
    except Exception as e:
        st.caption(f"读取失败: {str(e)[:60]}")


def view_sector_strength():
    st.header("板块强度表（真实指数，非成分股抽样）")
    rows = _sector_strength_table()
    if not rows:
        st.info("板块数据为空（先执行板块同步）")
        return
    st.caption(f"共 {len(rows)} 个板块，按强度分降序")
    st.dataframe(rows, use_container_width=True, hide_index=True,
                 column_config={"强度分": st.column_config.NumberColumn(
                     format="%.2f")})
    names = sorted({f"{r['板块']}（{r['代码']}）" for r in rows})
    pick = st.selectbox("点击板块 → 钻取成分股（走 analyze_one_stock）", names)
    if pick:
        st.session_state['jump_sector'] = pick.split("（")[-1][:-1]
        if st.button("进入板块钻取", type="primary"):
            st.rerun()


def _clear_subview():
    st.session_state['subview'] = None


def main():
    page = st.sidebar.radio("导航", ["个股分析", "自选股", "全市场扫描", "板块钻取",
                                    "真三振池", "系统状态", "板块强度表"],
                            on_change=_clear_subview)
    st.sidebar.caption("唯一计算入口：mystery.services.analyze.analyze_one_stock")
    st.sidebar.caption(
        f"chan 开关: {'开' if chan_enabled() else '关'} | "
        f"混合分: {'开' if chan_score_enabled() else '关（默认）'}")
    # 个股页「管理自选」进入的子视图优先
    if st.session_state.get('subview') == 'watchlist':
        view_watchlist_subpage()
        return
    if page == "个股分析":
        view_stock()
    elif page == "自选股":
        view_watchlist_subpage()
    elif page == "全市场扫描":
        view_scan()
    elif page == "真三振池":
        view_true_resonance()
    elif page == "系统状态":
        view_system()
    elif page == "板块强度表":
        view_sector_strength()
    else:
        # 板块钻取：支持从强度表跳转（预置所选板块）
        jump = st.session_state.pop('jump_sector', None)
        if jump:
            st.session_state['sector_pick'] = f"{jump}"
        view_sector()


main()
