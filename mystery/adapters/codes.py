"""mystery.adapters.codes — 代码 / 周期 / 复权归一（纯函数）。

内部统一代码格式：600519.SH（沪深）；周期：1d / 1w / 1M。
"""
from __future__ import annotations

import re

_INTERNAL_RE = re.compile(r"^(\d{6})\.(SH|SZ)$", re.I)


def normalize_symbol(symbol: str) -> str:
    """sh600519 / 600519.SH / SH600519 / sh.600519 / 600519 → 600519.SH"""
    s = str(symbol).strip()
    if _INTERNAL_RE.match(s):
        return s.upper()
    m = re.match(r"^(?:(sh|sz)\.?)?(\d{6})(?:\.(SH|SZ))?$", s, re.I)
    if not m:
        raise ValueError(f"无法识别股票代码: {symbol!r}")
    prefix, digits, suffix = m.group(1), m.group(2), m.group(3)
    exch = (suffix or prefix or "").upper()
    if not exch:
        # 无前缀无后缀：按交易所规则推断
        exch = "SH" if digits[0] in "569" else "SZ"
    return f"{digits}.{exch}"


def to_ths(symbol: str) -> str:
    """给扶摇：600519.SH"""
    return normalize_symbol(symbol)


def to_tdx_api(symbol: str) -> str:
    """给 tdx-api：SH600519"""
    s = normalize_symbol(symbol)
    digits, exch = s.split(".")
    return f"{exch}{digits}"


def to_tdx_local(symbol: str) -> str:
    """给 tdx 本地：sh600519"""
    s = normalize_symbol(symbol)
    digits, exch = s.split(".")
    return f"{exch.lower()}{digits}"


def db_code_of(symbol: str) -> str:
    """内部代码 600519.SH → 本地库格式 sh.600519。"""
    digits, exch = normalize_symbol(symbol).split('.')
    return f"{exch.lower()}.{digits}"


def exchange_of(symbol: str) -> str:
    return normalize_symbol(symbol).split(".")[1]


def normalize_freq(freq: str) -> str:
    """日线|daily|1d → 1d；周|weekly|1w → 1w；月|monthly|1M → 1M"""
    f = str(freq).strip().lower()
    if f in ("1d", "daily", "日线", "日k", "d"):
        return "1d"
    if f in ("1w", "weekly", "周线", "周k", "w"):
        return "1w"
    if f in ("1m", "1M", "monthly", "月线", "月k", "M"):
        return "1M"
    raise ValueError(f"无法识别周期: {freq!r}")


def normalize_adjust(adjust: str) -> str:
    a = str(adjust or "").strip().lower()
    return {"qfq": "qfq", "前复权": "qfq", "hfq": "hfq", "后复权": "hfq",
            "none": "none", "": "none"}.get(a, a)
