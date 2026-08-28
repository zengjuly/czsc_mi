"""test_codes — W5 代码/周期/复权归一（离线绿）。"""
import pytest

from mystery.adapters.codes import (
    db_code_of,
    exchange_of,
    normalize_adjust,
    normalize_freq,
    normalize_symbol,
    to_tdx_api,
    to_tdx_local,
    to_ths,
)


@pytest.mark.parametrize("raw", ["sh600519", "600519.SH", "SH600519",
                                 "sh.600519", "600519"])
def test_normalize_sh(raw):
    assert normalize_symbol(raw) == "600519.SH"


@pytest.mark.parametrize("raw", ["sz000001", "000001.SZ", "SZ000001",
                                 "sz.000001", "000001"])
def test_normalize_sz(raw):
    assert normalize_symbol(raw) == "000001.SZ"


def test_db_code_of():
    assert db_code_of("600519.SH") == "sh.600519"
    assert db_code_of("sh600519") == "sh.600519"
    assert db_code_of("000001.SZ") == "sz.000001"


def test_export_formats():
    assert to_ths("sh600519") == "600519.SH"
    assert to_tdx_api("sh600519") == "SH600519"
    assert to_tdx_local("sh600519") == "sh600519"
    assert exchange_of("600519") == "SH"


def test_normalize_freq():
    assert normalize_freq("日线") == "1d"
    assert normalize_freq("daily") == "1d"
    assert normalize_freq("1d") == "1d"
    assert normalize_freq("weekly") == "1w"
    assert normalize_freq("周线") == "1w"
    assert normalize_freq("月线") == "1M"
    with pytest.raises(ValueError):
        normalize_freq("5m")


def test_normalize_adjust():
    assert normalize_adjust("qfq") == "qfq"
    assert normalize_adjust("前复权") == "qfq"
    assert normalize_adjust("hfq") == "hfq"
    assert normalize_adjust("") == "none"
