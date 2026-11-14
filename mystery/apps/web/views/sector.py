from __future__ import annotations
import logging
import streamlit as st
from .components import *
# W64：拆分后跨 view 复用函数需显式导入（原单文件 app.py 同命名空间可直接调）。
from .scan import _render_scan_table
# W87：板块钻取页字段映射（纯函数，零 Streamlit，便于单测）。
from .sector_fields import related_stock_rows, sector_drilldown_view

logger = logging.getLogger(__name__)


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

    # W87：板块自身研判（与每日报告「行业板块」区块同口径）
    _render_sector_drilldown(s_name, s_code, ind)

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


@st.cache_data(ttl=600, max_entries=8, show_spinner=False)
def _sector_drilldown_cached(code: str, name: str,
                             top_n: int, corr_window: int) -> dict:
    """W87：板块研判取数（只读）。

    参数全部可哈希，便于 ``st.cache_data`` 缓存，避免每次 rerun 重算
    板块指数与成分股的 Pearson 相关矩阵。研判失败降级，不抛异常。
    """
    try:
        from mystery.services.sector_analysis import analyze_sector
        db = _service().market.db
        return analyze_sector(db, code, name,
                               top_n=top_n, corr_window=corr_window)
    except Exception as exc:  # noqa: BLE001 研判失败不阻塞钻取页
        logger.warning("板块钻取研判失败 %s(%s): %s", name, code, exc)
        return {"name": name, "code": code, "has_data": False,
                "related": [], "accumulation": None}


def _render_sector_drilldown(name: str, code: str, ind: dict) -> None:
    """W87：板块钻取页「板块分析内容」（与日报 Excel/HTML 板块区块同口径）。

    只读展示：不重算个股评分、不改信号、不改 ``RULE_VER``、不改分析缓存。
    板块清单仍沿用 ``get_sector_meta``，代码由 ``analyze_sector`` 内部归一。
    """
    st.subheader("📊 板块研判（真实板块指数 K 线，非成分股抽样）")
    try:
        from mystery.services import runtime_settings as rts
        opts = rts.sector_options_normalized()
        top_n = int(opts.get("top_n", 8))
        corr_window = int(opts.get("corr_window", 60))
    except Exception:  # noqa: BLE001 配置异常时落生产默认
        top_n, corr_window = 8, 60

    d = _sector_drilldown_cached(code, name, top_n, corr_window)
    v = sector_drilldown_view(d)

    if not v["has_data"]:
        # 无指数 K 线时保留原有「行业强度分」，成分股分析功能不受影响。
        st.info("该板块暂无足够的指数 K 线（sector_kline 未同步或不足 30 根），"
                "板块研判不可用；下方成分股分析仍可正常使用。")
        st.metric("行业强度分（0~25）", _fmt(ind.get('score')),
                  delta="向上" if ind.get("up") else "向下")
        return

    st.caption(f"数据日期 {v['date']} · 板块代码 {v['code']} · "
               f"口径同每日报告「行业板块」（相关窗口 {corr_window} 日 / "
               f"Top {top_n}）")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("最新收盘", v["close"])
    c2.metric("近5日涨跌", v["chg_5d"])
    c3.metric("近20日涨跌", v["chg_20d"])
    c4.metric("年线位置", v["year"])
    c5.metric("板块强度分", v["strength"])
    st.caption(f"MA 排列：{v['ma']}")

    k1, k2 = st.columns(2)
    k1.markdown(f"**日K形态**：{v['candle_daily']}")
    k2.markdown(f"**周K形态**：{v['candle_weekly']}")

    # W86：主力建仓 / 吸筹研判（四层证据链，与日报/个股页同口径）
    with st.expander(f"🧭 主力建仓 / 吸筹研判：{v['acc_level']}", expanded=True):
        a1, a2, a3, a4 = st.columns(4)
        a1.metric("较近120高点回撤", v["acc_retrace"])
        a2.metric("量能状态", v["acc_vol"])
        a3.metric("阶段", v["acc_stage"])
        a4.metric("近20日", v["chg_20d"])
        for ev in v["acc_evidence"]:
            st.markdown(f"- {ev}")
        st.info(v["acc_conclusion"])

    # 强相关个股：本期 Top10 扫描结果命中时自动补齐评分/建议/吸筹（只读关联）
    rows = related_stock_rows(d.get("related") or [],
                             st.session_state.get("sector_results"))
    with st.expander(f"🔗 与板块强相关的个股（{len(rows)} 只 · "
                     f"近 {corr_window} 日收益率 Pearson 相关 Top {top_n}）",
                     expanded=bool(rows)):
        if not rows:
            st.caption("暂无强相关个股（成分股日 K 与板块指数有效重叠样本"
                       "不足 15 日）。")
        else:
            st.dataframe(rows, use_container_width=True, hide_index=True,
                         column_config={
                             "相关系数": st.column_config.NumberColumn(
                                 format="%.3f")})
            st.caption("「本期已分析 / 评分 / 操作建议 / 主力吸筹」在点击"
                       "「分析板块成分股 Top10」且命中该股后自动补齐"
                       "（只读关联，不重算个股）。")


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

@st.cache_data(ttl=600, max_entries=1, show_spinner=False)
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
    '_sector_drilldown_cached',
    '_render_sector_drilldown',
    'sector_drilldown_view',
    'related_stock_rows',
]
