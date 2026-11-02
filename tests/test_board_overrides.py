"""⑤ 板块名外部映射覆盖（data/board_display_overrides.json）。"""
from __future__ import annotations

import json

import pytest

from mystery.adapters import tdx_local


@pytest.fixture
def override_file(monkeypatch, tmp_path):
    p = tmp_path / "board_display_overrides.json"
    monkeypatch.setattr(tdx_local, "BOARD_OVERRIDE_PATH", str(p))
    monkeypatch.setattr(tdx_local, "_BOARD_OVERRIDE_CACHE", None)
    return p


def test_override_beats_filename(override_file):
    override_file.write_text(
        json.dumps({"AI": "人工智能", "0131": "一月策略"}), encoding="utf-8")
    tdx_local.reload_board_overrides()
    assert tdx_local._board_name_of("AI.blk") == "人工智能"
    assert tdx_local._board_name_of("0131.blk") == "一月策略"


def test_override_file_autocreated_when_missing(tmp_path, monkeypatch):
    p = tmp_path / "board_display_overrides.json"
    monkeypatch.setattr(tdx_local, "BOARD_OVERRIDE_PATH", str(p))
    monkeypatch.setattr(tdx_local, "_BOARD_OVERRIDE_CACHE", None)
    # 首次读取：文件不存在 → 自动建空模板，返回空映射（不报错）
    assert tdx_local._load_board_overrides() == {}
    assert p.exists()
    assert json.loads(p.read_text(encoding="utf-8")) == {}


def test_override_no_side_effect_on_normal(override_file, monkeypatch):
    # 隔离 blocknew.cfg，确保只测 覆盖+文件名兜底 路径
    monkeypatch.setattr(tdx_local, "resolve_blocknew_dirs", lambda: [])
    assert tdx_local._load_board_overrides() == {}
    # zxg 特例仍生效
    assert tdx_local._board_name_of("zxg.blk") == "自选股"
    # 不在映射里的缩写回退到文件名（与从前一致；ASCII 文件名原样保留）
    assert tdx_local._board_name_of("MI.blk") == "MI"
