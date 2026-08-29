"""mystery.apps.web.app — Streamlit 前端（P3：只调 Service，session 只存结果 dict）。

三视图：个股分析（支持代码/名称搜索 + 自选股）/ 全市场扫描 / 板块钻取。
渲染与计算分离：结果存 session_state，按钮后 fall-through 到展示区（不用 st.stop）。
运行：streamlit run mystery/apps/web/app.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import streamlit as st  # noqa: E402
import streamlit.components.v1 as stc  # noqa: E402

st.set_page_config(page_title="Mistery 趋势交易分析", layout="wide")

from mystery.adapters.codes import normalize_symbol  # noqa: E402
from mystery.config import load_config, output_dir  # noqa: E402
from mystery.services.analyze import AnalysisService, chan_enabled  # noqa: E402
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


@st.cache_data(ttl=600, show_spinner=False)
def _name_map_cached() -> dict:
    """全市场 {code: name} 映射（缓存 10 分钟，侧栏自选股展示用）。"""
    try:
        svc = _service()
        return {s['code']: s['name'] for s in svc.market.fetch_stock_list()}
    except Exception:
        return {}


@st.cache_data(show_spinner=False)
def _plot_cache(symbol: str, trade_date: str) -> str:
    """缠论图 HTML（czsc 已装且 chan 开启时可用；失败/未装 → ''）。"""
    try:
        from mystery.adapters.czsc_adapter import CzscAdapter
        series = _service().market.fetch_bars(symbol, '1d')
        return CzscAdapter().plot_html(series)
    except Exception as e:  # noqa: BLE001
        st.warning(f"缠论图生成失败，降级文本展示: {str(e)[:80]}")
        return ""


# ================= 展示函数（模块级，先定义后调用） =================
def render_stock(d: dict):
    """个股页：指标卡 + 缠论卡 + 明细（只读 result dict，不再计算）。"""
    st.subheader(f"{d.get('name', '')} {d.get('symbol', '')}  "
                 f"({d.get('trade_date', '')})")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("综合评分", _fmt(d.get('score')))
    c2.metric("操作建议", d.get('advice', '-'))
    c3.metric("最新价", _fmt(d.get('price')))
    c4.metric("真三振", "✅" if d.get('true_resonance') else "❌")
    c5.metric("行业", d.get('sector', {}).get('行业名称', '-'))

    # 缠论卡（只读 chan；W3-A：czsc 已装且 chan 非空 → 嵌入 plot_czsc HTML）
    chan = d.get('chan', {}) or {}
    if chan:
        with st.expander("缠论结构（czsc，仅展示）", expanded=True):
            plot_html = st.session_state.get('stock_plot', '')
            if plot_html:
                stc.html(plot_html, height=640, scrolling=True)
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

    fin = d.get('financial', {}) or {}
    if fin:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("PE", _fmt(fin.get('PE')))
        c2.metric("PB", _fmt(fin.get('PB')))
        c3.metric("ROE", _fmt(fin.get('roe')))
        c4.metric("报告期", str(fin.get('report_date', '-')))


# ================= 侧栏自选股 =================
def render_watchlist_sidebar():
    st.sidebar.subheader("自选股")
    codes = _wl.load_watchlist()
    if codes:
        stock_map = _name_map_cached()
        labels = {c: f"{c} {stock_map.get(c, '')}".strip() for c in codes}
        sel = st.sidebar.multiselect("自选列表（可多选分析）",
                                     options=codes, default=codes[-1:],
                                     format_func=lambda c: labels.get(c, c))
        c1, c2 = st.sidebar.columns(2)
        if c1.button("分析所选", type="primary", use_container_width=True):
            with st.spinner("分析自选股..."):
                rows = scan_market(watchlist=sel, include_detail=False)
                st.session_state['scan_results'] = rows
                st.session_state['_wl_analyzed'] = True
        if c2.button("全部移除", use_container_width=True):
            _wl.save_watchlist([])
            st.rerun()
    else:
        st.sidebar.caption("自选股为空，可在个股页加入")


# ================= 视图 =================
def view_stock():
    st.header("个股分析（支持代码或股票名搜索）")
    c1, c2 = st.columns([3, 1])
    text = c1.text_input("输入代码或名称", "sh600519").strip()
    do_analyze = c2.button("分析", type="primary")
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
                    # 缠论图（W3-A）：仅 chan 非空且 czsc 已装时生成
                    st.session_state['stock_plot'] = \
                        _plot_cache(d['symbol'], d.get('trade_date', '')) \
                        if d.get('chan') else ''
                except Exception as e:
                    st.error(f"分析失败: {e}")
                    st.session_state.pop('stock_analysis', None)
                    st.session_state.pop('stock_plot', None)
    d = st.session_state.get('stock_analysis')
    if d:
        render_stock(d)
        cur = d.get('symbol', '')
        wl = _wl.load_watchlist()
        if cur in wl:
            if st.button("从自选股移除", use_container_width=True):
                _wl.remove_from_watchlist(cur)
                st.rerun()
        else:
            if st.button("加入自选股", use_container_width=True):
                _wl.add_to_watchlist(cur)
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
        st.dataframe([{'代码': r['symbol'], '名称': r.get('name', ''),
                       '评分': r.get('score'), '建议': r.get('advice', ''),
                       '日期': r.get('trade_date', '')} for r in rows],
                     use_container_width=True, hide_index=True)


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
        with st.spinner(f"分析 {len(stocks)} 只成分股..."):
            rows = scan_market(universe=stocks, include_detail=False)
            st.session_state['sector_results'] = rows
    rows = st.session_state.get('sector_results')
    if rows:
        st.dataframe([{'代码': r['symbol'], '名称': r.get('name', ''),
                       '评分': r.get('score'), '建议': r.get('advice', ''),
                       '行业': r.get('sector', {}).get('行业名称', '-')}
                      for r in rows], use_container_width=True, hide_index=True)


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
    st.header("真三振池（最近一次扫描结果）")
    job = latest_scan_job(load_config())
    if job:
        rows = scan_results_of(job, signal="true_resonance")
        st.caption(f"来自 scan job #{job}，共 {len(rows)} 只真三振")
        if rows:
            st.dataframe([{'代码': r['symbol'], '名称': r.get('name', ''),
                           '评分': r.get('score'), '建议': r.get('advice', ''),
                           '日期': r.get('trade_date', '')} for r in rows],
                         use_container_width=True, hide_index=True)
        else:
            st.info("最近一次扫描无真三振标的")
    c1, c2 = st.columns([2, 1])
    limit = c1.number_input("扫描只数", 1, 5000, 100)
    if c2.button("用自选/limit 扫描并更新", type="primary"):
        with st.spinner("扫描中（单票失败自动跳过）..."):
            rows = scan_market(limit=int(limit), include_detail=True,
                               cfg=load_config())
            st.session_state['scan_results'] = rows
            st.rerun()


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
        "chan 开关": "开" if chan_enabled() else "关（默认）",
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


def main():
    page = st.sidebar.radio("导航", ["个股分析", "全市场扫描", "板块钻取",
                                    "真三振池", "系统状态", "板块强度表"])
    st.sidebar.caption("唯一计算入口：mystery.services.analyze.analyze_one_stock")
    st.sidebar.caption(
        f"chan 开关: {'开' if os.environ.get('MYSTERY_CHAN_ENABLED', '0') not in ('0', 'false') else '关'}")
    render_watchlist_sidebar()
    if page == "个股分析":
        view_stock()
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
