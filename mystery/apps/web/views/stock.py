from __future__ import annotations
import logging
import streamlit as st
from .components import *

def render_stock(d: dict):
    """个股页：指标卡 + 缠论卡 + 明细（只读 result dict，不再计算）。"""
    name = d.get('name') or '未知'
    st.subheader(f"{name}（{d.get('symbol', '')}）  {d.get('trade_date', '')}")
    # W33：数据指纹 + 来源（诊断「扫描 vs 详情不一致」：两侧指纹不同 = K线快照不同）
    _fp = d.get('bars_fingerprint') or '-'
    _src = d.get('data_source') or '-'
    _rv = d.get('rule_ver') or '-'
    st.caption(f"数据指纹 {_fp} · 来源 {_src} · 口径 {_rv}")
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
            # W41：data_ver=交易日|指纹前8，盘后 K 线更新自动换代缓存（与分数同源）
            _dver = f"{d.get('trade_date', '')}|{_fp[:8]}"
            fig = _plot_cache(d.get('symbol', ''), freq, _dver)
            if fig is not None:
                st.plotly_chart(fig, height=720, width="stretch")
            else:
                st.info("缠论图不可用（czsc 未装或数据不足），见下方结构文本")
            with st.expander("官方校验图（czsc lightweight，仅对照）", expanded=False):
                lw_html = _lightweight_cache(d.get('symbol', ''), freq, _dver)
                if lw_html:
                    st.iframe(lw_html, height=640)
                else:
                    st.info("lightweight 校验图不可用（czsc 未装或数据不足）")
            for freq, cs in chan.items():
                bis = cs.get('bis', [])
                zss = cs.get('zss', [])
                last_bi = f"{'向上' if cs.get('last_bi_dir') == 'up' else '向下'}" \
                    if cs.get('last_bi_dir') else "-"
                _pos = {"above": "现价中枢上方", "below": "现价中枢下方",
                        "in": "现价中枢区间内"}.get(cs.get('zs_position') or "", "")
                st.markdown(
                    f"**{freq}**：分型 {cs.get('n_fx')} 个 · 笔 {len(bis)} 条 · "
                    f"中枢 {len(zss)} 个 · 最新笔 {last_bi}"
                    f"{'（已确认）' if cs.get('last_bi_confirmed') else '（未确认）'} · "
                    f"当前{'在中枢内' if cs.get('in_zs') else '不在中枢内'}"
                    f"{(' · ' + _pos) if _pos else ''}"
                    f"{(' · 离开中枢' + ('上' if cs.get('leave_zs') == 'up' else '下')) if cs.get('leave_zs') else ''}"
                    f"{(' · 末笔/中枢比 ' + str(cs.get('bi_stretch'))) if cs.get('bi_stretch') is not None else ''}"
                    # W50：单级别背驰/买卖点显式化（此前只在共振行合并展示，
                    # 且信号源恒空）；MACD 柱背驰口径 tas_macd_bc_V240307。
                    f" · 背驰 {cs.get('divergence') or '无'}"
                    f" · 买卖点 {cs.get('bs_flag') or '无'}"
                    f" · {cs.get('engine')} {cs.get('engine_ver')}")
                if _pos:
                    st.caption("结构摘要加深仅供展示，未进综合分")
                for zs in zss[-3:]:
                    st.markdown(
                        f"　中枢 {zs['sdt']}~{zs['edt']}：ZG {_fmt(zs['zg'])} / "
                        f"ZD {_fmt(zs['zd'])} / GG {_fmt(zs['gg'])} / DD {_fmt(zs['dd'])} · "
                        f"{zs['n_bi']}笔")
            _bcr = _bc_resonance(chan)
            if _bcr:
                st.markdown(f"**背驰共振：{_bcr}**（日/周多级别联动，"
                            f"20260524 第二章；只展示不进综合分）")
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

    # 阶段识别 + 突破质量（吸筹/洗盘/拉升/派发 + 回踩确认/假突破）
    stg = m.get('stage', {}) or {}
    if stg:
        bq = (stg.get('breakout', {}) or {}).get('quality', '无')
        st.markdown(
            f"**阶段识别**：`{stg.get('stage', '未知')}`"
            f"（筹码峰位移 {_fmt(stg.get('metrics', {}).get('poc_slope_20'))}）"
            f" ｜ **突破质量**：`{bq}`"
            + ("（回踩不破，可进）" if bq == '回踩确认'
               else "（假突破，回避）" if bq == '假突破' else ""))

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

    # ---------- G4 高级形态（强势反包阳/缺口/低位地量），仅展示/轻加权 ----------
    _pat = m.get('patterns', {}) or {}
    _eng = _pat.get('强势反包阳') or {}
    _gap = _pat.get('缺口') or {}
    _low = _pat.get('低位地量') or {}
    if _eng or _gap or _low:
        with st.expander("高级形态（强势反包阳/缺口/低位地量）", expanded=False):
            if _eng:
                _ed = _eng.get('detail', {}) or {}
                st.markdown(f"**强势反包阳**：{'✅ 命中' if _eng.get('hit') else '仅基础吞没'}"
                            f"（{_ed.get('强度', '-')}）")
                st.markdown(f"- 昨阴 {_ed.get('昨阴', '-')} → 今阳 {_ed.get('今阳', '-')}"
                            f" · 放量确认 {_ed.get('放量确认')} · 回撤 {_ed.get('回撤%')}%"
                            f" · 站上MA5 {_ed.get('站上MA5')}")
            if _gap and _gap.get('gap_up'):
                _gd = _gap.get('detail', {}) or {}
                _gt = _gap.get('gap_type')
                _warn = " ⚠️**卖/减仓信号**" if _gt == '衰竭' else ""
                st.markdown(f"**向上跳空缺口[{_gt}]**：幅度 {_gap.get('gap_magnitude_pct')}%{_warn}")
                st.markdown(f"- 缺口位 {_gd.get('缺口位')} · 前20日涨幅 {_gd.get('前20日涨幅%')}%"
                            f" · 横盘前兆 {_gd.get('横盘前兆')} · 站上MA20 {_gd.get('站上MA20')}"
                            f" · 三日不补 {'✅' if _gd.get('三日不补') else '❌'}")
            if _low:
                _ld = _low.get('detail', {}) or {}
                st.markdown(f"**低位地量**：{'✅ 命中' if _low.get('hit') else '未命中'}"
                            f"（低位 {_low.get('低位')} · 量能压缩 {_low.get('量能压缩')}）")
                st.markdown(f"- 回撤 {_ld.get('回撤%')}% · 近20日地量 {_ld.get('近20日地量')}"
                            f" · 量/60日均量 {_ld.get('量/60日均量')}"
                            f" · 长期缩量(MA20<MA60) {_ld.get('MA20<MA60(长期缩量)')}")

    # ---------- W82 典型 K 线组合形态（日 K / 周 K 独立分析，仅展示） ----------
    _cs = d.get('candlestick') or {}
    if _cs:
        with st.expander("典型K线组合形态（日K / 周K 独立分析）", expanded=False):
            _cfg = _cs.get('config') or {}
            st.caption(
                f"回溯 {_cfg.get('lookback', '-')} 根 · 启用 "
                f"{len(_cfg.get('patterns') or [])} 个形态 · "
                f"日K{'开' if _cfg.get('daily') else '关'} / "
                f"周K{'开' if _cfg.get('weekly') else '关'} · "
                "仅展示，不进综合分（配置见「系统状态」页）")
            _cd, _cw = st.columns(2)
            for _col, _per, _plabel in ((_cd, 'daily', '日 K'),
                                         (_cw, 'weekly', '周 K')):
                with _col:
                    st.markdown(f"**{_plabel}**")
                    _part = _cs.get(_per) or {}
                    _hits = [h for h in (_part.get('hits') or [])
                             if isinstance(h, dict)][:5]
                    if not _hits:
                        st.caption(_part.get('reason') or '窗口内未命中')
                        continue
                    for _h in _hits:
                        _dir = _h.get('direction') or '-'
                        # 中国习惯：看涨红、看跌绿
                        _icon = ('🔴' if _dir == '看涨' else
                                 '🟢' if _dir == '看跌' else '⚪')
                        st.markdown(
                            f"- {_icon} **{_h.get('name', '-')}**"
                            f"（{_h.get('date', '-')}）· {_dir}")
                        _det = _h.get('detail') or {}
                        # W83：重点观察结论单独高亮，其余量化明细仍按 kv 展示
                        _focus = _det.get('重点观察')
                        if _focus:
                            st.markdown(f"　**重点观察**：{_focus}")
                        _rest = {k: v for k, v in _det.items()
                                 if k != '重点观察'}
                        if _rest:
                            st.caption(" · ".join(
                                f"{k}={v}" for k, v in _rest.items()))

    # ---------- W86 主力建仓 / 吸筹研判（四层证据链，仅展示不进分） ----------
    from mystery.apps.reports import result_fields as _rf86
    _acc = _rf86.accumulation_of(d)
    if _acc and _acc.get('level') != 'unknown':
        _lvl = str(_acc.get('level_cn') or '—')
        _acc_ok = _acc.get('level') in ('strong', 'medium')
        with st.expander(f"主力建仓 / 吸筹研判：{_lvl}", expanded=_acc_ok):
            st.caption(
                f"阶段 {_acc.get('stage') or '—'} · 按 Mistery 四层证据链"
                "（位置 / 量能 / 筹码 / 阶段）推导，仅展示不进综合分")
            _m1, _m2, _m3, _m4 = st.columns(4)
            _m1.metric("较近120高点回撤", f"{_acc.get('retrace_pct')}%"
                       if _acc.get('retrace_pct') is not None else '—')
            _m2.metric("量能状态", str(_acc.get('vol_state') or '—'))
            _m3.metric("20日涨跌", f"{_acc.get('chg_20d')}%"
                       if _acc.get('chg_20d') is not None else '—')
            _m4.metric("年线", "站上" if _acc.get('above_year') is True else
                       ("跌破" if _acc.get('above_year') is False else '未知'))
            for _ev in (_acc.get('evidence') or []):
                st.markdown(f"- {_ev}")
            st.info(str(_acc.get('conclusion') or '—'))

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

    st.divider()
    st.subheader("通达信自选板块同步")
    st.caption("本地 T0002/blocknew/*.blk（只读，覆盖式同步，与通达信保持一致）。"
               "勾选要同步的板块，未勾选的不会同步；已同步板块默认勾选。")

    def _run_sync(boards):
        with st.spinner("从通达信同步自选板块..."):
            r = _wl.sync_tdx_boards(load_config(), boards=boards)
        if r.get("boards"):
            lines = [f"已同步 {len(r['boards'])} 个板块，合计 {r['total']} 只"
                     f"（自选股 +{r['watchlist_added']} / -{r['watchlist_removed']}）"]
            for bn, binfo in r['boards'].items():
                lines.append(f"- {bn}（{binfo['file']}）：{binfo['n']} 只")
            st.success("\n".join(lines))
            st.rerun()
        else:
            st.warning("未找到通达信自选板块（T0002/blocknew/*.blk；请设置 "
                       "TDX_VIPDOC_DIR / TDX_BLOCKNEW_DIR）")

    try:
        available = _wl.list_tdx_board_names(load_config())
    except Exception:
        available = []
    current = _wl.list_tdx_boards()
    default_sel = [b for b in current if b in available]

    if not available:
        st.info("未检测到通达信自选板块（T0002/blocknew/*.blk；请设置 "
                "TDX_VIPDOC_DIR / TDX_BLOCKNEW_DIR）。")
        if st.button("同步全部自选板块", key="sync_boards_all_btn", type="primary"):
            _run_sync(None)
    else:
        sel = st.multiselect("选择要同步的自选板块", available,
                             default=default_sel, key="board_sync_sel")
        c1, c2 = st.columns(2)
        if c1.button("同步所选板块", key="sync_boards_sel_btn", type="primary"):
            if not sel:
                st.warning("请至少勾选一个板块")
            else:
                _run_sync(sel)
        if c2.button("同步全部板块", key="sync_boards_all_btn"):
            _run_sync(None)

    if st.button("仅导入 zxg.blk（合并不覆盖）", key="import_zxg_btn"):
        with st.spinner("导入中..."):
            r = _wl.import_from_tdx(load_config())
        if r.get('path'):
            st.success(f"导入 {r['imported']} 只（已存在跳过 {r['skipped']}）"
                       f" ← {r['path']}")
            st.rerun()
        else:
            st.warning("未找到通达信自选文件 zxg.blk（请设置 TDX_VIPDOC_DIR / "
                       "TDX_BLOCKNEW_DIR，默认查 /mnt/c/new_tdx、/mnt/new_tdx 的 "
                       "T0002/blocknew）")

    # 已同步板块总览 + 按板块扫描
    boards = _wl.list_tdx_boards()
    if boards:
        st.divider()
        st.subheader("已同步自选板块")
        cols = st.columns(min(len(boards), 4))
        for i, bn in enumerate(boards):
            with cols[i % len(cols)]:
                n = len(_wl.codes_of_board(bn))
                st.caption(f"**{bn}**：{n} 只")
        st.divider()
        st.subheader("按自选板块扫描")
        bsel = st.selectbox("选择板块", boards, key="board_scan_sel")
        if st.button("🚀 后台扫描该板块", key="board_scan_btn",
                     use_container_width=True):
            _bg_launch(f"板块:{bsel}",
                       lambda cb, holder, _b=bsel: scan_market(
                           board=_b, include_detail=True,
                           progress_cb=cb, job_holder=holder))
            st.rerun()

    kw_col, add_col = st.columns([3, 1])
    with kw_col:
        sym = _stock_pick_select("添加自选（输入名称搜索）", key="wl_add_pick")
        _wl_add_name = sym[1]
    if add_col.button("添加", type="primary"):
        if sym[0]:
            _wl.add_to_watchlist(sym[0], name=_wl_add_name, source='manual')
            st.rerun()
        else:
            st.error("未找到股票")

    items = _wl.load_watchlist_items()
    if not items:
        st.info("自选股为空")
        return
    # 板块快照一次加载，避免每只重复 IO
    _boards_snap = _wl.load_tdx_boards()
    st.caption(f"共 {len(items)} 只")
    for it in items:
        sym = it['symbol']
        name = it['name'] or '未知'
        blist = _wl.boards_of_symbol(sym, _boards_snap)
        src = _wl.source_label(it['source'], it.get('source_file') or '')
        if blist:
            src = f"{src} ｜ {'·'.join(blist)}"
        c1, c2, c3, c4, c5 = st.columns([1.1, 1.4, 1.8, 0.7, 0.7])
        c1.markdown(f"`{sym}`")
        c2.write(name)
        c3.write(src)
        if c4.button("分析", key=f"an_{sym}"):
            st.session_state['_pending_symbol'] = sym
            st.session_state['subview'] = None
            st.rerun()
        if c5.button("删除", key=f"del_{sym}"):
            _wl.remove_from_watchlist(sym)
            st.rerun()

def view_stock():
    st.header("个股分析（输入名称或代码搜索，选中即分析）")

    # URL query 直达（扫描结果表格「详情」链接 ?stock=600519.SH&nav_key=...&nav_idx=...）
    try:
        _qp = st.query_params
        if _qp.get('stock'):
            st.session_state['_pending_symbol'] = _qp.get('stock')
            # 恢复扫描列表导航上下文（无 nav_key 时为独立查看，清空导航）
            nav_key = _qp.get('nav_key')
            if nav_key:
                nav_rows = _nav_cache_get(nav_key)
                if nav_rows:
                    try:
                        nav_idx = int(_qp.get('nav_idx') or 0)
                    except (TypeError, ValueError):
                        nav_idx = 0
                    nav_idx = max(0, min(nav_idx, len(nav_rows) - 1))
                    st.session_state['stock_nav_list'] = nav_rows
                    st.session_state['stock_nav_idx'] = nav_idx
                else:
                    st.session_state['stock_nav_list'] = None
                    st.session_state['stock_nav_idx'] = None
            else:
                st.session_state['stock_nav_list'] = None
                st.session_state['stock_nav_idx'] = None
            st.query_params.clear()
    except Exception:
        pass

    # 检查是否有扫描导航上下文
    nav_list = st.session_state.get('stock_nav_list')
    nav_idx = st.session_state.get('stock_nav_idx')
    has_nav = nav_list and nav_idx is not None
    
    # 显示导航按钮（如果有导航上下文）
    if has_nav:
        nav_cols = st.columns([1, 2, 1, 1])
        with nav_cols[0]:
            if st.button("← 上一只", disabled=(nav_idx == 0)):
                st.session_state['stock_nav_idx'] = nav_idx - 1
                st.rerun()
        with nav_cols[2]:
            if st.button("下一只 →", disabled=(nav_idx >= len(nav_list) - 1)):
                st.session_state['stock_nav_idx'] = nav_idx + 1
                st.rerun()
        with nav_cols[3]:
            if st.button("返回列表"):
                st.session_state.pop('stock_nav_list', None)
                st.session_state.pop('stock_nav_idx', None)
                st.rerun()
        
        # 显示当前位置
        st.caption(f"扫描结果 {nav_idx + 1} / {len(nav_list)}")
    
    # 处理导航跳转
    if has_nav and nav_list:
        pending = nav_list[nav_idx].get('symbol')
        st.session_state.pop('_pending_symbol', None)  # 清除待分析标记，避免重复分析
    else:
        pending = st.session_state.pop('_pending_symbol', None)
    
    # 名称搜索 selectbox（缓存全市场列表，输入名称即时过滤，无需回车）
    code, name = _stock_pick_select("选择股票（输入名称搜索）",
                                    key="stock_pick",
                                    default_code=pending or "")
    if not code:
        st.info("未匹配到股票，请输入完整代码或名称")
        return
    do_analyze = st.button("分析", type="primary") or bool(pending)
    if do_analyze:
        with st.spinner(f"分析中（{name} {code}）..."):
            try:
                r = _service().analyze_one_stock(code)
                d = r.to_dict()
                st.session_state['stock_analysis'] = d
                st.session_state['stock_analysis']['_input'] = f"{name}（{code}）"
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
            blist = _wl.boards_of_symbol(cur, _wl.load_tdx_boards())
            if blist:
                st.caption(f"所属自选板块：{'·'.join(blist)}")
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

def _stock_pick_select(label: str = "选择股票（输入名称搜索）",
                       key: str = "stock_pick",
                       default_code: str = "") -> tuple:
    """名称搜索 selectbox：选项「名称（代码）」，返回 (symbol, name)。

    默认值 default_code 优先匹配选项；匹配不到时附加一条「未知（代码）」候选，
    并把该选项强制写入 session_state[key]（覆盖旧的 selectbox 会话值）。
    """
    options = _stock_pick_options()
    default_idx = 0
    if default_code:
        try:
            dc = normalize_symbol(default_code)
        except Exception:
            dc = default_code
        matched = None
        for i, o in enumerate(options):
            if o.endswith(f"（{dc}）"):
                matched, default_idx = i, i
                break
        if matched is None:
            options = [f"未知（{dc}）"] + options
            default_idx = 0
        st.session_state[key] = options[default_idx]
    pick = st.selectbox(label, options, index=default_idx, key=key)
    if "（" in pick:
        name, code = pick.rsplit("（", 1)
        return code[:-1], name
    return pick, ""

@st.cache_data(ttl=3600, max_entries=16, show_spinner=False)
def _plot_cache(symbol: str, freq: str, data_ver: str = ""):
    """缠论图 Figure（plotly 自绘；czsc 已装且数据可用时返回，否则 None）。

    周期 1d/1w/1M 由 service 拉取（周/月=日K重采样），czsc 结构在 adapter 内
    重建。W41：缓存键含 data_ver（详情页传 "trade_date|bars_fingerprint前8"），
    盘后 sync 出新 K → 指纹变 → 自然 miss，杜绝「分和图对不上」（旧键只有
    (symbol, freq)，进程不重启永远画旧中枢/笔）；另加 ttl=3600 挡同日补洞。
    data_ver 不参与计算，只做缓存代际令牌。
    """
    try:
        from mystery.adapters.czsc_adapter import CzscAdapter
        series = _service().market.fetch_bars(symbol, freq)
        return CzscAdapter().plot_figure(series)
    except Exception as e:  # noqa: BLE001
        st.warning(f"缠论图生成失败，降级文本展示: {str(e)[:80]}")
        return None

@st.cache_data(ttl=3600, max_entries=4, show_spinner=False)
def _lightweight_cache(symbol: str, freq: str, data_ver: str = ""):
    """官方 lightweight 校验图 HTML（plot_czsc(c, output='html')）；失败返回 ''。

    仅作校验/降级对照，主图始终用 plotly 自绘中枢盒（_plot_cache）。
    缓存代际令牌 data_ver 同 _plot_cache（W41）。
    """
    try:
        from mystery.adapters.czsc_adapter import CzscAdapter
        series = _service().market.fetch_bars(symbol, freq)
        return CzscAdapter().plot_lightweight_html(series)
    except Exception:  # noqa: BLE001
        return ""

__all__ = [
    'render_stock',
    '_add_watchlist_widget',
    'view_watchlist_subpage',
    'view_stock',
    '_stock_pick_select',
    '_plot_cache',
    '_lightweight_cache',
]
