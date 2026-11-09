"""W78：czsc 升级到 1.0.2 的回归护栏（v0.10.37）。

背景：生产 czsc 从 1.0.1 升到 1.0.2。1.0.2 是 Rust/PyO3 架构（cp310-abi3
wheel，兼容 py3.12），声明新增 urllib3>=2.8.0 / wbt>=0.2.1 / requests-toolbelt。
升级前已在隔离目录完成全链路验证（见 docs/W78_czsc_1.0.2_升级.md）：
  - CZSC 结构字段（bi_list/fx_list/zs_list/bars_raw/finished_bis）齐全；
  - 5 个信号函数（3 买卖点 + 2 背驰）call_signal 全部可调用且在注册表；
  - 金标三只 ChanStructure 在 1.0.1→1.0.2 逐字段零漂移；
  - 完整升级态（czsc 1.0.2 + urllib3 2.8.0）全量 pytest 通过。

本测试不硬编码 czsc 版本（部署窗口内生产可能仍是 1.0.1），只校验两件事：
  1) 依赖下限已抬到 1.0.2（纯文本，不依赖 czsc）；
  2) 一旦 czsc 可导入，adapter 依赖的 CZSC 字段与信号函数契约仍成立
     （1.0.1 与 1.0.2 都应通过；缺 czsc 时 skip，chan 本就是可选依赖）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


def test_chan_dependency_floor_is_102():
    """pyproject.toml 的 chan extra 下限必须是 czsc>=1.0.2,<2.0。"""
    text = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    # 不引 tomllib（py3.10 兼容），直接找 chan 行
    chan_line = next((ln.strip() for ln in text.splitlines()
                      if ln.strip().startswith("chan")), "")
    assert "czsc>=1.0.2" in chan_line, f"chan 下限未升级: {chan_line!r}"
    assert "<2.0" in chan_line, f"缺少 <2.0 上界: {chan_line!r}"


def _synthetic_barseries(n: int = 300):
    import numpy as np
    import pandas as pd
    from mystery.core.models import Bar, BarSeries

    rng = np.random.default_rng(42)
    base = (np.linspace(10, 12, n)
            + np.sin(np.linspace(0, 8 * np.pi, n)) * 0.6
            + rng.normal(0, 0.15, n))
    dts = pd.date_range("2024-01-01", periods=n, freq="B")
    bars = [Bar(dt=str(d)[:10], open=float(o), high=float(o + 0.3),
                low=float(o - 0.3), close=float(o + 0.05),
                volume=float(v), amount=float(a))
            for d, o, v, a in zip(dts, base,
                                  rng.integers(1_000_000, 1_500_000, n),
                                  rng.integers(10_000_000, 15_000_000, n))]
    return BarSeries(symbol="TEST", freq="1d", bars=bars)


def test_adapter_structure_contract_with_installed_czsc():
    """czsc 可用时：CzscAdapter.analyze() 能抽取完整 ChanStructure，不抛异常。

    覆盖 _extract 依赖的 CZSC 字段：bi_list / fx_list / zs_list / bars_raw /
    finished_bis 及 bi/zs/bar 的属性。1.0.1 与 1.0.2 均须通过。
    """
    pytest.importorskip("czsc")  # chan 可选依赖；无则跳过
    from mystery.adapters.czsc_adapter import CzscAdapter, czsc_version

    st = CzscAdapter().analyze(_synthetic_barseries())
    assert st.engine == "czsc"
    assert st.engine_ver and st.engine_ver == czsc_version()
    assert st.freq == "1d"
    assert len(st.bis) >= 0           # 抽取链路未抛异常即可
    assert st.last_bi_dir in ("up", "down", "")
    assert st.in_zs in (True, False)
    assert isinstance(st.zs_position, str)
    assert isinstance(st.n_zs, int)


def test_native_signal_functions_callable():
    """czsc 可用时：signal_flags 依赖的 5 个信号函数 call_signal 均可调用。

    1.0.2 曾传闻有信号键迁移；此测试锁死「adapter 用到的 5 个名字仍可调用」，
    升级后在任一版本都能第一时间发现破坏性改名。
    """
    pytest.importorskip("czsc")
    from czsc import CZSC, Freq, format_standard_kline
    import czsc._native as native
    import numpy as np
    import pandas as pd

    n = 300
    rng = np.random.default_rng(7)
    base = np.linspace(10, 12, n) + np.sin(np.linspace(0, 8 * np.pi, n)) * 0.6
    df = pd.DataFrame({
        "symbol": ["TEST"] * n,
        "dt": pd.date_range("2024-01-01", periods=n, freq="B"),
        "open": base, "high": base + 0.3, "low": base - 0.3,
        "close": base + 0.05,
        "vol": rng.integers(1_000_000, 1_500_000, n),
        "amount": rng.integers(10_000_000, 15_000_000, n),
    })
    c = CZSC(format_standard_kline(df, freq=Freq.D), min_bi_len=7)

    sigs = {
        "cxt_first_buy_V221126": {"di": 1},
        "cxt_second_bs_V240524": {"di": 1, "big_gap_factor": 0.95, "window": 8},
        "cxt_third_bs_V230319": {"di": 1, "window": 8, "bm": 20,
                                 "fast": 5, "slow": 20, "vols_ma": 5},
        "tas_macd_bc_V240307": {},
        "tas_macd_bc_V230803": {},
    }
    for name, params in sigs.items():
        # 只要不抛异常即契约成立（值随行情不同，不作断言）
        list(native.call_signal(name, c, params=params))
