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


def test_scan_detail_link_restores_nav():
    """从扫描结果「详情」链接进入个股页：恢复上一只/下一只/返回列表导航。

    链接格式 ?stock=<sym>&nav_key=<key>&nav_idx=<i>；session 中对应
    <key>_nav_rows 为渲染扫描表时存的列表。无 nav_key 时不显示导航。
    """
    at = _make()
    rows = [
        {"symbol": "600519.SH", "name": "贵州茅台"},
        {"symbol": "000001.SZ", "name": "平安银行"},
        {"symbol": "000100.SZ", "name": "TCL科技"},
    ]
    at.session_state["scan_nav_rows"] = rows
    # 模拟点击「详情」链接（第 2 只，index 1）
    at.query_params["stock"] = "000001.SZ"
    at.query_params["nav_key"] = "scan"
    at.query_params["nav_idx"] = "1"
    at.run()
    assert len(at.exception) == 0, [str(e) for e in at.exception]
    labels = [b.label for b in at.button]
    assert "← 上一只" in labels
    assert "下一只 →" in labels
    assert "返回列表" in labels
    assert any("扫描结果 2 / 3" in c.value for c in at.caption)
    # 第 1 只：上一只应禁用
    at.session_state["stock_nav_idx"] = 0
    at.run()
    assert len(at.exception) == 0, [str(e) for e in at.exception]
    prev = [b for b in at.button if b.label == "← 上一只"]
    assert prev and prev[0].disabled


def test_scan_detail_link_without_nav():
    """无 nav_key 的直达链接（如收藏/外部链接）：不显示导航。"""
    at = _make()
    at.query_params["stock"] = "600519.SH"
    at.run()
    assert len(at.exception) == 0, [str(e) for e in at.exception]
    labels = [b.label for b in at.button]
    assert "← 上一只" not in labels
    assert "返回列表" not in labels


def test_scan_table_shows_signal_flags():
    """扫描结果表格展示判定列（年线滤网/周线锚定/破五反五/主升浪8项），值来自 mystery。"""
    from mystery.apps.web.app import _sig_flag, _main_wave_flag, _scan_table_data
    # _sig_flag 取值：True→✅ / False→❌ / 缺失→''
    assert _sig_flag({"mystery": {"signal": {"年线滤网": True}}}, "年线滤网") == "✅"
    assert _sig_flag({"mystery": {"signal": {"年线滤网": False}}}, "年线滤网") == "❌"
    assert _sig_flag({"mystery": {"signal": {}}}, "年线滤网") == ""
    assert _sig_flag({}, "年线滤网") == ""

    # _main_wave_flag：checklist8.满足数量 → N/8；缺失 → ''
    assert _main_wave_flag({"mystery": {"checklist8": {"满足数量": 4}}}) == "4/8"
    assert _main_wave_flag({"mystery": {"checklist8": {}}}) == ""
    assert _main_wave_flag({}) == ""

    # 表格数据列与判定值
    rows = [
        {"symbol": "600150.SH", "name": "中国船舶", "score": 0.0,
         "advice": "观望（未通过年线滤网）", "chip_low": False, "chip_quiet": False,
         "price_pos": 0.05, "trade_date": "2026-09-07",
         "mystery": {"signal": {"年线滤网": False, "周线锚定": True,
                                "破五反五": False},
                     "checklist8": {"满足数量": 4}}},
        {"symbol": "600519.SH", "name": "贵州茅台", "score": 60.0,
         "advice": "关注", "chip_low": True, "chip_quiet": False,
         "price_pos": None, "trade_date": "2026-09-07",
         "mystery": {"signal": {"年线滤网": True, "周线锚定": True,
                                "破五反五": True},
                     "checklist8": {"满足数量": 8}}},
    ]
    data = _scan_table_data(rows, key="scan")
    cols = list(data[0].keys())
    for c in ['年线滤网', '周线锚定', '破五反五', '主升浪8项', '筹码低位',
              '高位缩量', '回撤%', '详情']:
        assert c in cols, f"缺少列 {c}: {cols}"
    # 判定值正确映射
    assert data[0]['年线滤网'] == '❌' and data[0]['周线锚定'] == '✅'
    assert data[0]['主升浪8项'] == '4/8'
    assert data[1]['年线滤网'] == '✅' and data[1]['破五反五'] == '✅'
    assert data[1]['主升浪8项'] == '8/8'
    # 详情链接带 nav 参数
    assert data[0]['详情'] == "?stock=600150.SH&nav_key=scan&nav_idx=0"


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
