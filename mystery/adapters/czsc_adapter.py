"""mystery.adapters.czsc_adapter — 唯一允许 import czsc 的分析适配。

把 BarSeries → CZSC 对象 → ChanStructure（core 模型），不向 core/web 泄漏
CZSC / BI / ZS / RawBar。读 CZSC_MIN_BI_LEN 环境变量，不改 czsc 源码。
"""
from __future__ import annotations

import importlib.metadata
import json
import os
from typing import Dict, List, Optional

import pandas as pd

from ..core.models import Bar, BarSeries, ChanBi, ChanStructure, ChanZs

_CZSC_MIN_BI_LEN = int(os.environ.get("CZSC_MIN_BI_LEN", "7"))
_FREQ_MAP = {"1d": "D", "1w": "W", "1M": "M"}


def czsc_version() -> str:
    try:
        return importlib.metadata.version("czsc")
    except Exception:
        return "unknown"


def _to_df(series: BarSeries) -> pd.DataFrame:
    """BarSeries → czsc 标准 DataFrame（symbol/dt/open/high/low/close/vol/amount）。"""
    rows = [{"symbol": series.symbol, "dt": pd.to_datetime(b.dt), "open": b.open,
             "high": b.high, "low": b.low, "close": b.close, "vol": b.volume,
             "amount": b.amount} for b in series.bars]
    return pd.DataFrame(rows)


class CzscAdapter:
    """缠论结构适配器。"""

    def __init__(self, min_bi_len: Optional[int] = None):
        self.min_bi_len = min_bi_len or _CZSC_MIN_BI_LEN

    def analyze(self, series: BarSeries) -> ChanStructure:
        """单周期分析：BarSeries → ChanStructure（czsc 唯一入口）。"""
        from czsc import CZSC, Freq, format_standard_kline

        if not series.bars:
            return ChanStructure(freq=series.freq, engine="czsc",
                                 engine_ver=czsc_version())
        df = _to_df(series)
        ohlc = df[["open", "high", "low", "close"]]
        if ohlc.isna().any().any():
            raise ValueError(f"[{series.symbol}] K线含 NaN OHLC，拒绝分析")
        freq_name = _FREQ_MAP.get(series.freq, "D")
        bars = format_standard_kline(df, freq=getattr(Freq, freq_name))
        c = CZSC(bars, min_bi_len=self.min_bi_len)
        return self._extract(c, series.freq)

    def analyze_multi(self, daily: BarSeries,
                      freqs: List[str]) -> Dict[str, ChanStructure]:
        """多周期分析：{freq: ChanStructure}。非日线用日K重采样（统一口径）。"""
        from .market import resample

        out: Dict[str, ChanStructure] = {}
        for freq in freqs:
            if freq == daily.freq:
                series = daily
            else:
                df = resample(_to_df(daily).rename(columns={"vol": "成交量",
                                                            "amount": "成交额",
                                                            "open": "开盘价",
                                                            "high": "最高价",
                                                            "low": "最低价",
                                                            "close": "收盘价",
                                                            "dt": "日期"}),
                              freq)
                bars = [Bar(dt=str(r["日期"])[:10], open=float(r["开盘价"]),
                            high=float(r["最高价"]), low=float(r["最低价"]),
                            close=float(r["收盘价"]), volume=float(r["成交量"]),
                            amount=float(r["成交额"]))
                        for _, r in df.iterrows()]
                series = BarSeries(symbol=daily.symbol, freq=freq,
                                   adjust=daily.adjust, bars=bars,
                                   source=f"{daily.source}:resample")
            out[freq] = self.analyze(series)
        return out

    # ---------------- 抽取 ----------------
    def _extract(self, c, freq: str) -> ChanStructure:
        """CZSC 对象 → ChanStructure（只取展示字段）。"""
        bis = []
        for bi in c.bi_list:
            bis.append(ChanBi(
                direction="up" if str(bi.direction) == "向上" else "down",
                sdt=str(bi.sdt)[:10],
                edt=str(bi.edt)[:10],
                high=float(bi.high),
                low=float(bi.low),
                confirmed=True,
            ))
        zss = []
        for zs in c.zs_list:
            zss.append(ChanZs(
                zg=float(zs.zg), zd=float(zs.zd), gg=float(zs.gg), dd=float(zs.dd),
                sdt=str(zs.sdt)[:10], edt=str(zs.edt)[:10],
                n_bi=len(getattr(zs, "bis", []) or []),
                finished=bool(getattr(zs, "is_valid", True)),
            ))
        finished_edts = {str(bi.edt)[:10] for bi in getattr(c, "finished_bis", [])}
        last_bi = bis[-1] if bis else None
        last_bi_confirmed = bool(last_bi and last_bi.edt in finished_edts)
        # 当前是否位于中枢内：最新K线落在最后一个中枢时间区间内
        in_zs = False
        if zss and c.bars_raw:
            last_dt = str(c.bars_raw[-1].dt)[:10]
            in_zs = zss[-1].sdt <= last_dt <= zss[-1].edt
        return ChanStructure(
            freq=freq,
            n_fx=len(getattr(c, "fx_list", []) or []),
            bis=bis,
            zss=zss,
            last_bi_dir=last_bi.direction if last_bi else "",
            last_bi_confirmed=last_bi_confirmed,
            in_zs=in_zs,
            engine="czsc",
            engine_ver=czsc_version(),
        )


def chan_from_dict(data: dict) -> ChanStructure:
    """ChanStructure.to_dict() → ChanStructure（chan_cache 反序列化）。"""
    if not data:
        return ChanStructure(freq="")
    bis = [ChanBi(direction=b["direction"], sdt=b["sdt"], edt=b["edt"],
                  high=b["high"], low=b["low"], confirmed=b.get("confirmed", True))
           for b in data.get("bis", [])]
    zss = [ChanZs(zg=z["zg"], zd=z["zd"], gg=z["gg"], dd=z["dd"],
                  sdt=z["sdt"], edt=z["edt"], n_bi=z.get("n_bi", 0),
                  finished=z.get("finished", False))
           for z in data.get("zss", [])]
    return ChanStructure(
        freq=data.get("freq", ""),
        n_fx=data.get("n_fx", 0),
        bis=bis,
        zss=zss,
        last_bi_dir=data.get("last_bi_dir", ""),
        last_bi_confirmed=data.get("last_bi_confirmed", False),
        in_zs=data.get("in_zs", False),
        engine=data.get("engine", "czsc"),
        engine_ver=data.get("engine_ver", ""),
    )
