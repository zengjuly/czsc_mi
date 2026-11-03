from __future__ import annotations
import logging
import streamlit as st
from .components import *

def view_system():
    st.header("系统状态（只读）")
    cfg = load_config()
    svc = _service()
    db = svc.market.db
    # W49：写库任务互斥锁 + 进度心跳（cron/CLI/手动同通道）。
    # 心跳文件由 task_lock 写 <db>.tasks.json；running_tasks 只列
    # 锁仍真实持有的条目（崩溃残留自动忽略）。
    _render_running_tasks(db.db_path)
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
        "rule_ver": "mystery-1.22.30-compat-patgate",
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
        sig = st.radio("信号过滤", ["全部", "真三振", "VAP-ATR突破", "筹码低位", "回踩确认突破", "假突破"],
                       horizontal=True, key="sys_job_sig")
        sig_map = {"全部": None, "真三振": "true_resonance", "回踩确认突破": "breakout_confirm",
               "假突破": "false_breakout",
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

__all__ = [
    'view_system',
    'view_true_resonance',
]
