"""W74：三振共振口径运行时覆盖层（网页/CLI 可改、一键还原、热生效）。

聚焦 service 层覆盖层读写与 effective 合并逻辑；UI 表单在 views/system.py。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mystery.services.analyze as svc  # noqa: E402
from mystery.core.resonance import resolve_resonance_options  # noqa: E402


@pytest.fixture
def override_path(tmp_path, monkeypatch):
    """把覆盖层重定向到临时文件，并清空所有显式 env，保证隔离。"""
    p = tmp_path / "resonance_override.json"
    monkeypatch.setattr(svc, "RESONANCE_OVERRIDE_PATH", p)
    for env_key, _ in svc._RESONANCE_ENV_KEYS:
        monkeypatch.delenv(env_key, raising=False)
    return p


def test_no_override_equals_production_default(override_path):
    """未自定义 → effective 等于生产默认 yearline/80。"""
    assert svc.resonance_override_exists() is False
    eff = svc.resonance_options_effective()
    assert eff == svc.resonance_options_default()
    assert eff["market_mode"] == "yearline"
    assert eff["true_threshold"] == 80.0


def test_save_and_load_roundtrip(override_path):
    """save → load 往返，只存已知字段，非法字段被丢弃。"""
    svc.save_resonance_override({
        "market_mode": "ma",
        "true_threshold": 75.0,
        "capital_min_score": 6.0,
        "capital_min_volume_ratio": 2.0,
        "index_max_lag_days": 3,
        "bogus_field": "x",  # 应被忽略
    })
    assert svc.resonance_override_exists() is True
    loaded = svc.load_resonance_override()
    assert loaded == {
        "market_mode": "ma",
        "true_threshold": 75.0,
        "capital_min_score": 6.0,
        "capital_min_volume_ratio": 2.0,
        "index_max_lag_days": 3,
    }


def test_effective_merges_override(override_path):
    """覆盖层生效：effective 取覆盖层值，且经 resolve 归一化。"""
    svc.save_resonance_override({
        "market_mode": "ma",
        "true_threshold": 75.0,
        "index_max_lag_days": None,
    })
    eff = svc.resonance_options_effective()
    norm = resolve_resonance_options(eff)
    assert norm["market_mode"] == "ma"
    assert norm["true_threshold"] == 75.0
    # 未覆盖字段回退到生产默认
    assert norm["capital_min_score"] == 8.0
    assert norm["capital_min_volume_ratio"] == 1.5


def test_clear_returns_to_default(override_path):
    """一键还原：clear 后 effective 回到生产默认。"""
    svc.save_resonance_override({"market_mode": "ma", "true_threshold": 70.0})
    assert svc.resonance_override_exists() is True
    svc.clear_resonance_override()
    assert svc.resonance_override_exists() is False
    eff = svc.resonance_options_effective()
    assert eff == svc.resonance_options_default()
    assert eff["market_mode"] == "yearline"
    assert eff["true_threshold"] == 80.0


def test_env_outranks_override(override_path, monkeypatch):
    """显式 env 最高优先，可覆盖网页设置（运维应急）。"""
    svc.save_resonance_override({"market_mode": "yearline", "true_threshold": 75.0})
    monkeypatch.setenv("MYSTERY_RESONANCE_MARKET_MODE", "ma")
    monkeypatch.setenv("MYSTERY_RESONANCE_TRUE_THRESHOLD", "90")
    eff = svc.resonance_options_effective()
    norm = resolve_resonance_options(eff)
    assert norm["market_mode"] == "ma"          # env 覆盖网页
    assert norm["true_threshold"] == 90.0        # env 覆盖网页


def test_cache_tag_changes_with_override(override_path):
    """缓存键随口径变化：不同覆盖层得到不同 tag（W23「改任意一项必 miss」）。"""
    tag_default = svc.resonance_cache_tag()
    svc.save_resonance_override({"market_mode": "ma", "true_threshold": 85.0})
    tag_ma = svc.resonance_cache_tag()
    svc.save_resonance_override({"market_mode": "yearline", "true_threshold": 80.0})
    tag_year = svc.resonance_cache_tag()
    assert tag_default != tag_ma
    assert tag_ma != tag_year
    assert tag_default.startswith("|R[")


def test_corrupt_override_falls_back(override_path):
    """覆盖层文件损坏 → 安全回退空 dict，不抛错。"""
    override_path.write_text("{ this is not json", encoding="utf-8")
    assert svc.load_resonance_override() == {}
    assert svc.resonance_options_effective() == svc.resonance_options_default()
