"""test_blockname_cfg — blocknew.cfg 展示名解析（缩写代码 → 中文名）。

通达信自定义板块（blocknew）的真实展示名存于 blocknew.cfg（实测为
\x00 分隔的 (展示名, 代码) 成对记录）。本测试验证 _board_name_of 能
据此把 DL-S → 电力-算，而非回退为文件缩写。
"""
from __future__ import annotations

import os

import pytest

from mystery.adapters import tdx_local
from mystery.services import watchlist as wl


def _make_blocknew(tmp_path, files: dict, cfg_bytes: bytes = None) -> str:
    d = tmp_path / "T0002" / "blocknew"
    d.mkdir(parents=True)
    for fn, lines in files.items():
        (d / fn).write_bytes(("\n".join(lines) + "\n").encode("gbk"))
    if cfg_bytes is not None:
        (d / "blocknew.cfg").write_bytes(cfg_bytes)
    return str(d)


@pytest.fixture
def patch_tdx_cfg(monkeypatch, tmp_path):
    # blocknew.cfg：\x00 分隔的 (展示名, 代码) 对
    cfg = ("电力-算".encode("gbk") + b"\x00" + b"DL-S" + b"\x00" +
           "持仓股".encode("gbk") + b"\x00" + b"CCG" + b"\x00")
    blk = _make_blocknew(tmp_path, {
        "zxg.blk": ["1600519"],
        "DL-S.blk": ["1600036"],
        "CCG.blk": ["1600750"],
        "龙头股.blk": ["1600519"],
    }, cfg_bytes=cfg)
    monkeypatch.setattr(tdx_local, "resolve_blocknew_dirs", lambda: [blk])
    tdx_local._BLOCK_DISPLAY_CACHE.clear()
    return blk


def test_board_name_from_blocknew_cfg(patch_tdx_cfg):
    assert tdx_local._board_name_of("DL-S.blk") == "电力-算"
    assert tdx_local._board_name_of("CCG.blk") == "持仓股"
    # zxg 特例优先于 cfg
    assert tdx_local._board_name_of("zxg.blk") == "自选股"
    # 无 cfg 名的回退到文件名（GBK 还原）
    assert tdx_local._board_name_of("龙头股.blk") == "龙头股"


def test_enumerate_uses_cfg_names(patch_tdx_cfg):
    boards = wl.enumerate_tdx_boards({})
    assert "电力-算" in boards
    assert "持仓股" in boards
    assert "自选股" in boards
    assert "龙头股" in boards
    # 缩写代码不应再作为板块名出现
    assert "DL-S" not in boards
    assert "CCG" not in boards
