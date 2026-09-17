"""W42 v0.10.15 Web 鉴权单元测试（纯 stdlib，CI 离线集可跑）。"""
import json
import sys

import pytest

from mystery.apps.web import auth as AU


def test_hash_verify_roundtrip():
    cred = AU.hash_password("S3cret-pass!")
    assert cred["alg"] == "pbkdf2_sha256" and cred["iterations"] >= 100_000
    assert AU.verify_password("S3cret-pass!", cred)
    assert not AU.verify_password("wrong", cred)
    # salt 随机：同口令两次哈希不同
    assert AU.hash_password("S3cret-pass!")["hash"] != cred["hash"]
    # 固定 salt 可复现
    c2 = AU.hash_password("S3cret-pass!", salt_hex=cred["salt"],
                          iterations=cred["iterations"])
    assert c2["hash"] == cred["hash"]


def test_verify_bad_credential_fields():
    assert not AU.verify_password("x", {})
    assert not AU.verify_password("x", {"salt": "zz", "iterations": 1})
    assert not AU.verify_password("x", {"salt": "00", "iterations": "nan"})


def test_load_credential_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(AU, "AUTH_FILE", tmp_path / "nope.json")
    monkeypatch.delenv("MYSTERY_WEB_AUTH", raising=False)
    assert AU.load_credential() is None


def test_load_credential_env_disable(tmp_path, monkeypatch):
    p = tmp_path / "web_auth.json"
    p.write_text(json.dumps({"user": "u", "salt": "00", "iterations": 1,
                             "hash": "00"}))
    monkeypatch.setattr(AU, "AUTH_FILE", p)
    monkeypatch.setenv("MYSTERY_WEB_AUTH", "0")
    assert AU.load_credential() is None


def test_load_credential_bad_or_partial_file(tmp_path, monkeypatch):
    monkeypatch.delenv("MYSTERY_WEB_AUTH", raising=False)
    p = tmp_path / "web_auth.json"
    p.write_text("{ broken")
    monkeypatch.setattr(AU, "AUTH_FILE", p)
    assert AU.load_credential() is None
    p.write_text(json.dumps({"user": "u"}))  # 缺 hash/salt/iterations
    assert AU.load_credential() is None


def test_token_roundtrip_and_tamper():
    cred = {"user": "u", **AU.hash_password("p", salt_hex="00" * 16,
                                            iterations=1000)}
    tok = AU.make_auth_token(cred)
    assert AU.verify_auth_token(tok, cred)
    # W48 格式：<exp>.<nonce>.<sig>
    exp, nonce, sig = tok.split(".")
    assert len(nonce) == 16
    # 篡改签名 / 篡改过期时间 / 篡改nonce / 空 / 坏格式 / 其他凭据 → 全部拒绝
    assert not AU.verify_auth_token(f"{exp}.{nonce}{'0' * 31}{sig[-1:]}", cred)
    assert not AU.verify_auth_token(f"{int(exp) + 1}.{nonce}.{sig}", cred)
    assert not AU.verify_auth_token(f"{exp}.{nonce}{'f' * 32}", cred)
    assert not AU.verify_auth_token("", cred)
    assert not AU.verify_auth_token("abc", cred)
    assert not AU.verify_auth_token(f"{exp}.{sig}", cred)  # 旧两段格式拒收
    other = {"user": "v", **AU.hash_password("p", salt_hex="00" * 16,
                                             iterations=1000)}
    assert not AU.verify_auth_token(tok, other)  # 绑用户名：同hash不同user拒
    other2 = {"user": "u", **AU.hash_password("p", salt_hex="ff" * 16,
                                              iterations=1000)}
    assert not AU.verify_auth_token(tok, other2)
    # 过期 token 拒绝
    past = AU.make_auth_token(cred, ttl=-10)
    assert not AU.verify_auth_token(past, cred)
    # 默认时效 24h（不再是 7 天）
    import time as _t
    tok2 = AU.make_auth_token(cred)
    assert int(tok2.split(".")[0]) - _t.time() <= 24 * 3600 + 5


def test_fail_state_file_roundtrip(tmp_path, monkeypatch):
    """W48：失败计数/锁定落盘（换标签页不清零），锁满自动过期。"""
    ff = tmp_path / "fails.json"
    monkeypatch.setattr(AU, "FAILS_FILE", ff)
    assert AU._load_fail_state(100.0) == (0, 0.0)  # 缺文件=干净
    AU._save_fail_state(3, 0.0)
    assert AU._load_fail_state(100.0) == (3, 0.0)
    AU._save_fail_state(0, 200.0)
    assert AU._load_fail_state(150.0) == (0, 200.0)   # 锁定中
    assert AU._load_fail_state(250.0) == (0, 0.0)     # 到期清零
    ff.write_text("{ broken")
    assert AU._load_fail_state(100.0) == (0, 0.0)     # 坏文件不炸


def test_token_disabled_returns_empty(monkeypatch):
    monkeypatch.setenv("MYSTERY_WEB_AUTH", "0")
    assert AU.make_auth_token() == ""
    assert AU.verify_auth_token("123.abc") is False


def test_init_script_end_to_end(tmp_path, monkeypatch):
    """web_auth_init 非交互路径真实跑一遍：写 600 凭据 + 可校验回读。"""
    import os
    import stat
    import subprocess

    script = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "scripts", "web_auth_init.py")
    env = {**os.environ, "WEB_AUTH_PWD": "long-enough-pw-1",
           "HOME": str(tmp_path), "MYSTERY_WEB_AUTH": "1"}
    r = subprocess.run([sys.executable, script,
                        "--user", "tester", "--password-env"],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    target = tmp_path / ".config" / "czsc_mi" / "web_auth.json"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    monkeypatch.setattr(AU, "AUTH_FILE", target)
    loaded = AU.load_credential()
    assert loaded and loaded["user"] == "tester"
    assert AU.verify_password("long-enough-pw-1", loaded)
    assert not AU.verify_password("nope", loaded)
