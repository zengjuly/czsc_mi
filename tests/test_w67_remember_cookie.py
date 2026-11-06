"""W67：同一设备 48 小时免登录（浏览器持久 cookie）单元测试。

纯 stdlib / 不依赖真实 Streamlit runtime：用轻量 fake streamlit 模块替换
sys.modules，验证 require_login 的放行与回退分支。真实浏览器写/读 cookie
由 integration 端到端验证（见 W67 台账）。
"""
import sys
import time
import types

import pytest

from mystery.apps.web import auth as AU

ENV = "MYSTERY_WEB_AUTH_REMEMBER_HOURS"


def _cred():
    return {"user": "u", **AU.hash_password(
        "p", salt_hex="00" * 16, iterations=1000)}


def _fake_st(cookies=None, session_state=None):
    st = types.SimpleNamespace()
    st.runtime = types.SimpleNamespace(exists=lambda: True)
    st.session_state = dict(session_state or {})
    st.context = types.SimpleNamespace(cookies=dict(cookies or {}))
    st.query_params = {}
    return st


class _FormReached(Exception):
    """require_login 走到登录表单（即未被 cookie 放行）的探针。"""


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)


# ---------------- TTL ----------------
def test_remember_ttl_default_is_48h():
    assert AU._remember_ttl() == 48 * 3600


def test_remember_ttl_env_override_disable_and_bad_value(monkeypatch):
    monkeypatch.setenv(ENV, "12.5")
    assert AU._remember_ttl() == int(12.5 * 3600)
    monkeypatch.setenv(ENV, "0")
    assert AU._remember_ttl() == 0
    monkeypatch.setenv(ENV, "-3")
    assert AU._remember_ttl() == 0
    monkeypatch.setenv(ENV, "abc")
    assert AU._remember_ttl() == 48 * 3600


# ---------------- 写 cookie 的 JS ----------------
def test_remember_cookie_js_contains_48h_token():
    cred = _cred()
    ttl = AU._remember_ttl()
    tok = AU.make_auth_token(cred, ttl=ttl)
    js = AU._remember_cookie_js(tok, ttl)
    assert "<script>" in js and "</script>" in js
    assert AU._COOKIE_NAME in js and tok in js
    assert f"max-age={48 * 3600}" in js
    assert "path=/" in js and "SameSite=Lax" in js
    # 持久 token 到期约 48h；?at= 跨标签 token 默认仍是 24h（W48 契约不变）
    assert 48 * 3600 - 5 <= int(tok.split(".")[0]) - time.time() <= 48 * 3600 + 5
    assert int(AU.make_auth_token(cred).split(".")[0]) - time.time() <= 24 * 3600 + 5


def test_remember_cookie_js_escapes_token():
    """异常 token 含引号/尖括号时，仍被 json.dumps 包成 JS 字符串字面量。"""
    weird = "'\"><script>alert(1)</script>"
    js = AU._remember_cookie_js(weird, 100)
    assert '\\"' in js                       # 内容里的双引号被 JSON 转义
    assert js.count("<script>") == 2        # 外层 1 + token 内容 1
    assert js.endswith("</script>")
    assert "max-age=100" in js


# ---------------- 读端判定 ----------------
def test_cookie_token_valid_roundtrip_and_rejections():
    cred = _cred()
    tok = AU.make_auth_token(cred, ttl=AU._remember_ttl())
    C = AU._COOKIE_NAME
    assert AU._cookie_token_valid(cred, {C: tok})
    assert AU._cookie_token_valid(cred, {C: [tok]})        # AppTest 里可能是 list
    assert not AU._cookie_token_valid(cred, {})
    assert not AU._cookie_token_valid(cred, {C: ""})
    assert not AU._cookie_token_valid(cred, {C: tok + "0"})  # 篡改
    assert not AU._cookie_token_valid(cred, None)          # 坏对象不抛
    assert not AU._cookie_token_valid(cred, {C: object()})  # 非字符串
    # 过期 / 换凭据（改口令 → hash 变）即失效
    assert not AU._cookie_token_valid(
        cred, {C: AU.make_auth_token(cred, ttl=-10)})
    other = {"user": "u", **AU.hash_password(
        "q", salt_hex="ff" * 16, iterations=1000)}
    assert not AU._cookie_token_valid(other, {C: tok})


# ---------------- require_login 集成 ----------------
def test_require_login_accepts_remember_cookie(monkeypatch):
    """刷新后 session_state 为空，但带有效 cookie → 直接放行。"""
    cred = _cred()
    tok = AU.make_auth_token(cred, ttl=AU._remember_ttl())
    fake = _fake_st(cookies={AU._COOKIE_NAME: tok})
    monkeypatch.setitem(sys.modules, "streamlit", fake)
    monkeypatch.setattr(AU, "load_credential", lambda: cred)
    assert AU.require_login() is True
    assert fake.session_state.get("_auth_ok") is True


def test_require_login_without_cookie_shows_form(monkeypatch):
    """无 cookie → 仍走登录表单（原行为不变）。"""
    cred = _cred()
    fake = _fake_st()
    fake.title = lambda *a, **k: (_ for _ in ()).throw(_FormReached())
    monkeypatch.setitem(sys.modules, "streamlit", fake)
    monkeypatch.setattr(AU, "load_credential", lambda: cred)
    with pytest.raises(_FormReached):
        AU.require_login()


def test_require_login_disabled_remember_still_requires_login(monkeypatch):
    """env=0 关闭持久登录：即使带有效 cookie 也不放行。"""
    cred = _cred()
    tok = AU.make_auth_token(cred, ttl=48 * 3600)
    monkeypatch.setenv(ENV, "0")
    fake = _fake_st(cookies={AU._COOKIE_NAME: tok})
    fake.title = lambda *a, **k: (_ for _ in ()).throw(_FormReached())
    monkeypatch.setitem(sys.modules, "streamlit", fake)
    monkeypatch.setattr(AU, "load_credential", lambda: cred)
    with pytest.raises(_FormReached):
        AU.require_login()


def test_require_login_writes_cookie_after_successful_login(monkeypatch):
    """登录成功 → st.rerun 后的第一次 run 落 cookie，且只落一次。"""
    cred = _cred()
    fake = _fake_st(session_state={"_auth_ok": True,
                                   "_auth_need_cookie": True})
    calls = []
    monkeypatch.setitem(sys.modules, "streamlit", fake)
    monkeypatch.setattr(AU, "load_credential", lambda: cred)
    monkeypatch.setattr(AU, "_set_remember_cookie",
                        lambda c: calls.append(c))
    assert AU.require_login() is True
    assert calls == [cred]
    assert "_auth_need_cookie" not in fake.session_state
    # 第二次 run 不再写
    assert AU.require_login() is True
    assert len(calls) == 1


def test_set_remember_cookie_emits_html_component(monkeypatch):
    """写端确实调用 components.html 且 height=0（不可见）、JS 携带 48h token。"""
    cred = _cred()
    calls = []

    st_mod = types.ModuleType("streamlit")
    comp = types.ModuleType("streamlit.components")
    v1 = types.ModuleType("streamlit.components.v1")
    v1.html = lambda html, height=None: calls.append((html, height))
    st_mod.components = comp
    comp.v1 = v1
    monkeypatch.setitem(sys.modules, "streamlit", st_mod)
    monkeypatch.setitem(sys.modules, "streamlit.components", comp)
    monkeypatch.setitem(sys.modules, "streamlit.components.v1", v1)

    AU._set_remember_cookie(cred)
    assert len(calls) == 1
    js, height = calls[0]
    assert height == 0
    assert AU._COOKIE_NAME in js and f"max-age={48 * 3600}" in js

    monkeypatch.setenv(ENV, "0")
    calls.clear()
    AU._set_remember_cookie(cred)
    assert calls == []          # 关闭时不写
