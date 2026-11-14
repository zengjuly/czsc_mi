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
# W82：典型 K 线组合形态网页配置（勾选形态 / 窗口 / 日周开关）。
# W83：PATTERN_GROUPS 供「多方炮重点变体」快捷勾选与分组标注。
from mystery.core.candlestick_patterns import (
    PATTERN_SPECS, PATTERN_CODES, PATTERN_GROUPS)


def _int_or(v, default: int) -> int:
    """面板数值回填：覆盖层/env 里出现非法值时安全取默认。"""
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def render_candlestick_config():
    """W82：典型 K 线组合形态网页配置（勾选/窗口/日周开关 + 一键还原）。

    写入 ``runtime_settings.CANDLE_OVERRIDE_PATH`` 覆盖层；Web / CLI /
    日报 / 扫描全部经 ``candle_options_normalized()`` 读取，保存后**下次
    分析即热生效**（配置入分析缓存键，旧缓存自动失效重建；历史 job 不变）。
    """
    st.subheader("🕯️ 典型 K 线组合形态（可勾选 / 一键还原）")
    eff = rts.candle_options_display()
    norm = rts.candle_options_normalized()
    customized = rts.candle_override_exists()
    _def = rts.CANDLE_DEFAULTS

    st.caption(
        "日 K / 周 K **独立分析**经典组合（多方炮、仙人指路、早晨之星…）。"
        "结论仅展示、不进综合分；报告见 Excel/HTML 日报与个股页。"
        "保存后下次分析/扫描即热生效（无需重启，历史结果不变）。"
    )
    rows = [
        {"参数": "总开关", "当前生效": "开启" if norm["enabled"] else "关闭",
         "生产默认": "开启", "状态": "✅ 已自定义" if customized else "默认"},
        {"参数": "日 K 分析", "当前生效": "开启" if norm["daily"] else "关闭",
         "生产默认": "开启", "状态": "✅ 已自定义" if customized else "默认"},
        {"参数": "周 K 分析", "当前生效": "开启" if norm["weekly"] else "关闭",
         "生产默认": "开启", "状态": "✅ 已自定义" if customized else "默认"},
        {"参数": "回溯窗口(根)", "当前生效": str(norm["lookback"]),
         "生产默认": str(_def["MYSTERY_CANDLE_LOOKBACK"]),
         "状态": "✅ 已自定义" if customized else "默认"},
        {"参数": "启用形态",
         "当前生效": f"{len(norm['patterns'])} 个："
                     + "、".join(PATTERN_SPECS[c]["name"]
                                 for c in norm["patterns"] if c in PATTERN_SPECS),
         "生产默认": f"全部 {len(PATTERN_CODES)} 个",
         "状态": "✅ 已自定义" if customized else "默认"},
    ]
    st.dataframe(rows, use_container_width=True, hide_index=True)

    with st.form("candlestick_config_form"):
        st.caption(
            "W83 多方炮重点变体（勾选后结论见报告「形态重点观察」列）："
            "跳空多方炮—观察突破缺口是否得到支撑；"
            "两阳夹两阴—观察调整期间是否缩量；"
            "叠叠多方炮—观察每次回调低点是否抬高；"
            "多方炮蓄势(待确认)—观察下周一能否放量阳线确认。"
        )
        on = st.selectbox(
            "形态分析总开关", ["1", "0"],
            index=0 if eff.get("MYSTERY_CANDLE_ENABLED", "1") != "0" else 1,
            format_func=lambda v: "开启" if v == "1" else "关闭（跳过形态分析）",
            key="w82_enabled")
        c1, c2 = st.columns(2)
        with c1:
            daily = st.checkbox(
                "分析日 K", value=eff.get("MYSTERY_CANDLE_DAILY", "1") != "0",
                key="w82_daily")
        with c2:
            weekly = st.checkbox(
                "分析周 K", value=eff.get("MYSTERY_CANDLE_WEEKLY", "1") != "0",
                key="w82_weekly")
        lookback = st.number_input(
            "回溯窗口（最近 N 根 K 线内找形态）", min_value=5, max_value=500,
            value=_int_or(eff.get("MYSTERY_CANDLE_LOOKBACK"), 60), step=5,
            help="按根数计，日 K 与周 K 用同一窗口；越大越能发现历史形态，也略增计算量",
            key="w82_lookback")
        picked = st.multiselect(
            "启用哪些形态（可多选，全选=全部）", list(PATTERN_CODES),
            default=list(norm["patterns"]),
            format_func=lambda c: (
                f"[{PATTERN_SPECS[c].get('group', '基础形态')}] "
                f"{PATTERN_SPECS[c]['name']}"
                f"（{PATTERN_SPECS[c]['direction']}）"
                f" — {PATTERN_SPECS[c]['desc']}"),
            key="w82_patterns",
            help="只勾选的形态会参与日K/周K检测；形态定义见 core/candlestick_patterns.py")
        focus_only = st.checkbox(
            "仅启用多方炮 3 种重点变体 + 蓄势待确认（提交时覆盖上方多选）",
            value=False, key="w83_focus_only",
            help="快捷口径：只跑用户点名的四种多方炮形态，便于专注验证缺口支撑/缩量/低点抬高")
        if st.form_submit_button("保存配置（下次分析热生效）", type="primary"):
            if focus_only:
                picked = list(PATTERN_GROUPS["多方炮变体"])
            rts.save_candle_override({
                "MYSTERY_CANDLE_ENABLED": on,
                "MYSTERY_CANDLE_DAILY": "1" if daily else "0",
                "MYSTERY_CANDLE_WEEKLY": "1" if weekly else "0",
                "MYSTERY_CANDLE_LOOKBACK": int(lookback),
                "MYSTERY_CANDLE_PATTERNS": (
                    "all" if len(picked) == len(PATTERN_CODES) else picked),
            })
            st.success("已保存并热生效。下次分析/扫描即用新口径；历史结果不变。")
            st.rerun()

    if st.button("🔄 一键还原默认值", key="w82_restore",
                 help="清除网页自定义：全部形态 / 日+周 K / 回溯 60 根"):
        rts.clear_candle_override()
        st.success(
            f"已还原到生产默认（全部 {len(PATTERN_CODES)} 个形态、"
            "日K+周K、回溯 60 根）。")
        st.rerun()


def render_sector_config():
    """W85：行业板块关注跟踪网页配置（多选关注板块 / 相关个股数 / 一键还原）。

    写入 ``runtime_settings.SECTOR_OVERRIDE_PATH`` 覆盖层；Web / CLI /
    日报 / 扫描报告全部经 ``sector_options_normalized()`` 读取，保存后下次
    日报/扫描即热生效。板块清单来自真实 ``sector_kline``（688 个有指数数据）。
    """
    st.subheader("🏭 行业板块关注跟踪（可多选 / 一键还原）")
    eff = rts.sector_options_display()
    norm = rts.sector_options_normalized()
    customized = rts.sector_override_exists()
    _def = rts.SECTOR_DEFAULTS

    st.caption(
        "选择要关注跟踪的行业板块，日报 Excel/HTML 会新增「行业板块」区块："
        "板块年线/强度分/日周K形态/主力吸筹研判，以及**与板块强相关的个股**"
        "（板块指数与成分股近 N 日收益率相关系数 Top N，附带本期分析结论）。"
        "默认 ``auto`` = 自动跟随当前自选股主行业。仅展示、不进综合分。"
    )
    # 板块清单（只读库，缓存避免每次 rerun 重扫）
    try:
        from mystery.services.sector_analysis import list_sectors
        db = _service().market.db
        sectors = list_sectors(db)
    except Exception:
        sectors = []
    code_to_name = {d["code"]: d["name"] for d in sectors}
    name_to_code = {f"{d['name']}（{d['code']}）": d["code"] for d in sectors}

    focus = norm["focus"]
    focus_mode = focus if isinstance(focus, str) else "custom"
    rows = [
        {"参数": "板块分析总开关",
         "当前生效": "开启" if norm["enabled"] else "关闭",
         "生产默认": "开启", "状态": "✅ 已自定义" if customized else "默认"},
        {"参数": "关注板块",
         "当前生效": ("auto（跟随自选股主行业）" if focus == "auto" else
                    "all（全部）" if focus == "all" else
                    f"{len(focus)} 个：" + "、".join(
                        code_to_name.get(c, c) for c in focus[:6])),
         "生产默认": "auto（跟随自选股主行业）",
         "状态": "✅ 已自定义" if customized else "默认"},
        {"参数": "强相关个股数", "当前生效": str(norm["top_n"]),
         "生产默认": str(_def["MYSTERY_SECTOR_TOP_N"]),
         "状态": "✅ 已自定义" if customized else "默认"},
        {"参数": "相关性窗口(交易日)", "当前生效": str(norm["corr_window"]),
         "生产默认": str(_def["MYSTERY_SECTOR_CORR_WINDOW"]),
         "状态": "✅ 已自定义" if customized else "默认"},
        {"参数": "auto 模式最多板块数", "当前生效": str(norm["auto_max"]),
         "生产默认": str(_def["MYSTERY_SECTOR_AUTO_MAX"]),
         "状态": "✅ 已自定义" if customized else "默认"},
    ]
    st.dataframe(rows, use_container_width=True, hide_index=True)
    st.caption(f"库内有真实指数 K 线的可选板块：{len(sectors)} 个（行业 881 段排前）。")

    with st.form("sector_config_form"):
        on = st.selectbox(
            "板块分析总开关", ["1", "0"],
            index=0 if eff.get("MYSTERY_SECTOR_ENABLED", "1") != "0" else 1,
            format_func=lambda v: "开启" if v == "1" else "关闭（日报不出板块区块）",
            key="w85_enabled")
        mode = st.radio(
            "关注板块来源", ["auto（跟随自选股主行业）", "手动勾选", "all（全部板块）"],
            index=(0 if focus_mode == "auto" else
                   2 if focus_mode == "all" else 1),
            horizontal=True, key="w85_mode")
        picked: list = []
        if mode.startswith("手动"):
            default_codes = [c for c in (focus if isinstance(focus, list) else [])
                             if c in name_to_code.values()]
            default_labels = [lbl for lbl, c in name_to_code.items()
                              if c in default_codes]
            sel = st.multiselect(
                "选择关注板块（可多选，支持输入名称搜索）",
                list(name_to_code.keys()), default=default_labels,
                key="w85_picked",
                help="板块清单来自 sector_kline 真实指数数据；关注板块会进入每日报告的行业板块区块")
            picked = [name_to_code[l] for l in sel]
        c1, c2, c3 = st.columns(3)
        with c1:
            top_n = st.number_input(
                "强相关个股数", min_value=1, max_value=30,
                value=_int_or(eff.get("MYSTERY_SECTOR_TOP_N"), 8), step=1,
                key="w85_topn")
        with c2:
            corr_win = st.number_input(
                "相关性窗口(交易日)", min_value=20, max_value=250,
                value=_int_or(eff.get("MYSTERY_SECTOR_CORR_WINDOW"), 60), step=10,
                key="w85_corrwin")
        with c3:
            auto_max = st.number_input(
                "auto 最多板块数", min_value=1, max_value=30,
                value=_int_or(eff.get("MYSTERY_SECTOR_AUTO_MAX"), 8), step=1,
                key="w85_automax")
        if st.form_submit_button("保存配置（下次日报/扫描热生效）", type="primary"):
            focus_val = ("auto" if mode.startswith("auto") else
                         "all" if mode.startswith("all") else
                         (picked or "auto"))
            rts.save_sector_override({
                "MYSTERY_SECTOR_ENABLED": on,
                "MYSTERY_SECTOR_FOCUS": focus_val,
                "MYSTERY_SECTOR_TOP_N": int(top_n),
                "MYSTERY_SECTOR_CORR_WINDOW": int(corr_win),
                "MYSTERY_SECTOR_AUTO_MAX": int(auto_max),
            })
            st.success("已保存。下次日报/扫描即用新板块口径；历史结果不变。")
            st.rerun()

    if st.button("🔄 一键还原默认值", key="w85_restore",
                 help="清除网页自定义：恢复 auto（跟随自选股主行业）/ 8 只相关个股 / 60 日窗口"):
        rts.clear_sector_override()
        st.success("已还原到生产默认（auto 跟随自选股主行业、8 个板块、8 只相关个股、60 日窗口）。")
        st.rerun()


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
        # W95/①：大盘非高位风控三态（默认 hard，生产信号零变化）
        _hpg = eff.get("high_position_gate", "hard")
        hpg = st.selectbox(
            "大盘非高位风控", ["hard", "soft", "off"],
            index=["hard", "soft", "off"].index(
                _hpg if _hpg in ("hard", "soft", "off") else "hard"),
            help="hard=一票否决+高位-15惩罚（生产默认，零回归）；"
                 "soft=保留-15惩罚但不作否决门闩；off=去掉惩罚且不作否决门闩",
            key="w95_high_position_gate")
        if st.form_submit_button("保存配置（立即热生效）", type="primary"):
            opts = {
                "market_mode": m,
                "true_threshold": thr,
                "capital_min_score": cms,
                "capital_min_volume_ratio": cvr,
                "index_max_lag_days": (lag if use_lag else None),
                "high_position_gate": hpg,
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
    render_candlestick_config()
    render_sector_config()
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
        opts = {_job_label(j): j[0] for j in jobs}
        pick = st.selectbox("选择扫描任务查看结果", list(opts.keys()),
                            key="sys_job_pick")
        job_id = opts[pick]
        # W79：范围/耗时/三浪口径一行摘要（系统状态页）。
        _jm = scan_job_meta(job_id)
        if _jm.get("label"):
            st.caption(f"📐 job #{job_id}：{_jm['label']}")
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
    # W79：真三振池顶部标明本次扫描范围/耗时/三浪口径与时间来源，
    # 避免「自选股扫描」被误当作「全市场扫描」。
    _jm = scan_job_meta(job)
    if _jm.get("label"):
        st.caption(f"📐 扫描范围：{_jm['label']}")
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
    'render_candlestick_config',
    'render_sector_config',
]
