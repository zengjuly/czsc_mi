"""mystery.apps.web.auth — Web 访问鉴权（应用层登录门，W42 v0.10.15）。

设计（零新依赖，纯标准库）：
- 凭据文件 ``~/.config/czsc_mi/web_auth.json``：
  ``{"user": "...", "salt": "<hex>", "iterations": 200000,
     "hash": "<hex>", "alg": "pbkdf2_sha256"}``，chmod 600，不进 git。
- **文件存在即启用**登录门；不存在则完全关闭（CI / integration 冒烟零影响）。
  显式关闭：env ``MYSTERY_WEB_AUTH=0``。
- 口令校验用 PBKDF2-HMAC-SHA256 + ``hmac.compare_digest``；不落明文、不用 md5。
- 失败限速：连续 8 次错误锁 60 秒（会话级，防在线暴力；本机个人站够用）。
- 会话持久化走 ``st.session_state["_auth_ok"]``（同一连接 rerun 不掉线，
  断开/刷新需重登）。

管理口令：``python scripts/web_auth_init.py``（getpass 交互，支持 --user /
--password-env 供非交互初始化）。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

AUTH_FILE = Path.home() / ".config" / "czsc_mi" / "web_auth.json"
_ITERATIONS = 200_000
_MAX_FAILS = 8
_LOCK_SECONDS = 60


def hash_password(password: str, salt_hex: str | None = None,
                  iterations: int = _ITERATIONS) -> dict:
    """生成凭据 dict（salt 缺省随机 16 字节）。"""
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             salt, iterations)
    return {"salt": salt.hex(), "iterations": iterations,
            "hash": dk.hex(), "alg": "pbkdf2_sha256"}


def verify_password(password: str, cred: dict) -> bool:
    """常时比较校验；凭据字段缺失/坏格式一律 False（不抛异常）。"""
    try:
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(cred["salt"]),
                                 int(cred["iterations"]))
        return hmac.compare_digest(dk.hex(), str(cred.get("hash", "")))
    except (KeyError, ValueError, TypeError):
        return False


def load_credential() -> dict | None:
    """返回凭据 dict（已并入 user 字段），未启用/坏文件返回 None。"""
    if os.environ.get("MYSTERY_WEB_AUTH", "") == "0":
        return None
    try:
        data = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not (isinstance(data, dict) and data.get("user") and data.get("hash")
            and data.get("salt") and data.get("iterations")):
        return None
    return data


def require_login() -> bool:
    """登录门。True=放行（未启用或本会话已验证）；False=本次渲染已出示登录页，
    调用方必须 st.stop()。仅可在 Streamlit 运行时脚本内调用。"""
    cred = load_credential()
    if cred is None:
        return True
    import streamlit as st

    if st.session_state.get("_auth_ok"):
        return True

    now = time.time()
    locked_until = st.session_state.get("_auth_locked_until", 0.0)
    if now < locked_until:
        st.title("🔒 Mistery 趋势交易分析")
        st.error(f"失败次数过多，请 {int(locked_until - now) + 1} 秒后重试")
        return False

    st.title("🔒 Mistery 趋势交易分析")
    with st.form("login_form"):
        user = st.text_input("用户名")
        pwd = st.text_input("密码", type="password")
        ok = st.form_submit_button("登录", type="primary")

    if ok:
        if user == str(cred["user"]) and verify_password(pwd, cred):
            st.session_state["_auth_ok"] = True
            st.session_state.pop("_auth_fails", None)
            st.session_state.pop("_auth_locked_until", None)
            st.rerun()
        fails = int(st.session_state.get("_auth_fails", 0)) + 1
        st.session_state["_auth_fails"] = fails
        if fails >= _MAX_FAILS:
            st.session_state["_auth_locked_until"] = now + _LOCK_SECONDS
            st.session_state["_auth_fails"] = 0
            st.error("失败次数过多，已锁定 60 秒")
        else:
            st.error(f"用户名或密码错误（{fails}/{_MAX_FAILS}）")
    return False
