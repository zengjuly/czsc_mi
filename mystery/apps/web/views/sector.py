from __future__ import annotations
import logging
import streamlit as st
from .components import *
# W64：拆分后跨 view 复用函数需显式导入（原单文件 app.py 同命名空间可直接调）。
from .scan import _render_scan_table

def view_sector():
    st.header("板块钻取（真实指数，非成分股抽样）")
    svc = _service()
    meta = svc.market.db.get_sector_meta(active_only=True)
    names = sorted({f"{m[1]}（{m[0]}）" for m in meta if m[1]})
    pick = st.selectbox("选择板块", names)
    if not pick:
        return
    s_code = pick.split("（")[-1][:-1]
    s_name = pick.rsplit("（", 1)[0]
    ind = svc.sector.get_sector(s_code)
    st.metric("行业强度分（0~25）", _fmt(ind.get('score')),
              delta="向上" if ind.get("up") else "向下")
    c_top, c_all = st.columns(2)
    if c_top.button("分析板块成分股 Top10", type="primary",
                    use_container_width=True):
        stocks = svc.market.db.get_sector_stocks(s_code)[:10]
        if not stocks:
            st.warning(
                f"该板块暂无成分股数据（stock_sector_rel 未同步 {s_code}）。"
                f"执行 `czsc-mi sector-sync --sector {s_code}` 同步后重试，"
                "或改用板块强度表/全市场扫描。")
        else:
            with st.spinner(f"分析 {len(stocks)} 只成分股..."):
                rows = scan_market(universe=stocks, include_detail=False,
                                   scan_type=f"sector:{s_name}")
                st.session_state['sector_results'] = rows
    if c_all.button("🚀 后台扫描全部成分股", use_container_width=True):
        stocks = svc.market.db.get_sector_stocks(s_code)
        if not stocks:
            st.warning(
                f"该板块暂无成分股数据（stock_sector_rel 未同步 {s_code}）。"
                f"执行 `czsc-mi sector-sync --sector {s_code}` 同步后重试。")
        else:
            _bg_launch(f"板块:{s_name}({len(stocks)})",
                       lambda cb, holder, _st=stocks, _nm=s_name: scan_market(
                           universe=_st, include_detail=True,
                           progress_cb=cb, job_holder=holder,
                           scan_type=f"sector:{_nm}"))
            st.rerun()
    _render_bg_tasks()
    rows = st.session_state.get('sector_results')
    if rows:
        st.caption(f"板块成分股 Top10 结果（共 {len(rows)} 只，按分降序）")
        _render_scan_table(rows, key="sector")

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

__all__ = [
    'view_sector',
    'view_sector_strength',
    '_sector_change_pct',
    '_sector_strength_table',
]
