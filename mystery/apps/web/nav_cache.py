"""进程级导航缓存：跨标签页/会话共享扫描结果列表。

背景：扫描结果表的「详情」列使用 st.column_config.LinkColumn，
浏览器点击后默认在新标签页打开 = 全新 Streamlit session，session_state 不共享。
因此导航列表不能存 session_state，必须存进程级缓存（单进程 streamlit run）。

实现：模块级 dict + uuid key + TTL 30 分钟 + 上限 300 条惰性清理。
模块 import 只初始化一次，进程生命周期内跨 rerun / 跨 session 持久，
且不依赖 streamlit runtime（AppTest 外 pre-seed 也可用）。

多 worker / 多副本部署时该缓存不共享（nav_key 查不到 → 退化为独立查看，
导航不展示但不报错），本产品单进程部署，可接受。
"""
from __future__ import annotations

import threading
import time
import uuid

_store: dict[str, tuple[float, list]] = {}
_lock = threading.Lock()
_TTL = 30 * 60  # 30 分钟
_MAX = 300


def nav_cache_put(rows: list) -> str:
    """存导航列表，返回随机 nav_key。"""
    nav_key = uuid.uuid4().hex
    now = time.time()
    with _lock:
        _store[nav_key] = (now, rows)
        # 惰性清理：超过上限时清理过期项
        if len(_store) > _MAX:
            for k, (ts, _) in list(_store.items()):
                if now - ts > _TTL:
                    _store.pop(k, None)
    return nav_key


def nav_cache_get(nav_key: str) -> list | None:
    """按 nav_key 取列表；过期或未知返回 None。"""
    if not nav_key:
        return None
    with _lock:
        item = _store.get(nav_key)
        if not item:
            return None
        ts, rows = item
        if time.time() - ts > _TTL:
            _store.pop(nav_key, None)
            return None
        return rows


def nav_cache_clear() -> None:
    """清空全部缓存（测试用）。"""
    with _lock:
        _store.clear()