"""mystery.apps.web.app — Streamlit 前端（P3：只调 Service，session 只存结果 dict）。

三视图：个股分析 / 全市场扫描 / 板块钻取 —— 底层同一 analyze_one_stock，同股同分。
渲染与计算分离：结果存 session_state，按钮后 fall-through 到展示区（不用 st.stop）。
运行：streamlit run mystery/apps/web/app.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import streamlit as st  # noqa: E402

st.set_page_config(page_title="Mistery 趋势交易分析", layout="wide")

from mystery.services.analyze import AnalysisService  # noqa: E402
from mystery.services.scan import scan_market  # noqa: E402


@st.cache_resource
def _service():
    return AnalysisService({})


def _fmt(v, nd=2):
    if v is None:
        return "-"
    try:
        return f"{float(v):.{nd}f}"
    except Exception:
        return str(v)


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

    # 缠论卡（只读 chan）
    chan = d.get('chan', {}) or {}
    if chan:
        with st.expander("缠论结构（czsc，仅展示不进评分）", expanded=True):
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
    c4.metric("筹码集中度", m.get('checklist8', {}).get('满足数量', '-'))

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


# ================= 视图 =================
def view_stock():
    st.header("个股分析")
    code = st.text_input("股票代码", "sh600519").strip()
    if st.button("开始分析", type="primary"):
        with st.spinner("分析中..."):
            try:
                r = _service().analyze_one_stock(code)
                st.session_state['stock_analysis'] = r.to_dict()
            except Exception as e:
                st.error(f"分析失败: {e}")
                st.session_state.pop('stock_analysis', None)
    if 'stock_analysis' in st.session_state:
        render_stock(st.session_state['stock_analysis'])


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
              delta="向上" if ind.get('up') else "向下")
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


def main():
    page = st.sidebar.radio("导航", ["个股分析", "全市场扫描", "板块钻取"])
    st.sidebar.caption("唯一计算入口：mystery.services.analyze.analyze_one_stock")
    st.sidebar.caption(f"chan 开关: {'开' if os.environ.get('MYSTERY_CHAN_ENABLED', '0') not in ('0', 'false') else '关'}")
    if page == "个股分析":
        view_stock()
    elif page == "全市场扫描":
        view_scan()
    else:
        view_sector()


main()
