"""冒烟测试：czsc_mi Web 后台扫描 + 名称搜索改动后各页面渲染无异常。

仅验证渲染路径（不触发真实分析/后台线程落库）：
- 个股分析：selectbox 名称搜索 + 渲染
- 自选股：添加自选 selectbox + 列表
- 全市场扫描：后台扫描按钮 + 最近任务选择器
- 板块钻取：后台扫描全部成分股按钮
- 系统状态：最近扫描任务与结果选择器

依赖真实生产库（MYSTERY_DB_PATH），只读；AppTest 从文件渲染。
"""
from __future__ import annotations

import os

from streamlit.testing.v1 import AppTest

APP = "/home/ai/ai_runner/stock/czsc_mi/mystery/apps/web/app.py"


def _make(mtimeo=120):
    if not os.environ.get("MYSTERY_DB_PATH"):
        os.environ["MYSTERY_DB_PATH"] = (
            "/home/ai/ai_runner/stock/data/db/mystery_cache.db")
    return AppTest.from_file(APP, default_timeout=mtimeo)


def test_stock_page_renders():
    at = _make()
    at.run()
    assert len(at.exception) == 0, [str(e) for e in at.exception]
    # 个股分析 selectbox（名称搜索）存在
    assert any("选择股票" in s.label for s in at.selectbox)


def test_watchlist_page_renders():
    at = _make()
    at.session_state["subview"] = "watchlist"
    at.run()
    assert len(at.exception) == 0, [str(e) for e in at.exception]
    # 添加自选 selectbox（名称搜索）存在
    assert any("添加自选" in s.label for s in at.selectbox)


def test_scan_page_renders():
    at = _make()
    at.run()
    assert len(at.exception) == 0, [str(e) for e in at.exception]
    # 导航到「全市场扫描」
    at.sidebar.radio[0].set_value("全市场扫描")
    at.run()
    assert len(at.exception) == 0, [str(e) for e in at.exception]
    # 后台扫描按钮存在
    labels = [b.label for b in at.button]
    assert any("后台扫描全部股票" in l for l in labels)
    assert any("后台扫描全部自选股" in l for l in labels)


def test_sector_page_renders():
    at = _make()
    at.run()
    at.sidebar.radio[0].set_value("板块钻取")
    at.run()
    assert len(at.exception) == 0, [str(e) for e in at.exception]
    labels = [b.label for b in at.button]
    assert any("后台扫描全部成分股" in l for l in labels)


def test_system_page_renders():
    at = _make()
    at.run()
    at.sidebar.radio[0].set_value("系统状态")
    at.run()
    assert len(at.exception) == 0, [str(e) for e in at.exception]
    # 系统状态含「最近扫描任务与结果」入口
    assert any("最近扫描任务" in s.value for s in at.subheader)


def test_bg_store_persists_across_rerun():
    """后台任务仓库跨 rerun 持久（st.cache_resource，非模块级 dict）。"""
    from mystery.apps.web.app import _bg_store, _bg_lock, _bg_tasks, _bg_launch
    # 第一次"rerun"：启动一个 fake 任务并完成
    s1, l1, t1, launch = _bg_store, _bg_lock, _bg_tasks, _bg_launch
    tid = launch("测试任务", lambda cb, holder: (cb(1, 2),
                                                 holder.append(99),
                                                 [{"symbol": "x"}])[2])
    for _ in range(50):
        if t1().get(tid, {}).get("status") == "done":
            break
        import time
        time.sleep(0.05)
    # 第二次"rerun"：模块级 dict 若被重置会丢任务，这里必须仍是同一对象
    assert s1() is _bg_store()
    assert l1() is _bg_lock()
    assert t1() is _bg_tasks()
    assert tid in t1(), "任务跨 rerun 丢失"
    assert t1()[tid]["status"] == "done", t1()[tid]
