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


import streamlit as st  # noqa: E402


logger = logging.getLogger(__name__)

from mystery.apps.web.nav_cache import (nav_cache_get as _nav_cache_get,
                                        nav_cache_put as _nav_cache_put,
                                        nav_cache_clear as _nav_cache_clear)  # noqa: E402

from mystery.adapters.codes import normalize_symbol  # noqa: E402
from mystery.apps.reports.excel_report import excel_bytes  # noqa: E402
from mystery.config import load_config, output_dir  # noqa: E402
from mystery.core.scan_signals import classify  # noqa: E402
from mystery.core.bc_resonance import bc_resonance as _bc_resonance  # noqa: E402
from mystery.services.analyze import (AnalysisService, chan_enabled,  # noqa: E402
                                      chan_score_enabled)
from mystery.services.scan import (scan_market, latest_scan_job,  # noqa: E402
                                   scan_results_of)
from mystery.services import watchlist as _wl  # noqa: E402

@st.cache_resource
def _service():
    return AnalysisService(load_config())

@st.cache_data(ttl=600, show_spinner=False)
def _market_env_line_cached() -> str:
    """W37 大盘滤网一行摘要（10 分钟缓存；fetch_index 会话缓存双保险）。"""
    from mystery.services.analyze import market_env_line, market_env_summary
    try:
        return market_env_line(market_env_summary(_service().market))
    except Exception:  # noqa: BLE001
        return "[大盘滤网] 大盘未知"

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

@st.cache_resource
def _bg_store() -> dict:
    return {"lock": threading.Lock(), "tasks": {}}

def _bg_lock() -> threading.Lock:
    return _bg_store()["lock"]

def _bg_tasks() -> dict:
    return _bg_store()["tasks"]

def _bg_launch(label: str, fn) -> str:
    """启动后台扫描任务。fn(cb, job_holder) -> rows（cb(done,total) 报进度）。"""
    tid = uuid.uuid4().hex[:8]
    lock, tasks = _bg_lock(), _bg_tasks()
    with lock:
        tasks[tid] = {"id": tid, "label": label, "status": "running",
                      "done": 0, "total": 0, "job_id": None,
                      "results": [], "error": "", "created": time.time()}

    def _run():
        holder: list = []

        def cb(done, total):
            with lock:
                t = tasks.get(tid)
                if t:
                    t["done"], t["total"] = done, total

        try:
            rows = fn(cb, holder)
            with lock:
                t = tasks.get(tid)
                if t:
                    t["status"] = "done"
                    t["results"] = rows
                    t["job_id"] = holder[0] if holder else None
        except Exception as e:  # noqa: BLE001
            with lock:
                t = tasks.get(tid)
                if t:
                    t["status"] = "error"
                    t["error"] = str(e)[:200]

    threading.Thread(target=_run, daemon=True).start()
    return tid

@st.fragment(run_every=2.0)
def _bg_running_fragment():
    """运行中后台任务进度（每 2 秒自动刷新）。"""
    with _bg_lock():
        rt = sorted([t for t in _bg_tasks().values() if t["status"] == "running"],
                    key=lambda t: t["created"], reverse=True)
    for t in rt:
        pct = (t["done"] / t["total"]) if t["total"] else 0.0
        st.progress(min(1.0, pct),
                    text=f"⏳ {t['label']} 运行中 {t['done']}/{t['total']}（自动刷新）")

def _recent_jobs(limit: int = 20) -> list:
    """最近扫描任务 [(id, trade_date, started_at, n_ok, n_fail, scan_type), ...] 降序。"""
    svc = _service()
    conn = svc.market.db._connect()
    try:
        return conn.execute(
            "SELECT id, trade_date, started_at, n_ok, n_fail, scan_type "
            "FROM scan_jobs ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    finally:
        conn.close()

def _render_running_tasks(db_path: str):
    """W49：写库任务互斥锁 + 进度心跳——显示正在运行的任务及进度。

    心跳文件由 mystery.services.task_lock 写 <db>.tasks.json；
    running_tasks() 只列锁仍真实持有的条目（崩溃残留自动忽略）。
    """
    try:
        from mystery.services.task_lock import running_tasks
        tasks = running_tasks(db_path)
    except Exception as e:
        st.caption(f"任务心跳读取失败：{e}")
        return
    if not tasks:
        st.caption("无正在运行的任务")
        return
    st.subheader("正在运行的任务")
    for t in tasks:
        label = (t.get('label') or t.get('name') or '任务')
        line = label
        if t.get('pid'):
            line += f"（pid {t['pid']}"
            line += "，存活" if t.get('pid_alive') else "，进程已退出"
            line += "）"
        holder = (t.get('holder') or '').strip()
        if holder:
            line += f" · 持有者 {holder}"
        pct = t.get('pct')
        if pct is None:
            st.info(f"⏳ {line} · 进度未知")
        else:
            st.progress(min(max(float(pct), 0.0), 1.0),
                        text=f"{line} · {int(float(pct) * 100)}%"
                             f"（{t.get('done')}/{t.get('total')}）")

def _render_bg_tasks():
    """后台任务列表：运行中自动刷新 + 完成/失败态 + 查看结果。"""
    with _bg_lock():
        tasks = sorted(_bg_tasks().values(), key=lambda t: t["created"],
                       reverse=True)
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
                st.rerun()
        elif t["status"] == "error":
            st.error(f"❌ {t['label']} 失败：{t['error'][:120]}")
    view_id = st.session_state.get('bg_view_id')
    if view_id:
        with _bg_lock():
            t = _bg_tasks().get(view_id)
        if t and t["results"]:
            _render_scan_table(t["results"], key=f"bg_{view_id}")

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

def _clear_subview():
    st.session_state['subview'] = None

def _needs_detail(r: dict) -> bool:
    """行是否缺个股详情（web 扫描落库 include_detail=False，缺 VAP/平台明细）。"""
    m = r.get('mystery') or {}
    return not m.get('vap_atr') or not m.get('platform')

__all__ = [
    '_service',
    '_market_env_line_cached',
    '_fmt',
    '_stock_pick_options',
    '_bg_store',
    '_bg_lock',
    '_bg_tasks',
    '_bg_launch',
    '_bg_running_fragment',
    '_recent_jobs',
    '_render_running_tasks',
    '_render_bg_tasks',
    '_sig_text',
    '_vp_text',
    '_render_technical',
    '_clear_subview',
    '_needs_detail',
    'normalize_symbol',
    '_bc_resonance',
    'classify',
    'AnalysisService',
    'chan_enabled',
    'chan_score_enabled',
    'scan_market',
    'latest_scan_job',
    'scan_results_of',
    '_wl',
    'excel_bytes',
    '_nav_cache_get',
    '_nav_cache_put',
    '_nav_cache_clear',
    'load_config',
    'output_dir',
]
