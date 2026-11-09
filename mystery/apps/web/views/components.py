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
                                   scan_results_of, scan_job_meta)
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

# W75：后台任务「僵尸防护」——running 状态超过该秒数无任何进度/阶段更新，
# 视为疑似失联（线程崩溃 / 进程被 OOM 杀但字典残留），页面给出提示与清除入口，
# 不再无限转圈。逐票全市场扫描实测约 2 分钟、收尾限流后秒级，10 分钟足够宽松。
_BG_STALE_SECONDS = 600.0


def _bg_launch(label: str, fn) -> str:
    """启动后台扫描任务。fn(cb, job_holder) -> rows（cb(done,total,stage) 报进度）。

    W77：任务启动 / 进度 / 终态同步写入生产库 ``bg_task_log`` 台账。此前记录
    只在内存字典里，服务重启、跨会话、W75 清除卡住任务后全部消失；失败
    （并发锁拒绝、线程被重启打断）更是不留任何痕迹，用户「最近任务」看不到。
    台账写入全部经 bg_task_log 异常硬隔离，绝不影响扫描本身。
    """
    from mystery.services import bg_task_log as _btl
    tid = uuid.uuid4().hex[:8]
    lock, tasks = _bg_lock(), _bg_tasks()
    now = time.time()
    db_path = _service().market.db.db_path
    with lock:
        tasks[tid] = {"id": tid, "label": label, "status": "running",
                      "done": 0, "total": 0, "stage": "", "job_id": None,
                      "results": [], "error": "", "created": now,
                      "updated": now, "_refreshed": False}
    _btl.log_start(db_path, tid, label, created=now)
    # 进度写库节流：逐票回调可达数千次，每 5 秒或每 50 只才 UPDATE 一次，
    # 避免短事务拖慢全市场扫描；终态由 log_finish 兜底，不会丢最后一次。
    _last_log = {"ts": 0.0, "done": -1}

    def _run():
        holder: list = []

        def cb(done, total, stage=""):
            # W75：收尾阶段带 stage 文字（三浪评估/写库），逐票阶段 stage 为空。
            with lock:
                t = tasks.get(tid)
                if t:
                    t["done"], t["total"] = done, total
                    t["stage"] = stage or ""
                    t["updated"] = time.time()
            t_now = time.time()
            if (t_now - _last_log["ts"] >= 5.0
                    or done - _last_log["done"] >= 50):
                _last_log["ts"], _last_log["done"] = t_now, done
                _btl.log_progress(db_path, tid, done, total, stage)

        job_id = None
        try:
            rows = fn(cb, holder)
            job_id = holder[0] if holder else None
            with lock:
                t = tasks.get(tid)
                if t:
                    t["status"] = "done"
                    t["results"] = rows
                    t["job_id"] = job_id
                    t["updated"] = time.time()
            _btl.log_finish(db_path, tid, "done", job_id=job_id,
                            n_results=len(rows or []))
        except Exception as e:  # noqa: BLE001
            err = str(e)[:200]
            with lock:
                t = tasks.get(tid)
                if t:
                    t["status"] = "error"
                    t["error"] = err
                    t["updated"] = time.time()
            _btl.log_finish(db_path, tid, "error", error=err)

    threading.Thread(target=_run, daemon=True).start()
    return tid

@st.fragment(run_every=2.0)
def _bg_running_fragment():
    """运行中后台任务进度（每 2 秒自动刷新）。

    W75：区分「逐票扫描」「收尾阶段」「疑似失联」三种态；失联态提供清除按钮，
    避免 W69 三浪收尾卡顿 / 进程重启残留导致进度条永远停在 100%。
    W77：检测到内存任务刚进入终态时触发一次全页刷新，让同页「最近扫描
    任务与结果」下拉立即出现新 job（此前 fragment 只刷进度条，完成态与
    下拉列表都不更新，用户以为记录没生成）。
    """
    from mystery.services import bg_task_log as _btl
    now = time.time()
    with _bg_lock():
        rt = sorted([t for t in _bg_tasks().values() if t["status"] == "running"],
                    key=lambda t: t["created"], reverse=True)
    for t in rt:
        pct = (t["done"] / t["total"]) if t["total"] else 0.0
        stage = t.get("stage") or ""
        idle = now - (t.get("updated") or t["created"])
        if idle > _BG_STALE_SECONDS:
            st.warning(f"⚠️ {t['label']} 已 {int(idle)} 秒无进度更新，疑似失联/卡住。"
                       f"可清除后重试（大池收尾已自动限流，正常扫描约 2 分钟）。")
            if st.button("清除该卡住的任务", key=f"bg_clear_{t['id']}"):
                with _bg_lock():
                    tk = _bg_tasks().pop(t["id"], None)
                    if tk is not None:
                        tk["status"] = "cleared"
                _btl.log_finish(_service().market.db.db_path, t["id"],
                                "cleared", error="页面手动清除（疑似失联）")
                st.rerun()
            continue
        if stage:
            # 收尾阶段：显示阶段文字 + 独立刻度，不再用「100% 运行中」误导
            st.progress(min(1.0, pct),
                        text=f"⏳ {t['label']} · {stage} {t['done']}/{t['total']}（自动刷新）")
        else:
            st.progress(min(1.0, pct),
                        text=f"⏳ {t['label']} 运行中 {t['done']}/{t['total']}（自动刷新）")

    # W77：终态出现时只刷新一次全页（打标记防 fragment 2s 循环 rerun）。
    with _bg_lock():
        fresh = [t["id"] for t in _bg_tasks().values()
                 if t["status"] in ("done", "error", "cleared")
                 and not t.get("_refreshed")]
        for tid in fresh:
            _bg_tasks()[tid]["_refreshed"] = True
    if fresh:
        st.rerun()

def _recent_jobs(limit: int = 50) -> list:
    """最近扫描任务行（降序）。

    基础 6 列 ``(id, trade_date, started_at, n_ok, n_fail, scan_type)`` +
    W79 增加 ``scope, duration_sec, wave3_enabled, universe_n``。
    W77：默认从 20 提到 50（配合 scan_jobs 历史保留策略）。
    W79：用 PRAGMA 动态列探测——老库未应用 006 迁移时缺失列以 NULL/
    默认值补齐，保证返回列数稳定、页面零回归。返回元组为：
    ``(id, trade_date, started_at, n_ok, n_fail, scan_type, scope,
       duration_sec, wave3_enabled, universe_n)``。
    """
    svc = _service()
    conn = svc.market.db._connect()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(scan_jobs)")}
        def sel(col, default):
            return col if col in cols else f"NULL AS {col}" if default is None \
                else f"'{default}' AS {col}"
        sql = (
            "SELECT id, trade_date, started_at, n_ok, n_fail, scan_type, "
            + sel("scope", "") + ", " + sel("duration_sec", None) + ", "
            + sel("wave3_enabled", None) + ", " + sel("universe_n", None)
            + " FROM scan_jobs ORDER BY id DESC LIMIT ?")
        rows = conn.execute(sql, (int(limit),)).fetchall()
        # 列缺失时 sel() 用默认值占位；统一成 10 元组，缺失值兜底
        out = []
        for r in rows:
            t = list(r)
            if t[6] is None:
                t[6] = ""           # scope
            if t[8] is None:
                t[8] = 0            # wave3_enabled
            out.append(tuple(t))
        return out
    finally:
        conn.close()


def _job_label(j) -> str:
    """W79：扫描 job 下拉标签——范围/股票池/耗时/三浪一眼可辨。

    兼容 6 列（W77 老库）与 10 列（W79）元组；缺失列按默认省略。
    """
    from mystery.services.scan import _SCOPE_CN
    scope = (j[6] if len(j) > 6 and j[6] else j[5]) or "market"
    scope_cn = _SCOPE_CN.get(scope, scope or "market")
    extra = f"[{scope_cn}]"
    if len(j) > 9 and j[9]:
        extra += f" {j[9]}只"
    if len(j) > 7 and j[7] is not None:
        extra += f" 耗时{round(float(j[7]), 1)}s"
    if len(j) > 8 and j[8]:
        extra += " 三浪开"
    return (f"job#{j[0]} {extra} {j[1] or ''} "
            f"成功{j[3]} 失败{j[4]} {j[2] or ''}")

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
    """后台任务：实时进度 + 完成/失败态 + 持久化台账（W77）+ 查看结果。

    W77：除内存实时态外，额外渲染生产库 ``bg_task_log`` 台账——服务重启、
    跨会话、任务被清除后历史任务仍可见；启动即留痕，失败也留痕。
    """
    from mystery.services import bg_task_log as _btl
    db_path = _service().market.db.db_path
    # 进程重启残留的 running 记录标记为「中断」，不再假装还在跑。
    _btl.mark_stale(db_path, _BG_STALE_SECONDS)
    logs = _btl.recent(db_path, limit=20)
    with _bg_lock():
        tasks = sorted(_bg_tasks().values(), key=lambda t: t["created"],
                       reverse=True)
    # 内存态：运行中进度（fragment 自动刷新）+ 完成/失败提示 + 查看结果。
    # 台账历史为空且内存也为空时才显示「暂无后台任务」。
    if tasks:
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
            # 函数内延迟导入：_render_scan_table 定义在 scan.py，而 scan 依赖本
            # 模块（from .components import *），顶层导入会形成循环导入。此处
            # 调用发生在页面渲染阶段，各 view 模块均已加载完毕，导入安全。
            from .scan import _render_scan_table
            _render_scan_table(t["results"], key=f"bg_{view_id}")

    # W77：持久化台账（本次问题核心——「最近任务」看不到后台扫描记录）。
    st.caption("📋 后台任务台账：每次启动即留痕，失败/中断也记录；持久化于生产库，"
               "服务重启后仍可查。带 job # 的任务可在下方「最近扫描任务与结果」查看结果。")
    if not logs:
        st.caption("暂无历史任务记录（点击上方「🚀 后台扫描」后出现）")
        return
    st.dataframe(
        [{
            '任务': r.get('label') or '-',
            '状态': _btl.status_label(r.get('status')),
            '进度': (f"{r.get('done') or 0}/{r.get('total') or 0}"
                     if r.get('status') == 'running' else
                     (f"{r.get('n_results') or 0} 只" if r.get('status') == 'done' else '-')),
            '扫描 job': (f"#{r['job_id']}" if r.get('job_id') else '-'),
            '开始': (r.get('created_at') or '')[5:19],
            '更新': (r.get('updated_at') or '')[5:19],
            '备注': ((r.get('error') or r.get('stage') or '')[:60]),
        } for r in logs],
        hide_index=True, use_container_width=True)
    st.caption(f"共显示最近 {len(logs)} 条（台账最多保留 200 条，超出自动裁剪）")

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
    '_job_label',
    'scan_job_meta',
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
