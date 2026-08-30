"""CLI 入口默认 THS 环境注入回归测试（W8-env）。

czsc-mi 未设置 THS_FUYAO_SCRIPT / THS_MARKETDB_DIR 时，应从仓库
sibling（../Financial-API）推导默认并注入，避免 ths 直接降级。
"""
from __future__ import annotations

import os

import pytest

from mystery.apps.cli import _ensure_default_ths_env


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("THS_FUYAO_SCRIPT", raising=False)
    monkeypatch.delenv("THS_MARKETDB_DIR", raising=False)
    yield


def _repo_parent() -> str:
    """测试文件在 czsc_mi/tests/ 下 → 3 层 dirname = czsc_mi 的上级目录
    （该目录同时含 czsc_mi 与 Financial-API sibling）。"""
    return os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))


def test_inject_defaults_when_unset():
    fa = os.path.join(_repo_parent(), "Financial-API")
    if not os.path.isdir(fa):
        pytest.skip("Financial-API sibling 不存在")
    _ensure_default_ths_env()
    assert os.environ["THS_FUYAO_SCRIPT"].endswith(
        os.path.join("Financial-API", "python", "toolkit", "fuyao", "scripts",
                     "fuyao.py"))
    assert os.environ["THS_MARKETDB_DIR"].endswith(
        os.path.join("Financial-API", "data"))


def test_keep_user_env(monkeypatch):
    monkeypatch.setenv("THS_FUYAO_SCRIPT", "/custom/fuyao.py")
    monkeypatch.setenv("THS_MARKETDB_DIR", "/custom/data")
    _ensure_default_ths_env()
    assert os.environ["THS_FUYAO_SCRIPT"] == "/custom/fuyao.py"
    assert os.environ["THS_MARKETDB_DIR"] == "/custom/data"
