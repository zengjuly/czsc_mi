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
