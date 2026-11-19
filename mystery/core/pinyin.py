"""mystery.core.pinyin — 拼音首字母工具（W104）。

用途：个股/板块分析页的「加自选」「选股票/板块」selectbox 支持拼音首字母
匹配查询，例如输入 ``gzmt`` 命中「贵州茅台」，输入 ``bd`` 命中「半导体」。

实现原理：Streamlit selectbox 按选项文本做大小写不敏感的子串过滤，因此把
首字母以 `` · GZMT`` 后缀拼进选项文本即可获得检索能力，无需前端改造。
解析选项时统一走 :func:`parse_option`，兼容有无后缀两种格式。

pypinyin 为**可选依赖**：未安装时 ``initials()`` 返回空串，选项退化为纯
「名称（代码）」，既有中文名/代码搜索完全不受影响，只是不提供拼音检索。
安装：``pip install pypinyin``（已加入 pyproject 的 pinyin extra）。
"""
from __future__ import annotations

import re

# 进程内缓存：全市场约 5000 只股票 + 板块，首字母只算一次。
_CACHE: dict[str, str] = {}

# 仅保留大写字母与数字，去掉空格/括号/符号（如 "ST 荃银" -> "STQY"）。
_NON_ALNUM = re.compile(r"[^A-Z0-9]")

# 选项文本："名称（代码） · GZMT"，后缀可有可无。
_OPTION_RE = re.compile(r"^(?P<name>.*?)（(?P<code>[^（）]+)）(?:\s*·\s*[A-Z0-9]+)?$")


def initials(text: str) -> str:
    """返回文本的拼音首字母大写串。

    例：
        ``贵州茅台`` -> ``GZMT``
        ``ST荃银``   -> ``STQY``
        ``沪深300``  -> ``HS300``

    :param text: 中文名称（可含英文/数字）。
    :return: 首字母大写串；pypinyin 未安装、文本为空或计算异常时返回 ``''``。
    """
    s = str(text or "").strip()
    if not s:
        return ""
    if s in _CACHE:
        return _CACHE[s]
    try:
        from pypinyin import lazy_pinyin, Style
    except Exception:  # pypinyin 未安装/损坏 → 优雅降级，不影响原功能
        _CACHE[s] = ""
        return ""
    try:
        parts = lazy_pinyin(s, style=Style.FIRST_LETTER,
                            errors=lambda x: x, strict=False)
        out = _NON_ALNUM.sub("", "".join(parts).upper())
    except Exception:
        out = ""
    _CACHE[s] = out
    return out


def matches(text: str, keyword: str) -> bool:
    """判断关键字是否命中文本本身或其拼音首字母（均大小写不敏感）。

    供 ``search_stock`` 等纯 Python 搜索复用，UI 层 selectbox 不需要此函数。
    """
    kw = str(keyword or "").strip().upper()
    if not kw:
        return False
    if kw in str(text or "").upper():
        return True
    py = initials(text)
    return bool(py) and kw in py


def decorate_option(name: str, code: str) -> str:
    """生成供 selectbox 使用的选项文本：``名称（代码） · 首字母``。

    首字母为空（pypinyin 未安装）时退化为 ``名称（代码）``，保持向后兼容。
    """
    base = f"{name or '未知'}（{code}）"
    py = initials(name)
    return f"{base} · {py}" if py else base


def parse_option(opt: str) -> tuple[str, str]:
    """把选项文本解析回 ``(code, name)``。

    兼容 ``名称（代码）`` 与 ``名称（代码） · 首字母`` 两种格式；
    解析失败时尽力退化：无括号则把整串当作 code（沿用旧行为）。
    """
    s = str(opt or "").strip()
    m = _OPTION_RE.match(s)
    if m:
        return m.group("code").strip(), m.group("name").strip()
    if "（" in s:
        name, code = s.rsplit("（", 1)
        return code.rstrip("）").strip(), name.strip()
    return s, ""


__all__ = ["initials", "matches", "decorate_option", "parse_option"]
