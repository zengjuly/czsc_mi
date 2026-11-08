"""W76：三浪评估上限网页覆盖层 + 结果列「名称在前」回归。

全部使用合成数据 + monkeypatch，零网络、零写库、零生产库/自选股改动。
- 覆盖层：稀疏语义 / env 优先 / 一键还原 / 损坏回退 / 实际驱动限流；
- 列顺序：Excel 汇总 / Web 扫描表 / 真三振池，名称必须排在代码之前。
"""
from __future__ import annotations

import inspect
import io
import json
import sys
from pathlib import Path

import openpyxl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mystery.services import analyze as svc  # noqa: E402
from mystery.services import runtime_settings as rts  # noqa: E402
from mystery.services import wave3_scan as ws  # noqa: E402


class _FakeClient:
    db = object()

    def resample_bars(self, daily, freq):
        return daily


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    """隔离覆盖层到临时文件、清空 env、让日K取数直接返回空（秒级）。"""
    p = tmp_path / "scan_wave3_override.json"
    monkeypatch.setattr(rts, "SCAN_WAVE3_OVERRIDE_PATH", p)
    for k in rts.SCAN_WAVE3_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    # 不 setenv("MYSTERY_SCAN_WAVE3", "1")：env 属「显式设置」，会破坏稀疏
    # 语义且优先级高于覆盖层；代码默认 "1" 已足够让三浪开启。
    monkeypatch.setattr(ws, "_daily_from_db", lambda db, sym: None)
    return p


def _rows(n):
    return [{"symbol": f"{600000 + i}.SH", "name": f"票{i}",
             "score": 100.0 - i * 0.01, "advice": "持有",
             "trade_date": "2026-11-08", "true_resonance": False}
            for i in range(n)]


def _attach(rows):
    return ws.attach_wave3_to_results(
        rows, db=object(), client=_FakeClient())


# ---------------------------------------------------------------------------
# 1. 覆盖层路径（顺带修 W74 的 Path("") 恒真缺陷）
# ---------------------------------------------------------------------------

def test_override_path_is_concrete_file():
    """默认路径必须是 <repo>/data 下的具体文件，不能退化成 '.'。"""
    assert not rts.RUNTIME_OVERRIDE_DIR.is_file()
    p = rts.resolve_override_path("__NO_SUCH_ENV_XYZ__", "x.json")
    assert p.name == "x.json"
    assert p.parent == rts.RUNTIME_OVERRIDE_DIR
    assert str(p) != "."


def test_resonance_override_path_fixed():
    """W74 修复：共振覆盖层不再是目录 '.'，且不是当前工作目录。"""
    p = svc.RESONANCE_OVERRIDE_PATH
    assert p.name == "resonance_override.json"
    assert not p.is_dir()
    assert p != Path(".")
    # 未被自定义时，读取应安全返回空而非抛 IsADirectoryError
    assert isinstance(svc.load_resonance_override(), dict)


# ---------------------------------------------------------------------------
# 2. 稀疏语义 / 读写 / 优先级 / 一键还原
# ---------------------------------------------------------------------------

def test_default_is_sparse_w75_semantics():
    """未自定义 → effective 为空（稀疏），W75 自动限流语义完全不变。"""
    assert rts.scan_wave3_options_effective() == {}
    assert rts.scan_wave3_mode() == "auto"
    disp = rts.scan_wave3_options_display()
    assert disp["MYSTERY_SCAN_WAVE3_AUTO_TRIGGER"] == "500"
    assert disp["MYSTERY_SCAN_WAVE3_AUTO_MAX"] == "200"
    assert disp["MYSTERY_SCAN_WAVE3"] == "1"
    # MAX 默认「未设置」绝不固化成 0（否则会跳过自动限流强制全量）
    assert "MYSTERY_SCAN_WAVE3_MAX" not in disp


def test_save_load_roundtrip_keeps_sparse(_clean):
    """只存已知键；MAX=0（强制全量）是合法显式值必须保留；None/空串跳过。"""
    rts.save_scan_wave3_override({
        "MYSTERY_SCAN_WAVE3_AUTO_MAX": 50,
        "MYSTERY_SCAN_WAVE3_MAX": 0,
        "MYSTERY_SCAN_WAVE3_STRICT": None,
        "MYSTERY_SCAN_WAVE3_AUTO_TRIGGER": "  ",
        "BOGUS_FIELD": "x",
    })
    loaded = json.loads(_clean.read_text(encoding="utf-8"))
    assert loaded == {
        "MYSTERY_SCAN_WAVE3_AUTO_MAX": "50",
        "MYSTERY_SCAN_WAVE3_MAX": "0",
    }
    assert rts.scan_wave3_override_exists() is True
    assert rts.load_scan_wave3_override() == loaded


def test_env_outranks_override(monkeypatch):
    """显式 env 最高优先，可覆盖网页设置（运维应急）。"""
    rts.save_scan_wave3_override({"MYSTERY_SCAN_WAVE3_AUTO_MAX": "50"})
    monkeypatch.setenv("MYSTERY_SCAN_WAVE3_AUTO_MAX", "7")
    eff = rts.scan_wave3_options_effective()
    assert eff["MYSTERY_SCAN_WAVE3_AUTO_MAX"] == "7"
    assert rts.scan_wave3_options_display()["MYSTERY_SCAN_WAVE3_AUTO_MAX"] == "7"


def test_clear_restores_default():
    """一键还原：删文件后回到 W75 自动限流 500/200。"""
    rts.save_scan_wave3_override({
        "MYSTERY_SCAN_WAVE3_MAX": "0",
        "MYSTERY_SCAN_WAVE3_AUTO_MAX": "999",
    })
    assert rts.scan_wave3_mode() == "full"
    rts.clear_scan_wave3_override()
    assert rts.scan_wave3_override_exists() is False
    assert rts.scan_wave3_options_effective() == {}
    assert rts.scan_wave3_mode() == "auto"


def test_corrupt_override_falls_back(_clean):
    """覆盖层损坏 → 安全回退空 dict，不抛错、不影响扫描。"""
    _clean.write_text("{ this is not json", encoding="utf-8")
    assert rts.load_scan_wave3_override() == {}
    assert rts.scan_wave3_options_effective() == {}


def test_mode_detection():
    """三种模式：未设=auto / MAX>0=fixed / MAX=0=full。"""
    assert rts.scan_wave3_mode() == "auto"
    rts.save_scan_wave3_override({"MYSTERY_SCAN_WAVE3_MAX": "300"})
    assert rts.scan_wave3_mode() == "fixed"
    rts.save_scan_wave3_override({"MYSTERY_SCAN_WAVE3_MAX": "0"})
    assert rts.scan_wave3_mode() == "full"


# ---------------------------------------------------------------------------
# 3. 网页保存的值真正驱动扫描收尾限流（不打生产库）
# ---------------------------------------------------------------------------

def test_override_auto_max_drives_attach():
    """网页把评估上限改成 50 → 600 只池实际只评 50 只，550 只未评估。"""
    rows = _rows(600)
    rts.save_scan_wave3_override({"MYSTERY_SCAN_WAVE3_AUTO_MAX": "50"})
    s = _attach(rows)
    assert s["enabled"] is True
    assert s["auto_limited"] is True
    assert s["cap"] == 50
    assert s["requested"] == 50
    assert sum(1 for r in rows
               if r.get("wave3", {}).get("verdict") == "未评估") == 550


def test_override_trigger_drives_attach():
    """网页把触发阈值改成 100 → 168 只池也会自动限流（默认 500 时不会）。"""
    rows = _rows(168)
    assert _attach(rows)["auto_limited"] is False   # 默认：小池不限
    rows2 = _rows(168)
    rts.save_scan_wave3_override({
        "MYSTERY_SCAN_WAVE3_AUTO_TRIGGER": "100",
        "MYSTERY_SCAN_WAVE3_AUTO_MAX": "20",
    })
    s = _attach(rows2)
    assert s["auto_limited"] is True and s["cap"] == 20


def test_override_fixed_max_disables_auto():
    """网页设固定上限 N → 显式语义，不再触发自动限流（与 env 等价）。"""
    rows = _rows(600)
    rts.save_scan_wave3_override({"MYSTERY_SCAN_WAVE3_MAX": "10"})
    s = _attach(rows)
    assert s["cap_reason"] == "explicit"
    assert s["auto_limited"] is False
    assert s["requested"] == 10


def test_override_disable_wave3(monkeypatch):
    """网页关闭三浪策略 → 不附加任何 wave3 字段。"""
    rows = _rows(3)
    rts.save_scan_wave3_override({"MYSTERY_SCAN_WAVE3": "0"})
    s = _attach(rows)
    assert s["enabled"] is False
    assert all("wave3" not in r for r in rows)


# ---------------------------------------------------------------------------
# 4. 结果列「名称在前，代码在后」（Web / Excel / 真三振池）
# ---------------------------------------------------------------------------

def _excel_headers(rows):
    from mystery.apps.reports.excel_report import excel_bytes
    wb = openpyxl.load_workbook(io.BytesIO(excel_bytes(rows)))
    return [c.value for c in wb["汇总报告"][1]]


def test_excel_name_before_code(monkeypatch):
    from mystery.apps.reports.excel_report import SUMMARY_COLS
    from mystery.services import watchlist as wl
    monkeypatch.setattr(wl, "load_tdx_boards", lambda: None)
    assert SUMMARY_COLS[0] == ("name", "名称")
    assert SUMMARY_COLS[1] == ("symbol", "代码")
    headers = _excel_headers([
        {"symbol": "sh600519", "name": "贵州茅台", "score": 49.0,
         "advice": "持有", "trade_date": "2026-11-08"},
    ])
    assert headers.index("名称") < headers.index("代码")
    assert headers[0] == "名称" and headers[1] == "代码"


def test_web_scan_table_name_before_code(monkeypatch):
    from mystery.apps.web.views.scan import _scan_table_data
    from mystery.services import watchlist as wl
    monkeypatch.setattr(wl, "load_tdx_boards", lambda: None)
    cols = list(_scan_table_data([_rows(1)[0]], nav_key="k")[0].keys())
    assert cols.index("名称") < cols.index("代码")
    assert cols[1] == "名称" and cols[2] == "代码"   # 首列仍是序号


def test_true_resonance_pool_name_before_code():
    """真三振池表格列顺序（静态防回归：面板内联 dict 无法直接取行）。"""
    from mystery.apps.web.views import system as sysview
    src = inspect.getsource(sysview.view_true_resonance)
    assert src.index("'名称'") < src.index("'代码'")
