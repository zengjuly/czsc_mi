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
    st.caption("后台扫描不阻塞页面：任务在后台线程执行，结果自动落库（scan_jobs），"
               "可随时回到本页/系统状态查看。")
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
        _bg_launch("全部股票",
                   lambda cb, holder: scan_market(include_detail=True,
                                                  progress_cb=cb,
                                                  job_holder=holder))
        st.rerun()
    if b2.button("🚀 后台扫描全部自选股", use_container_width=True):
        wl = _wl.load_watchlist()
        if not wl:
            st.warning("自选股为空，先到「自选股」页添加")
        else:
            _bg_launch(f"全部自选股({len(wl)})",
                       lambda cb, holder, _wl=wl: scan_market(
                           watchlist=_wl, include_detail=True,
                           progress_cb=cb, job_holder=holder))
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
        _render_scan_table(rows, key="scanjob")
    else:
        st.info("该任务无满足条件的标的")

    rows = st.session_state.get('scan_results')
    if rows:
        st.divider()
        st.subheader("前台扫描结果")
        _render_scan_table(rows, key="scan")

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

def _render_scan_table(rows: list, key: str = "scan"):
    """扫描结果表格（代码可点击进入详情）+ 加入自选。
    
    「详情」列为超链接（?stock=<symbol>&nav_key=<nav_key>&nav_idx=<i>）；
    LinkColumn 默认新标签页打开（全新 session，session_state 不共享），
    因此导航列表存进程级缓存 _nav_cache_store（cache_resource 跨会话保留），
    nav_key 为该缓存的随机 key，进入详情页按 nav_key 恢复导航。
    """
    st.caption(f"共 {len(rows)} 只（按分降序）")
    _w3_line = wave3_summary_line(rows)
    st.caption(_w3_line or "三浪主升：本任务未含该策略（重新扫描后自动合并）")
    st.caption(_market_env_line_cached())
    
    # 存当前列表到进程级缓存（跨标签页/会话），供详情页恢复导航
    nav_key = _nav_cache_put(rows)
    
    # 构建表格数据
    table_data = _scan_table_data(rows, nav_key=nav_key)
    
    # 显示表格
    df = st.dataframe(
        table_data,
        use_container_width=True,
        hide_index=True,
        key=f"{key}_table",
        column_config={
            '详情': st.column_config.LinkColumn(
                '详情',
                help='点击查看个股分析详情',
                display_text='查看 →',
            ),
        }
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
    
    _render_excel_download(rows, key=key)
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

def _render_excel_download(rows: list, key: str = "scan"):
    """扫描结果表下方「下载 Excel 报告」按钮（汇总 + 每只个股详情，同 daily 格式）。

    首次点击补齐详情（web 扫描落库缺明细）并生成 xlsx bytes 存 session；
    再次点击直接用已生成文件下载，避免每次 rerun 重算。
    session key 绑定 rows 签名：切换任务/信号后自动作废旧文件。
    """
    if not rows:
        return
    sig = f"{len(rows)}:{rows[0].get('symbol')}:{rows[-1].get('symbol')}:{rows[0].get('trade_date', '')}"
    gen_key, data_key = f"{key}_xlsx_gen", f"{key}_xlsx_data_{sig}"
    if st.button(f"📥 下载 Excel 报告（{len(rows)} 只）", key=gen_key):
        with st.spinner(f"补齐 {len(rows)} 只个股详情并生成 Excel..."):
            enriched = _enrich_scan_rows(rows)
            try:
                st.session_state[data_key] = excel_bytes(enriched)
            except Exception as e:  # noqa: BLE001
                st.error(f"生成 Excel 失败: {e}")
                st.session_state.pop(data_key, None)
    if data_key in st.session_state:
        fname = f"扫描报告_{datetime.now().strftime('%Y%m%d')}.xlsx"
        st.download_button("💾 保存 Excel 报告", data=st.session_state[data_key],
                           file_name=fname,
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           key=f"{key}_xlsx_dl")
        st.caption("包含：汇总报告 + 每只个股详情（与每日报告同格式）")

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
    '_turn_tags',
    '_wave3_cell',
    '_sig_flag',
    '_yl_flag',
    '_mainwave_flag',
    '_main_wave_count',
]
