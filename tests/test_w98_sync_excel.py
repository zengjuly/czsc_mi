"""W98：Excel 报告同步生成专项。

用户诉求：扫描完成时即同步落盘 Excel，不再等用户点下载按钮时现场补齐。
改造点：
- cli._write_scan_report(job_id=...)：按 job 独立命名，不覆盖 cron 正式日报；
- cli._cmd_scan_body：--report 时把 job_id 透传；
- scan_launcher.build_scan_argv：Web 后台扫描带 --report；
- web scan._report_path_of / _render_excel_download：优先读落盘文件。

零回归契约：
- 不传 job_id 时 _write_scan_report 文件名与旧版完全一致（cron/回放/手动）；
- --report 默认行为不变；
- 生成失败不阻塞扫描本身。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from mystery.apps import cli
from mystery.services import scan_launcher as sl


# ---------------------------------------------------------------- launcher argv
def test_build_scan_argv_market_has_report(monkeypatch):
    monkeypatch.setattr(sl, "cli_executable", lambda: "/venv/bin/czsc-mi")
    argv = sl.build_scan_argv("market", "abcd1234", exe="/venv/bin/czsc-mi")
    assert argv[:2] == ["/venv/bin/czsc-mi", "scan"]
    assert "--resume" in argv and "--market" in argv
    assert "--bg-task-id" in argv and "abcd1234" in argv
    assert "--report" in argv, "W98：后台扫描必须带 --report 以同步生成 Excel"
    assert "--watchlist" not in argv


def test_build_scan_argv_watchlist_has_report():
    argv = sl.build_scan_argv("watchlist", "ef567890", exe="czsc-mi")
    assert "--watchlist" in argv and "--market" not in argv
    assert "--report" in argv


def test_build_scan_argv_rejects_unknown():
    with pytest.raises(ValueError):
        sl.build_scan_argv("sector", "x")


# ---------------------------------------------------------------- 报告命名（纯函数验证）
def test_report_filename_job_vs_daily(monkeypatch):
    """同一天：job 命名 ≠ 正式日报命名，互不覆盖。"""
    import datetime as _dt

    class _FakeDate(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 17, 18, 0, 0)

    captured = {}
    monkeypatch.setattr(cli, "datetime", _FakeDate)
    monkeypatch.setattr(cli, "output_dir", lambda cfg=None: "/tmp/out")
    # 用极简 fake results + 打桩 write_excel/write_html，只验证路径分流
    import mystery.apps.reports.excel_report as ER
    import mystery.apps.reports.html_report as HR
    monkeypatch.setattr(ER, "write_excel", lambda results, path, **kw: captured.setdefault("xlsx", path))
    monkeypatch.setattr(HR, "write_html", lambda results, path, **kw: captured.setdefault("html", path))
    monkeypatch.setattr(cli, "_turnover_qa_line", lambda *a, **k: "")
    monkeypatch.setattr(cli, "_suffix_orphans_hard_fail", lambda: 0)

    class _Args:
        cfg = None
        as_of = ""

    cli._write_scan_report([{"symbol": "sh.600000", "trade_date": "2026-11-17"}], _Args())
    assert captured["xlsx"] == "/tmp/out/每日股票分析报告_20261117.xlsx"

    captured.clear()
    cli._write_scan_report([{"symbol": "sh.600000", "trade_date": "2026-11-17"}],
                           _Args(), job_id=112)
    assert captured["xlsx"] == "/tmp/out/扫描报告_job112_20261117.xlsx"
    assert captured["html"] == "/tmp/out/扫描报告_job112_20261117.html"
    assert "每日股票分析报告" not in captured["xlsx"]


def test_report_filename_replay_unchanged(monkeypatch):
    """回放命名（_回放_）零回归，且不因 job_id 改动。"""
    import datetime as _dt

    class _FakeDate(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 17)

    captured = {}
    monkeypatch.setattr(cli, "datetime", _FakeDate)
    monkeypatch.setattr(cli, "output_dir", lambda cfg=None: "/tmp/out")
    import mystery.apps.reports.excel_report as ER
    import mystery.apps.reports.html_report as HR
    monkeypatch.setattr(ER, "write_excel", lambda results, path, **kw: captured.setdefault("xlsx", path))
    monkeypatch.setattr(HR, "write_html", lambda results, path, **kw: captured.setdefault("html", path))
    monkeypatch.setattr(cli, "_turnover_qa_line", lambda *a, **k: "")
    monkeypatch.setattr(cli, "_suffix_orphans_hard_fail", lambda: 0)

    class _Args:
        cfg = None
        as_of = "2026-01-14"

    cli._write_scan_report([], _Args())
    assert captured["xlsx"] == "/tmp/out/每日股票分析报告_回放_20260114.xlsx"


# ---------------------------------------------------------------- Web 查找 helper
def test_report_path_of_none(monkeypatch):
    from mystery.apps.web.views.scan import _report_path_of
    assert _report_path_of(None) is None
    assert _report_path_of(0) is None


def test_report_path_of_finds_newest(monkeypatch, tmp_path):
    from mystery.apps.web.views import scan as S
    monkeypatch.setattr("mystery.config.output_dir", lambda cfg=None: str(tmp_path))
    (tmp_path / "扫描报告_job112_20261116.xlsx").write_bytes(b"old")
    newer = tmp_path / "扫描报告_job112_20261117.xlsx"
    newer.write_bytes(b"new")
    os.utime(newer, (2_000_000_000, 2_000_000_000))
    (tmp_path / "扫描报告_job999_20261117.xlsx").write_bytes(b"other")
    got = S._report_path_of(112)
    assert got and got.endswith("扫描报告_job112_20261117.xlsx")


def test_report_path_of_missing(tmp_path, monkeypatch):
    from mystery.apps.web.views import scan as S
    monkeypatch.setattr("mystery.config.output_dir", lambda cfg=None: str(tmp_path))
    assert S._report_path_of(112) is None
