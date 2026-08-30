"""mystery.apps.web.app — Streamlit 前端（P3：只调 Service，session 只存结果 dict）。

三视图：个股分析（支持代码/名称搜索 + 自选股）/ 全市场扫描 / 板块钻取。
渲染与计算分离：结果存 session_state，按钮后 fall-through 到展示区（不用 st.stop）。
运行：streamlit run mystery/apps/web/app.py
"""
from __future__ import annotations

import logging
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import streamlit as st  # noqa: E402

st.set_page_config(page_title="Mistery 趋势交易分析", layout="wide")

logger = logging.getLogger(__name__)

from mystery.adapters.codes import normalize_symbol  # noqa: E402
from mystery.apps.reports.excel_report import excel_bytes  # noqa: E402
from mystery.config import load_config, output_dir  # noqa: E402
from mystery.core.scan_signals import classify  # noqa: E402
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


@st.cache_data(ttl=600, show_spinner=False)
def _stock_pick_options() -> list:
    """全市场「名称（代码）」选项列表（selectbox 名称搜索用，缓存 10 分钟）。"""
    svc = _service()
    stocks = svc.market.fetch_stock_list()
    seen, out = set(), []
    for s in stocks:
        code = str(s.get('code', ''))
        name = str(s.get('name') or '')
        if code in seen:
            continue
        seen.add(code)
        try:
            norm = normalize_symbol(code)
        except Exception:
            norm = code
        out.append(f"{name or '未知'}（{norm}）")
    out.sort()
    return out


# ================= 后台扫描任务（模块级，进程内跨 rerun 持久） =================
_BG_LOCK = threading.Lock()
_BG_TASKS: dict = {}


def _bg_launch(label: str, fn) -> str:
    """启动后台扫描任务。fn(cb, job_holder) -> rows（cb(done,total) 报进度）。"""
    tid = uuid.uuid4().hex[:8]
    with _BG_LOCK:
        _BG_TASKS[tid] = {"id": tid, "label": label, "status": "running",
                          "done": 0, "total": 0, "job_id": None,
                          "results": [], "error": "", "created": time.time()}

    def _run():
        holder: list = []

        def cb(done, total):
            with _BG_LOCK:
                t = _BG_TASKS.get(tid)
                if t:
                    t["done"], t["total"] = done, total

        try:
            rows = fn(cb, holder)
            with _BG_LOCK:
                t = _BG_TASKS.get(tid)
                if t:
                    t["status"] = "done"
                    t["results"] = rows
                    t["job_id"] = holder[0] if holder else None
        except Exception as e:  # noqa: BLE001
            with _BG_LOCK:
                t = _BG_TASKS.get(tid)
                if t:
                    t["status"] = "error"
                    t["error"] = str(e)[:200]

    threading.Thread(target=_run, daemon=True).start()
    return tid


@st.fragment(run_every=2.0)
def _bg_running_fragment():
    """运行中后台任务进度（每 2 秒自动刷新）。"""
    with _BG_LOCK:
        rt = sorted([t for t in _BG_TASKS.values() if t["status"] == "running"],
                    key=lambda t: t["created"], reverse=True)
    for t in rt:
        pct = (t["done"] / t["total"]) if t["total"] else 0.0
        st.progress(min(1.0, pct),
                    text=f"⏳ {t['label']} 运行中 {t['done']}/{t['total']}（自动刷新）")


def _recent_jobs(limit: int = 20) -> list:
    """最近扫描任务 [(id, trade_date, started_at, n_ok, n_fail), ...] 降序。"""
    svc = _service()
    conn = svc.market.db._connect()
    try:
        return conn.execute(
            "SELECT id, trade_date, started_at, n_ok, n_fail "
            "FROM scan_jobs ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    finally:
        conn.close()


def _render_bg_tasks():
    """后台任务列表：运行中自动刷新 + 完成/失败态 + 查看结果。"""
    with _BG_LOCK:
        tasks = sorted(_BG_TASKS.values(), key=lambda t: t["created"], reverse=True)
    if not tasks:
        st.caption("暂无后台任务")
        return
    _bg_running_fragment()
    for t in tasks:
        if t["status"] == "done":
            st.success(f"✅ {t['label']} 完成：{len(t['results'])} 只"
                       f"（job #{t['job_id']}）")
            if st.button(f"查看结果（{t['label']}）", key=f"bg_view_{t['id']}"):
                st.session_state['bg_view_id'] = t['id']
        elif t["status"] == "error":
            st.error(f"❌ {t['label']} 失败：{t['error'][:120]}")
    view_id = st.session_state.get('bg_view_id')
    if view_id:
        with _BG_LOCK:
            t = _BG_TASKS.get(view_id)
        if t and t["results"]:
            _render_scan_table(t["results"], key=f"bg_{view_id}")


def _render_scan_table(rows: list, key: str = "scan"):
    """扫描结果表格（代码/名称/评分/建议/筹码/日期）+ 加入自选。"""
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
    _render_excel_download(rows, key=key)
    _add_watchlist_widget(rows, key=key)


def _needs_detail(r: dict) -> bool:
    """行是否缺个股详情（web 扫描落库 include_detail=False，缺 VAP/平台明细）。"""
    m = r.get('mystery') or {}
    return not m.get('vap_atr') or not m.get('platform')


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
    st.header("个股分析（输入名称或代码搜索，选中即分析）")
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
    st.caption("后台扫描不阻塞页面：任务在后台线程执行，结果自动落库（scan_jobs），"
               "可随时回到本页/系统状态查看。")
    c1, c2, c3 = st.columns(3)
    with c1:
        limit = st.number_input("前台快速扫描只数", 1, 5000, 100)
    with c2:
        min_score = st.number_input("最低分", 0.0, 100.0, 0.0)
    if c3.button("开始前台扫描", type="primary"):
        with st.spinner("扫描中（单票失败自动跳过）..."):
            rows = scan_market(limit=int(limit), include_detail=False,
                               min_score=min_score or None)
            st.session_state['scan_results'] = rows
            st.session_state['scan_ts'] = len(rows)

    st.divider()
    st.subheader("后台扫描任务")
    b1, b2 = st.columns(2)
    if b1.button("🚀 后台扫描全部股票", type="primary", use_container_width=True):
        _bg_launch("全部股票",
                   lambda cb, holder: scan_market(include_detail=False,
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
                           watchlist=_wl, include_detail=False,
                           progress_cb=cb, job_holder=holder))
            st.rerun()
    _render_bg_tasks()

    st.divider()
    st.subheader("最近扫描任务与结果")
    jobs = _recent_jobs(limit=20)
    if not jobs:
        st.caption("暂无扫描记录（前台或后台扫描后出现）")
        return
    opts = {f"job#{j[0]} {j[1] or ''} 成功{j[3]} 失败{j[4]} {j[2] or ''}": j[0]
            for j in jobs}
    pick = st.selectbox("选择扫描任务查看结果", list(opts.keys()), key="scan_job_pick")
    job_id = opts[pick]
    sig = st.radio("信号过滤", ["全部", "真三振", "VAP-ATR突破", "筹码低位"],
                   horizontal=True, key="scan_job_sig")
    sig_map = {"全部": None, "真三振": "true_resonance",
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
            st.warning("该板块暂无成分股数据（stock_sector_rel 未同步该板块）。"
                       "可先执行板块成分同步，或改用板块强度表/全市场扫描。")
        else:
            with st.spinner(f"分析 {len(stocks)} 只成分股..."):
                rows = scan_market(universe=stocks, include_detail=False)
                st.session_state['sector_results'] = rows
    if c_all.button("🚀 后台扫描全部成分股", use_container_width=True):
        stocks = svc.market.db.get_sector_stocks(s_code)
        if not stocks:
            st.warning("该板块暂无成分股数据（stock_sector_rel 未同步该板块）。")
        else:
            _bg_launch(f"板块:{s_name}({len(stocks)})",
                       lambda cb, holder, _st=stocks: scan_market(
                           universe=_st, include_detail=False,
                           progress_cb=cb, job_holder=holder))
            st.rerun()
    _render_bg_tasks()
    rows = st.session_state.get('sector_results')
    if rows:
        st.caption(f"板块成分股 Top10 结果（共 {len(rows)} 只，按分降序）")
        _render_scan_table(rows, key="sector")


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
        _render_excel_download(rows, key="resonance")
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
    st.subheader("最近扫描任务与结果")
    jobs = _recent_jobs(limit=20)
    if jobs:
        opts = {f"job#{j[0]} {j[1] or ''} 成功{j[3]} 失败{j[4]} {j[2] or ''}": j[0]
                for j in jobs}
        pick = st.selectbox("选择扫描任务查看结果", list(opts.keys()),
                            key="sys_job_pick")
        job_id = opts[pick]
        sig = st.radio("信号过滤", ["全部", "真三振", "VAP-ATR突破", "筹码低位"],
                       horizontal=True, key="sys_job_sig")
        sig_map = {"全部": None, "真三振": "true_resonance",
                   "VAP-ATR突破": "vap_atr", "筹码低位": "chip_low"}
        rows = scan_results_of(job_id, signal=sig_map[sig])
        st.caption(f"job #{job_id}（{sig}）共 {len(rows)} 只")
        if rows:
            _render_scan_table(rows, key="sysjob")
        else:
            st.info("该任务无满足条件的标的")
    else:
        st.caption("尚无扫描记录（czsc-mi scan 或页面扫描后出现）")
    _render_bg_tasks()


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
