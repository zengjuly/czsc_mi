"""mystery.adapters.codes — 代码 / 周期 / 复权归一（纯函数）。

内部统一代码格式：600519.SH（沪深）；周期：1d / 1w / 1M。
"""
from __future__ import annotations

import re

_INTERNAL_RE = re.compile(r"^(\d{6})\.(SH|SZ|BJ)$", re.I)


def normalize_symbol(symbol: str) -> str:
    """sh600519 / 600519.SH / SH600519 / sh.600519 / 600519 / bj920002 → 600519.SH/920002.BJ"""
    s = str(symbol).strip()
    if _INTERNAL_RE.match(s):
        return s.upper()
    m = re.match(r"^(?:(sh|sz|bj)\.?)?(\d{6})(?:\.(SH|SZ|BJ))?$", s, re.I)
    if not m:
        raise ValueError(f"无法识别股票代码: {symbol!r}")
    prefix, digits, suffix = m.group(1), m.group(2), m.group(3)
    exch = (suffix or prefix or "").upper()
    if not exch:
        # 无前缀无后缀：按交易所规则推断
        # W41：北交所 43/83/87/92 段（旧实现只认 92，430/83x/87x 被误标 SZ，
        # 后续 db_code_of/扶摇/tdx 全链路错码）。
        # W48（review P1#8）：400/410/420 为老三板（两网及退市），无交易所
        # 前缀时无法与 BJ 43 段可靠区分，宁拒绝不猜错——猜 BJ 会把它们送进
        # 全市场扫描失败池。带显式 sh./sz. 前缀的仍按前缀走。
        if digits.startswith(("400", "410", "420")):
            raise ValueError(
                f"老三板代码 {symbol!r} 需带交易所前缀（sh./sz.）")
        if digits.startswith(("43", "83", "87", "92")):
            exch = "BJ"
        elif digits[0] in "569":
            exch = "SH"
        else:
            exch = "SZ"
    return f"{digits}.{exch}"


def is_bj_stock(symbol: str) -> bool:
    """判断是否北交所代码（920xxx）。"""
    try:
        return normalize_symbol(symbol).endswith(".BJ")
    except ValueError:
        return False


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
