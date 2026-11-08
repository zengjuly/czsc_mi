from __future__ import annotations
import logging
import streamlit as st
from .components import *
# W64：拆分后跨 view 复用函数需显式导入（原单文件 app.py 同命名空间可直接调）。
from .scan import _render_excel_download, _render_scan_table
from .stock import _add_watchlist_widget
from mystery.services.analyze import (
    resonance_options_effective, resonance_options_default,
    resonance_override_exists, save_resonance_override, clear_resonance_override,
    RESONANCE_FIELDS,
)
from mystery.core.resonance import resolve_resonance_options
# W76：三浪扫描参数覆盖层（与共振面板同一套「覆盖层文件 + 一键还原」机制）。
from mystery.services import runtime_settings as rts


def _int_or(v, default: int) -> int:
    """面板数值回填：覆盖层/env 里出现非法值时安全取默认。"""
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def render_scan_wave3_config():
    """W76：三浪扫描参数网页配置（评估上限可调、一键还原默认值）。

    写入 ``runtime_settings.SCAN_WAVE3_OVERRIDE_PATH`` 覆盖层文件；Web /
    CLI / 日报 / 扫描收尾全部经 ``scan_wave3_options_effective()`` 读取，
    保存后**下次扫描即时热生效**（不改历史扫描结果、无需重启）。
    """
    st.subheader("⚙️ 三浪扫描参数（评估上限可调）")
    eff = rts.scan_wave3_options_display()
    defaults = rts.SCAN_WAVE3_DEFAULTS
    customized = rts.scan_wave3_override_exists()
    mode = rts.scan_wave3_mode()
    mode_label = {"auto": "自动限流（默认）", "fixed": "固定上限",
                  "full": "强制全量（不限）"}.get(mode, "自动限流（默认）")

    st.caption(
        "控制扫描收尾阶段三浪主升评估的规模。保存后**下次扫描即热生效**"
        "（无需重启；已落库的历史 job 结果不变，需重扫才会更新三浪列）。"
        "点击「一键还原默认值」恢复 W75 自动限流（>500 只时评前 200 只）。"
    )

    rows = [{"参数": "评估上限模式", "当前生效": mode_label,
             "生产默认": "自动限流（默认）",
             "状态": "✅ 已自定义" if customized else "默认"}]
    for key, label, _, _ in rts.SCAN_WAVE3_FIELDS:
        rows.append({
            "参数": label,
            "当前生效": eff.get(key, "-"),
            "生产默认": (defaults.get(key) if defaults.get(key) is not None
                         else "(未设置)"),
            "状态": "✅ 已自定义" if customized else "默认",
        })
    st.dataframe(rows, use_container_width=True, hide_index=True)

    with st.form("scan_wave3_config_form"):
        enabled = st.selectbox(
            "三浪策略开关", ["1", "0"],
            index=0 if eff.get("MYSTERY_SCAN_WAVE3", "1") != "0" else 1,
            format_func=lambda v: "开启" if v == "1" else "关闭（跳过三浪附加）",
            key="w76_enabled")
        trig = st.number_input(
            "自动限流触发阈值（池 > N 只才限流）", min_value=0, max_value=100000,
            value=_int_or(eff.get("MYSTERY_SCAN_WAVE3_AUTO_TRIGGER"), 500),
            step=100,
            help="扫描结果数超过该值才启用自动限流；自选股 168 只等小池不受影响",
            key="w76_trigger")
        auto_max = st.number_input(
            "三浪评估上限（仅评估评分前 N 只）", min_value=0, max_value=10000,
            value=_int_or(eff.get("MYSTERY_SCAN_WAVE3_AUTO_MAX"), 200),
            step=50,
            help="自动限流生效时只评估评分降序前 N 只，其余标「未评估」；0=不限流（全量）",
            key="w76_auto_max")
        strict = st.selectbox(
            "三浪铁律严格模式", ["0", "1"],
            index=0 if eff.get("MYSTERY_SCAN_WAVE3_STRICT", "0") != "1" else 1,
            format_func=lambda v: "关（默认）" if v == "0" else "开（第4/6条升硬项）",
            key="w76_strict")
        with st.expander("高级：评估上限模式", expanded=(mode != "auto")):
            mode_sel = st.radio(
                "模式", ["自动限流（默认）", "固定上限", "强制全量（不限）"],
                index={"auto": 0, "fixed": 1, "full": 2}.get(mode, 0),
                key="w76_mode",
                help="自动限流=超大池才限；固定上限=始终只评前 N 只；强制全量=跳过限流")
            fixed_n = st.number_input(
                "固定上限 N（模式=固定上限时生效）", min_value=1, max_value=10000,
                value=(_int_or(eff.get("MYSTERY_SCAN_WAVE3_MAX"), 300)
                       if mode == "fixed" else 300),
                step=50, key="w76_fixed_n")
            st.warning(
                "「强制全量」等价于 MYSTERY_SCAN_WAVE3_MAX=0，会跳过自动限流："
                "全市场 5500+ 只逐只评估约十几分钟、内存数 GB，可能重现 W75"
                "「后台扫描卡在最后」。仅建议小池或离线回测使用。"
            )
        if st.form_submit_button("保存配置（下次扫描热生效）", type="primary"):
            opts = {
                "MYSTERY_SCAN_WAVE3": enabled,
                "MYSTERY_SCAN_WAVE3_AUTO_TRIGGER": int(trig),
                "MYSTERY_SCAN_WAVE3_AUTO_MAX": int(auto_max),
                "MYSTERY_SCAN_WAVE3_STRICT": strict,
            }
            if mode_sel == "固定上限":
                opts["MYSTERY_SCAN_WAVE3_MAX"] = int(fixed_n)
            elif mode_sel.startswith("强制全量"):
                opts["MYSTERY_SCAN_WAVE3_MAX"] = 0
            # 自动限流：不写 MAX 键 → 稀疏语义保留 → W75 自动限流生效
            rts.save_scan_wave3_override(opts)
            st.success("已保存并热生效。下次扫描即用新口径；历史 job 结果不变。")
            st.rerun()

    if st.button("🔄 一键还原默认值", key="w76_restore",
                 help="清除网页自定义，恢复自动限流 trigger=500 / 上限=200"):
        rts.clear_scan_wave3_override()
        st.success("已还原到生产默认（自动限流 500/200，三浪开启，严格模式关）。")
        st.rerun()

def render_resonance_config():
    """W74：三振共振口径网页配置（修改即时热生效，一键还原默认值）。

    写入 ``mystery.services.analyze.RESONANCE_OVERRIDE_PATH`` 覆盖层文件，
    Web 与 CLI/scan 全链路经 ``resonance_options_effective()`` 读取，无需重启。
    """
    st.subheader("⚙️ 三振共振口径配置（生产默认可调）")
    eff_raw = resonance_options_effective()
    eff = resolve_resonance_options(eff_raw)
    default = resonance_options_default()
    customized = resonance_override_exists()

    st.caption(
        "下列参数决定「真三振」信号的判定口径（改变信号语义）。网页保存后"
        "**立即热生效**（下次分析与扫描即用新口径，历史缓存按口径自动失效重建）；"
        "点击「一键还原默认值」可恢复生产默认。"
    )

    rows = []
    for key, label, _, _ in RESONANCE_FIELDS:
        rows.append({
            "参数": label,
            "当前生效": eff.get(key),
            "生产默认": default.get(key),
            "状态": "✅ 已自定义" if customized else "默认",
        })
    st.dataframe(rows, use_container_width=True, hide_index=True)

    with st.form("resonance_config_form"):
        m = st.selectbox(
            "大盘腿口径", ["ma", "yearline"],
            index=["ma", "yearline"].index(eff.get("market_mode", "yearline")),
            help="ma=收盘>MA20/MA60；yearline=站年线且 MA5>MA20>MA60>MA250 多头（更严格）",
            key="w74_market_mode")
        thr = st.number_input(
            "真三振评分门闩 (70~100)", min_value=70.0, max_value=100.0,
            value=float(eff.get("true_threshold", 80.0)), step=1.0,
            help="共振评分 ≥ 此值才判真三振；只放宽/收紧该门闩，不绕过高位等硬门闩",
            key="w74_thr")
        cms = st.number_input(
            "资金活跃门槛(评分≥)", min_value=0.0, max_value=20.0,
            value=float(eff.get("capital_min_score", 8.0)), step=1.0, key="w74_cms")
        cvr = st.number_input(
            "资金活跃门槛(量比≥)", min_value=0.0, max_value=5.0,
            value=float(eff.get("capital_min_volume_ratio", 1.5)), step=0.1, key="w74_cvr")
        use_lag = st.checkbox(
            "启用指数滞后降级", value=eff.get("index_max_lag_days") is not None,
            help="关闭=只诊断不降级；开启后指数滞后个股超过 N 天则大盘腿降级为 unknown（绝不误放）",
            key="w74_use_lag")
        lag = st.number_input(
            "指数滞后降级天数 N", min_value=0, max_value=30,
            value=int(eff.get("index_max_lag_days") or 3), step=1,
            disabled=not use_lag, key="w74_lag")
        if st.form_submit_button("保存配置（立即热生效）", type="primary"):
            opts = {
                "market_mode": m,
                "true_threshold": thr,
                "capital_min_score": cms,
                "capital_min_volume_ratio": cvr,
                "index_max_lag_days": (lag if use_lag else None),
            }
            save_resonance_override(opts)
            st.success("已保存并热生效。下次分析/扫描即用新口径；历史缓存按口径自动失效重建。")
            st.rerun()

    if st.button("🔄 一键还原默认值", help="清除网页自定义，恢复生产默认 yearline / 80"):
        clear_resonance_override()
        st.success("已还原到生产默认（yearline / 80）。")
        st.rerun()


def view_system():
    st.header("系统状态（只读）")
    render_resonance_config()
    render_scan_wave3_config()
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
    # W77：任务完成后 fragment 自动刷新全页；此按钮供手动兜底。
    if st.button("🔄 刷新记录", key="sys_job_refresh"):
        st.rerun()
    jobs = _recent_jobs(limit=50)
    if jobs:
        st.caption(f"共 {len(jobs)} 条历史扫描 job（每种类型保留最近若干份）；"
                   f"后台任务的启动/失败/中断记录见下方「后台任务台账」。")
        opts = {f"job#{j[0]} [{j[5] or 'market'}] {j[1] or ''} 成功{j[3]} 失败{j[4]} {j[2] or ''}": j[0]
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
        st.dataframe([{'名称': r.get('name') or '未知', '代码': r['symbol'],
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
    'render_resonance_config',
    'render_scan_wave3_config',
]
