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
