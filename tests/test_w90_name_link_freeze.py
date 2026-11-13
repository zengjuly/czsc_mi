"""W90：报告表格「详情链接挂到股票名称」+「冻结名称列」专项测试。

覆盖两类载体：
- 网页扫描结果表（scan.py）：渲染层把「详情」URL 挂到「名称」列、移除独立
  「详情」列、名称列 pinned=True 原生冻结；纯函数 _scan_table_data 字段不变。
- Excel 汇总报告（excel_report.py）：名称单元格加个股详情超链接、freeze_panes
  由 "A2" 改为 "B2"（冻结首行 + 名称列 A）；代码列仍无链接（W14 契约不变）。

Web 部分构造 ColumnConfig 需 streamlit，仅本机（装 streamlit）运行；CI 无
streamlit 时按 importorskip 跳过，不挡全量收集。
"""
from __future__ import annotations

import io
import os

import pytest

pytest.importorskip("streamlit")

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
if ROOT not in __import__("sys").path:
    __import__("sys").path.insert(0, ROOT)


# ---------------------------------------------------------------------------
# 网页端
# ---------------------------------------------------------------------------
def _web_mod():
    import mystery.apps.web.views.scan as m
    return m


def test_name_link_rows_moves_detail_into_name():
    """W90：详情 URL 挂到名称（附 &_n=股票名），独立「详情」列被移除。"""
    m = _web_mod()
    table = [
        {"序号": 1, "名称": "中国船舶", "综合评分": 0.0,
         "详情": "?stock=600150.SH&nav_key=k&nav_idx=0&at=tok"},
        {"序号": 2, "名称": "贵州茅台", "综合评分": 60.0,
         "详情": "?stock=600519.SH&nav_key=k&nav_idx=1&at=tok"},
    ]
    out = m._name_link_rows(table)
    assert len(out) == 2
    # 原 table 不被修改
    assert "详情" in table[0]
    for d in out:
        assert "详情" not in d
        assert d["名称"].startswith("?stock=")
        assert "&_n=" in d["名称"]
    # 名称里中文原样保留（仅结构性字符编码），供 display_text 正则回显
    assert "中国船舶" in out[0]["名称"] and "贵州茅台" in out[1]["名称"]
    assert out[0]["名称"].startswith("?stock=600150.SH&nav_key=k&nav_idx=0&at=tok&_n=中国船舶")
    assert out[1]["名称"].startswith("?stock=600519.SH&nav_key=k&nav_idx=1&at=tok&_n=贵州茅台")


def test_name_link_rows_structural_chars_encoded():
    """W90：名称里的 & # ? = % 空白 被编码，不破坏 query 解析与正则。"""
    m = _web_mod()
    table = [{"名称": "A&B 股#1", "详情": "?stock=000001.SZ&nav_key=k&nav_idx=0"}]
    out = m._name_link_rows(table)
    name = out[0]["名称"]
    # 中英文原样，结构性字符被编码
    assert "A" in name and "B" in name and "股" in name and "1" in name
    assert "&_n=A%B" not in name  # 不应出现未编码的 &
    assert " " not in name.split("&_n=")[1]  # _n 段内无裸空白


def test_name_link_rows_preserves_unknown():
    """W90：详情缺失时名称保持原样，不抛异常。"""
    m = _web_mod()
    table = [{"名称": "无名", "综合评分": 5.0}]
    out = m._name_link_rows(table)
    assert out[0]["名称"] == "无名"


def test_scan_table_column_config_freezes_name_and_links():
    """W90：列配置——名称=LinkColumn(pinned)+display_text 正则；序号 pinned；
    不再配置独立「详情」列。"""
    m = _web_mod()
    cfg = m._scan_table_column_config()
    assert "详情" not in cfg
    # 序号、名称均 pinned（glide 冻结左侧连续前缀列）
    seq = dict(cfg["序号"])
    assert seq.get("pinned") is True
    name_cfg = dict(cfg["名称"])
    assert name_cfg.get("pinned") is True
    # LinkColumn 特征：type_config.type=='link' 且含 display_text 回显正则
    tc = name_cfg.get("type_config") or {}
    assert tc.get("type") == "link", "名称列应为 LinkColumn"
    assert m._NAME_DISPLAY_RE in (tc.get("display_text") or "")


def test_scan_table_data_still_has_detail_field():
    """W90：纯函数 _scan_table_data 字段不变（既有展示契约零改动）。"""
    import mystery.apps.web.views.scan as sc
    rows = [{"symbol": "600150.SH", "name": "中国船舶", "score": 0.0,
             "advice": "观望", "mystery": {"signal": {"年线滤网": False},
                                          "checklist8": {"满足数量": 4}},
             "chip_low": False, "chip_quiet": False,
             "price_pos": 0.05, "trade_date": "2026-09-07"}]
    data = sc._scan_table_data(rows, nav_key="k")
    assert "详情" in data[0]
    assert data[0]["详情"].startswith("?stock=600150.SH&nav_key=k&nav_idx=0")


# ---------------------------------------------------------------------------
# Excel 端
# ---------------------------------------------------------------------------
def _excel_mod():
    import mystery.apps.reports.excel_report as m
    return m


def test_stock_detail_url_builds_and_configurable():
    """W90：个股详情页 URL 构造，base 可配（默认 127.0.0.1:18601）。"""
    m = _excel_mod()
    # 默认 base
    assert m.stock_detail_url("600150.SH") == "http://127.0.0.1:18601/?stock=600150.SH"
    # 自定义 base（带尾斜杠/带 ? 两种）
    assert m.stock_detail_url("600150.SH", base_url="https://x.example.com/") == \
        "https://x.example.com/?stock=600150.SH"
    assert m.stock_detail_url("600150.SH", base_url="https://x.example.com/dash") == \
        "https://x.example.com/dash?stock=600150.SH"
    assert m.stock_detail_url("600150.SH", base_url="https://x.example.com/dash?a=1") == \
        "https://x.example.com/dash?a=1&stock=600150.SH"
    # 环境变量覆盖
    os.environ["MYSTERY_WEB_BASE_URL"] = "https://pub.example.com/"
    try:
        assert m.stock_detail_url("600519.SH") == "https://pub.example.com/?stock=600519.SH"
    finally:
        os.environ.pop("MYSTERY_WEB_BASE_URL", None)


def test_excel_summary_freezes_name_and_links_name_only():
    """W90：汇总报告冻结首行+名称列(B2)，名称(A)超链接，代码(B)无链接。"""
    import openpyxl
    m = _excel_mod()
    results = [
        {"symbol": "600150.SH", "name": "中国船舶", "score": 12.0,
         "advice": "观望", "price": 30.1, "mystery": {}},
        {"symbol": "600519.SH", "name": "贵州茅台", "score": 60.0,
         "advice": "关注", "price": 1680.0, "mystery": {}},
    ]
    data = m.excel_bytes(results)
    wb = openpyxl.load_workbook(io.BytesIO(data))
    ws = wb["汇总报告"]
    # 冻结窗格：首行 + 名称列(A) → B2
    assert ws.freeze_panes == "B2"
    # 表头首行加粗
    assert ws["A1"].font.bold is True
    # 名称列(1) 有超链接；代码列(2) 无超链接
    for r in range(2, ws.max_row + 1):
        name_cell = ws.cell(row=r, column=1)
        code_cell = ws.cell(row=r, column=2)
        assert name_cell.hyperlink is not None, f"第{r}行名称缺少详情超链接"
        assert str(name_cell.hyperlink.target).startswith("http")
        assert "stock=" in str(name_cell.hyperlink.target)
        assert code_cell.hyperlink is None, f"第{r}行代码列不应有链接(W14 契约)"
        # 名称单元格文字仍是股票名（链接叠加在文字上）
        assert name_cell.value is not None
