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
from .views.stock import *
from .views.scan import *
from .views.sector import *
from .views.system import *
from .views.components import *

def main():
    # W42 v0.10.15: 访问鉴权门（~/.config/czsc_mi/web_auth.json 存在即启用；
    # 未配置 = 关闭，CI/integration 冒烟零影响）。失败即停，不渲染业务内容。
    from mystery.apps.web.auth import require_login
    if not require_login():
        st.stop()

    page = st.sidebar.radio("导航", ["个股分析", "自选股", "全市场扫描", "板块钻取",
                                    "真三振池", "系统状态", "板块强度表"],
                            key="nav_page",
                            on_change=_clear_subview)
    
    # 检查是否有跳转请求（从扫描结果点击股票）
    jump_target = st.session_state.pop('_jump_to_page', None)
    if jump_target:
        page = jump_target
        # 同步更新 sidebar radio 的显示
        st.session_state['nav_page'] = jump_target
    
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
