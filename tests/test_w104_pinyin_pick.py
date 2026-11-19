"""W104 单测：拼音首字母匹配（纯函数，零 Streamlit 依赖）。"""
import builtins
import importlib
import sys

import pytest

from mystery.core import pinyin as py


def test_initials_basic():
    assert py.initials("贵州茅台") == "GZMT"
    assert py.initials("半导体") == "BDT"  # 半(b)导(d)体(t)
    assert py.initials("科大讯飞") == "KDXF"


def test_initials_mixed_and_symbols():
    # 英文/数字保留，符号去掉
    assert py.initials("ST荃银") == "STQY"
    assert py.initials("沪深300") == "HS300"
    assert py.initials("XD海尔智") == "XDHEZ"


def test_initials_empty():
    assert py.initials("") == ""
    assert py.initials(None) == ""


def test_matches_by_name_or_pinyin():
    assert py.matches("贵州茅台", "gzmt") is True
    assert py.matches("贵州茅台", "GZMT") is True
    assert py.matches("贵州茅台", "贵州") is True
    assert py.matches("贵州茅台", "600519") is False  # 名称本身/首字母都不含代码
    assert py.matches("半导体", "bdt") is True
    assert py.matches("半导体", "bd") is True  # 前缀子串命中
    assert py.matches("半导体", "xyz") is False
    assert py.matches("贵州茅台", "") is False


def test_decorate_and_parse_roundtrip():
    opt = py.decorate_option("贵州茅台", "600519.SH")
    assert opt.startswith("贵州茅台（600519.SH）")
    assert "GZMT" in opt
    code, name = py.parse_option(opt)
    assert code == "600519.SH"
    assert name == "贵州茅台"


def test_parse_option_legacy_no_suffix():
    # 兼容旧格式「名称（代码）」
    assert py.parse_option("贵州茅台（600519.SH）") == ("600519.SH", "贵州茅台")
    assert py.parse_option("未知（sh600519）") == ("sh600519", "未知")


def test_parse_option_no_paren():
    assert py.parse_option("600519.SH") == ("600519.SH", "")


def test_parse_option_with_spaces():
    opt = "华特达因（000915.SZ） · HTDY"
    assert py.parse_option(opt) == ("000915.SZ", "华特达因")


def test_graceful_degrade_without_pypinyin(monkeypatch):
    """pypinyin 未安装时：initials 空串、decorate 退化为旧格式、解析不受影响。"""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pypinyin":
            raise ImportError("simulated: no pypinyin")
        return real_import(name, *args, **kwargs)

    py._CACHE.clear()
    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert py.initials("贵州茅台") == ""
    opt = py.decorate_option("贵州茅台", "600519.SH")
    assert opt == "贵州茅台（600519.SH）"  # 无后缀
    assert py.parse_option(opt) == ("600519.SH", "贵州茅台")
    py._CACHE.clear()
