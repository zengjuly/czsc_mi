"""mystery.apps.web.auth — Web 访问鉴权（应用层登录门，W42 v0.10.15）。

设计（零新依赖，纯标准库）：
- 凭据文件 ``~/.config/czsc_mi/web_auth.json``：
  ``{"user": "...", "salt": "<hex>", "iterations": 200000,
     "hash": "<hex>", "alg": "pbkdf2_sha256"}``，chmod 600，不进 git。
- **文件存在即启用**登录门；不存在则完全关闭（CI / integration 冒烟零影响）。
  显式关闭：env ``MYSTERY_WEB_AUTH=0``。
- 口令校验用 PBKDF2-HMAC-SHA256 + ``hmac.compare_digest``；不落明文、不用 md5。
- 失败限速：连续 8 次错误锁 60 秒（会话级，防在线暴力；本机个人站够用）。
- 会话持久化走 ``st.session_state["_auth_ok"]``；跨标签页（扫描表「详情」
  LinkColumn 新标签 = 全新 session）用 URL 内嵌 HMAC token ``?at=`` 自动放行，
  密钥派生自凭据 hash，负载绑 用户名+nonce，24 小时时效；token 验过即从
  query 摘除。失败限速跨会话（文件级）。

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
FAILS_FILE = Path.home() / ".config" / "czsc_mi" / "web_auth_fails.json"
_ITERATIONS = 200_000
_MAX_FAILS = 8
_LOCK_SECONDS = 60


def _load_fail_state(now: float) -> tuple[int, float]:
    """(失败计数, 锁定截止秒)。文件缺失/坏损视为干净状态。"""
    try:
        d = json.loads(FAILS_FILE.read_text(encoding="utf-8"))
        fails = int(d.get("fails", 0))
        locked = float(d.get("locked_until", 0.0))
        if locked and now >= locked:  # 锁定期已过，计数清零
            return 0, 0.0
        return fails, locked
    except (OSError, ValueError, TypeError):
        return 0, 0.0


def _save_fail_state(fails: int, locked_until: float) -> None:
    try:
        FAILS_FILE.parent.mkdir(parents=True, exist_ok=True)
        FAILS_FILE.write_text(json.dumps(
            {"fails": fails, "locked_until": locked_until}), encoding="utf-8")
        os.chmod(FAILS_FILE, 0o600)
    except OSError:
        pass  # 限速尽力而为，不得因写盘失败拒绝正常登录


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


# ---------------- 跨标签页会话保持（W42 v0.10.16；W48 加固） ----------------
# 扫描表「详情」列是 LinkColumn，新标签页打开 = 全新 Streamlit session，
# session_state 登录位不共享 → 又要求输密码。解法：详情链接内嵌 HMAC 签名
# token，新页验签通过即自动放行。密钥派生自凭据 hash 字段（不可反推密码）。
# W48（review P1#7）：token 负载绑定 用户名+随机nonce（'<exp>.<nonce>'），
# 换凭据文件/改口令即全量失效，且不可被手工构造；默认时效 7 天 → 24 小时
# （跨标签跳转只需秒级，24h 已属宽裕）。token 仍会进浏览器历史，本机池
# 内网个人工具威胁模型下接受该暴露面。


def _token_key(cred: dict) -> bytes:
    return hashlib.sha256(("czsc_mi_auth_token:" + str(cred["hash"])).encode(
        "utf-8")).digest()


def make_auth_token(cred: dict | None = None,
                    ttl: int = 24 * 3600) -> str:
    """签发跨标签页 token：'<exp_unix>.<nonce>.<hmac_hex32>'；未启用返回 ''。

    签名覆盖 exp+nonce+用户名：换凭据（含改口令 → hash 变）后旧 token 全失效。
    """
    cred = cred or load_credential()
    if cred is None:
        return ""
    exp = int(time.time()) + ttl
    nonce = secrets.token_hex(8)
    payload = f"{exp}.{nonce}"
    msg = f"{payload}|{cred.get('user', '')}".encode("utf-8")
    sig = hmac.new(_token_key(cred), msg, hashlib.sha256).hexdigest()[:32]
    return f"{payload}.{sig}"


def verify_auth_token(token: str, cred: dict | None = None) -> bool:
    """校验 token 签名 + 时效；任何异常/缺失/过期均 False。"""
    cred = cred or load_credential()
    if cred is None or not token or token.count(".") != 2:
        return False
    payload, _, sig = token.rpartition(".")
    exp_s = payload.partition(".")[0]
    try:
        exp = int(exp_s)
    except ValueError:
        return False
    if exp <= time.time():
        return False
    msg = f"{payload}|{cred.get('user', '')}".encode("utf-8")
    want = hmac.new(_token_key(cred), msg, hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(sig, want)


def require_login() -> bool:
    """登录门。True=放行（未启用或本会话已验证）；False=本次渲染已出示登录页，
    调用方必须 st.stop()。仅可在 Streamlit 运行时脚本内调用。"""
    cred = load_credential()
    if cred is None:
        return True
    import streamlit as st

    # 裸模式（无 ScriptRunContext，如 pytest 里 import app 模块）：登录表单无法
    # 交互且会在主线程残留 form 上下文、污染后续 AppTest（W42 实测），直接放行。
    if not st.runtime.exists():
        return True

    if st.session_state.get("_auth_ok"):
        return True

    # 跨标签页 token 放行（扫描表「详情」LinkColumn 新标签 = 新 session）
    try:
        raw = st.query_params.get("at")
        if isinstance(raw, (list, tuple)):  # AppTest 模拟里 query 值是 list
            raw = raw[0] if raw else ""
        if verify_auth_token(str(raw or ""), cred):
            st.session_state["_auth_ok"] = True
            try:
                del st.query_params["at"]  # 尽早从地址栏摘掉 token
            except Exception:
                pass
            return True
    except Exception:
        pass  # 非标准 runtime 拿不到 query_params → 走表单

    now = time.time()
    # W48（review P1#7）：失败锁定改文件级（~/.config/czsc_mi/web_auth_fails.json）
    # ——原来记在 session_state，换标签页/清 cookie 即重置，限速形同虚设。
    fails, locked_until = _load_fail_state(now)
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
        # 用户名也走常时比较（口令已是 compare_digest，用户名 == 泄露时序）
        if (hmac.compare_digest(user.encode("utf-8"),
                                str(cred["user"]).encode("utf-8"))
                and verify_password(pwd, cred)):
            _save_fail_state(0, 0.0)
            st.session_state["_auth_ok"] = True
            st.rerun()
        fails += 1
        if fails >= _MAX_FAILS:
            _save_fail_state(0, now + _LOCK_SECONDS)
            st.error("失败次数过多，已锁定 60 秒")
        else:
            _save_fail_state(fails, 0.0)
            st.error(f"用户名或密码错误（{fails}/{_MAX_FAILS}）")
    return False
