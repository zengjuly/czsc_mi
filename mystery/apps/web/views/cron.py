"""mystery.apps.web.views.cron — 定时任务网页配置（W108）。

读取/启停/增删当前用户的 crontab（经 :mod:`mystery.services.cron_manager`），
czsc_mi 相关任务与其它任务分组展示，改动即时生效（crontab 原子重写）。
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from mystery.services import cron_manager as _cm

__all__ = ["view_cron_page"]

_KIND_LABEL = {"czsc_mi": "czsc_mi", "other": "其它", "env": "env"}


def _render_job_table(jobs: list, title: str) -> str | None:
    """渲染一组任务表格（首列名称定宽），返回选中任务的 job_id。"""
    if not jobs:
        return None
    st.subheader(title)
    rows = [{
        "任务": j.label,
        "计划": j.schedule or "-",
        "状态": "🟢 启用" if j.enabled else "🔴 已停用",
        "命令": j.command,
        "job_id": j.job_id,
    } for j in jobs]
    event = st.dataframe(
        pd.DataFrame(rows, columns=["任务", "计划", "状态", "命令", "job_id"]),
        hide_index=True, use_container_width=True,
        on_select="rerun", selection_mode="single-row",
        column_config={
            "任务": st.column_config.TextColumn("任务", width="medium"),
            "计划": st.column_config.TextColumn("计划", width="small"),
            "状态": st.column_config.TextColumn("状态", width="small"),
            "命令": st.column_config.TextColumn("命令", width="large"),
            "job_id": None,  # 隐藏技术列
        },
        key=f"cron_tbl_{title}")
    try:
        sel = list(event.selection.rows or [])
    except AttributeError:
        sel = []
    return rows[sel[0]]["job_id"] if sel else None


def view_cron_page():
    st.header("定时任务配置（当前用户 crontab）")
    st.caption(
        "改动直接写用户 crontab（启停 = 注释/取消注释，可随时恢复）；"
        "重启不丢失。修改后下次触发即按新配置执行。")

    try:
        jobs = _cm.list_jobs()
    except RuntimeError as e:
        st.error(f"crontab 读取失败：{e}")
        return

    cz = [j for j in jobs if j.kind == "czsc_mi"]
    others = [j for j in jobs if j.kind == "other"]
    envs = [j for j in jobs if j.kind == "env"]

    sel_cz = _render_job_table(cz, "czsc_mi 相关")
    sel_other = _render_job_table(others, "其它任务")
    if envs:
        with st.expander(f"环境变量行（{len(envs)} 条，只读）"):
            for e in envs:
                st.code(e.command, language=None)

    sel = sel_cz or sel_other
    if not sel:
        st.caption("点击上方表格行选中任务后，可启停 / 删除。")
    else:
        job = next(j for j in jobs if j.job_id == sel)
        st.caption(f"已选中：**{job.label}**（`{job.schedule}` {'启用中' if job.enabled else '已停用'}）")
        c1, c2, c3, _ = st.columns([1, 1, 1, 5])
        if c1.button("🔴 停用", disabled=not job.enabled, key="cron_disable"):
            _cm.set_enabled(sel, False)
            st.rerun()
        if c2.button("🟢 启用", disabled=job.enabled, key="cron_enable"):
            _cm.set_enabled(sel, True)
            st.rerun()
        if c3.button("🗑 删除", key="cron_delete"):
            _cm.remove_job(sel)
            st.rerun()

    st.divider()
    st.subheader("➕ 新增任务")
    with st.form("cron_add_form", clear_on_submit=True):
        sch = st.text_input("计划表达式（5 段，如 `1,31 9-15 * * 1-5`，或 `@daily`）")
        cmd = st.text_area("命令（完整路径 + 参数）", height=68)
        note = st.text_input("备注（可选，写入 crontab 注释行）")
        submitted = st.form_submit_button("添加", type="primary")
        if submitted:
            try:
                j = _cm.add_job(sch, cmd, note)
                st.success(f"已添加：{j.label}（{j.schedule}）")
            except ValueError as e:
                st.error(f"添加失败：{e}")
            except RuntimeError as e:
                st.error(str(e))
