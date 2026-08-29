"""mystery.adapters.czsc_adapter — 唯一允许 import czsc 的分析适配。

把 BarSeries → CZSC 对象 → ChanStructure（core 模型），不向 core/web 泄漏
CZSC / BI / ZS / RawBar。读 CZSC_MIN_BI_LEN 环境变量，不改 czsc 源码。
"""
from __future__ import annotations

import importlib.metadata
import json
import logging
import os
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from ..core.models import Bar, BarSeries, ChanBi, ChanStructure, ChanZs

logger = logging.getLogger(__name__)

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


# ---------------- W4 缠论图（plotly 自绘） ----------------
# 默认均线周期：MA5 / 10 / 20 / 55 / 233 / 610（Mistery 常用六线）。
_MA_PERIODS = (5, 10, 20, 55, 233, 610)

_FREQ_LABEL = {"1d": "日线", "1w": "周线", "1M": "月线"}

# 涨红跌绿（与 czsc 历史 Plotly/lightweight 配色一致）
_THEME_COLORS = {
    "light": {
        "bg": "#FBF9F4", "text": "#1A1A17", "grid": "#E8E2D4",
        "up": "#C03A2B", "down": "#2E7D32",
        "ma": ["#C78A2E", "#2D6A8C", "#7B4FA8", "#0C7B93", "#A52A2A", "#5B7C0C"],
        "fx": "#8B7E5E", "bi": "#1F3C6E",
        "zs_fill": "rgba(31,60,110,0.14)", "zs_edge": "#1F3C6E",
        "zs_outer": "rgba(139,126,94,0.55)",
        "macd_diff": "#1F3C6E", "macd_dea": "#C78A2E",
    },
    "dark": {
        "bg": "#0E1116", "text": "#EFEBE0", "grid": "#1F242E",
        "up": "#E94B3C", "down": "#5BB85B",
        "ma": ["#E6A93B", "#6EB6E4", "#C29CF2", "#6FCFE0", "#E08989", "#B9D560"],
        "fx": "#8B8678", "bi": "#A8B8E8",
        "zs_fill": "rgba(168,184,232,0.16)", "zs_edge": "#A8B8E8",
        "zs_outer": "rgba(139,134,120,0.5)",
        "macd_diff": "#A8B8E8", "macd_dea": "#E6A93B",
    },
}


def _ema_czsc(close: np.ndarray, period: int) -> np.ndarray:
    """czsc 口径 EMA：以首值作种子直接递推（与 czsc 仪表盘 MACD 一致）。"""
    res = [0.0] * len(close)
    for i in range(len(close)):
        if i < 1:
            res[i] = float(close[i])
        else:
            res[i] = (2 * float(close[i]) + res[i - 1] * (period - 1)) / (period + 1)
    return np.asarray(res)


def _macd_czsc(close: np.ndarray, fast: int = 12, slow: int = 26,
               signal: int = 9):
    """返回 (diff, dea, macd)，macd = (diff - dea) * 2（czsc ×2 约定）。"""
    diff = _ema_czsc(close, fast) - _ema_czsc(close, slow)
    dea = _ema_czsc(diff, signal)
    return diff, dea, (diff - dea) * 2


def _build_chan_figure(c, series: BarSeries, tail_bars: Optional[int],
                       theme: str, ma_periods: tuple):
    """CZSC 对象 → plotly Figure（只在本 adapter 内持有 czsc 对象）。

    主图：K线 + MA(5/10/20/55/233/610) + 分型虚线 + 笔实线 + 中枢矩形
    （ZG-ZD 填充实线框、GG-DD 虚线外框）；
    副图1：成交量；副图2：MACD(12,26,9,×2)。
    MA/MACD 在全集 K 线上计算再截取尾窗，保证长周期均线有暖机值。
    """
    import plotly.graph_objects as go  # noqa: PLC0415
    from plotly.subplots import make_subplots  # noqa: PLC0415

    colors = _THEME_COLORS.get(theme, _THEME_COLORS["light"])
    bars_all = list(c.bars_raw)
    if not bars_all:
        return None

    # 尾窗：按时间截断（bars_raw 升序）
    cutoff = None
    if tail_bars and 0 < tail_bars < len(bars_all):
        cutoff = bars_all[-tail_bars].dt
    start_idx = 0 if cutoff is None else next(
        i for i, b in enumerate(bars_all) if b.dt >= cutoff)
    bars = bars_all[start_idx:]
    times = [b.dt for b in bars]
    closes = np.asarray([float(b.close) for b in bars_all], dtype=float)

    fig = make_subplots(rows=3, cols=1, shared_xaxes=True,
                        vertical_spacing=0.03, row_heights=[0.62, 0.13, 0.25],
                        subplot_titles=("", "成交量", "MACD(12,26,9)"))

    fig.add_trace(go.Candlestick(
        x=times,
        open=[float(b.open) for b in bars],
        high=[float(b.high) for b in bars],
        low=[float(b.low) for b in bars],
        close=[float(b.close) for b in bars],
        name="K线", increasing_line_color=colors["up"],
        increasing_fillcolor=colors["up"], decreasing_line_color=colors["down"],
        decreasing_fillcolor=colors["down"],
    ), row=1, col=1)

    # MA（全集滚动均值 → 截窗）
    ma_series = pd.Series(closes).rolling
    for n, color in zip(ma_periods, colors["ma"]):
        vals = ma_series(n).mean().to_numpy()
        fig.add_trace(go.Scatter(
            x=times,
            y=[None if pd.isna(vals[i]) else float(vals[i])
               for i in range(start_idx, len(bars_all))],
            name=f"MA{n}", mode="lines", line=dict(color=color, width=1.1),
            hovertemplate=f"MA{n} %{{y:.2f}}<extra></extra>",
        ), row=1, col=1)

    # 分型虚线（同 time 去重保首）
    fx_pts = [(fx.dt, float(fx.fx)) for fx in c.fx_list
              if cutoff is None or fx.dt >= cutoff]
    fx_pts.sort(key=lambda p: p[0])
    seen: set = set()
    fx_dedup = []
    for t, v in fx_pts:
        if t not in seen:
            seen.add(t)
            fx_dedup.append((t, v))
    if fx_dedup:
        fig.add_trace(go.Scatter(
            x=[p[0] for p in fx_dedup], y=[p[1] for p in fx_dedup],
            name="分型", mode="lines", line=dict(color=colors["fx"], width=1,
                                                dash="dot"),
            hovertemplate="分型 %{y:.2f}<extra></extra>",
        ), row=1, col=1)

    # 笔实线（端点折线，同 time 去重保后）
    bi_pts: List = []
    for bi in c.bi_list:
        if cutoff is None or bi.fx_b.dt >= cutoff:
            bi_pts.append((bi.fx_a.dt, float(bi.fx_a.fx)))
            bi_pts.append((bi.fx_b.dt, float(bi.fx_b.fx)))
    bi_dedup = {}
    for t, v in bi_pts:
        bi_dedup[t] = v
    bi_line = sorted(bi_dedup.items(), key=lambda p: p[0])
    if bi_line:
        fig.add_trace(go.Scatter(
            x=[p[0] for p in bi_line], y=[p[1] for p in bi_line],
            name="笔", mode="lines", line=dict(color=colors["bi"], width=1.6),
            hovertemplate="笔 %{y:.2f}<extra></extra>",
        ), row=1, col=1)

    # 中枢区间：ZG-ZD 填充框 + GG-DD 虚线外框 + 标注
    for zs in c.zs_list:
        if cutoff is not None and str(zs.edt)[:10] < str(cutoff)[:10]:
            continue
        x0, x1 = pd.to_datetime(zs.sdt), pd.to_datetime(zs.edt)
        fig.add_shape(type="rect", x0=x0, x1=x1, y0=float(zs.zd),
                      y1=float(zs.zg), fillcolor=colors["zs_fill"],
                      line=dict(color=colors["zs_edge"], width=1), row=1, col=1)
        fig.add_shape(type="rect", x0=x0, x1=x1, y0=float(zs.dd),
                      y1=float(zs.gg), fillcolor="rgba(0,0,0,0)",
                      line=dict(color=colors["zs_outer"], width=1, dash="dot"),
                      row=1, col=1)
        fig.add_annotation(x=x0, y=float(zs.zg),
                           text=f"中枢 {len(getattr(zs, 'bis', []) or [])}笔",
                           showarrow=False,
                           font=dict(size=10, color=colors["zs_edge"]),
                           xanchor="left", yanchor="bottom", row=1, col=1)

    # 成交量
    vol_colors = [colors["up"] if float(b.close) >= float(b.open)
                  else colors["down"] for b in bars]
    fig.add_trace(go.Bar(x=times, y=[float(b.vol) for b in bars],
                         name="成交量", marker_color=vol_colors, opacity=0.75,
                         hovertemplate="量 %{y:,.0f}<extra></extra>"),
                  row=2, col=1)

    # MACD（全集计算 → 截窗；柱 ×2，涨红跌绿）
    diff, dea, macd = _macd_czsc(closes)
    sl = slice(start_idx, len(bars_all))
    fig.add_trace(go.Scatter(x=times, y=[float(v) for v in diff[sl]],
                             name="DIFF", line=dict(color=colors["macd_diff"],
                                                    width=1),
                             hovertemplate="DIFF %{y:.3f}<extra></extra>"),
                  row=3, col=1)
    fig.add_trace(go.Scatter(x=times, y=[float(v) for v in dea[sl]],
                             name="DEA", line=dict(color=colors["macd_dea"],
                                                    width=1),
                             hovertemplate="DEA %{y:.3f}<extra></extra>"),
                  row=3, col=1)
    macd_vals = [float(v) for v in macd[sl]]
    fig.add_trace(go.Bar(
        x=times, y=macd_vals, name="MACD",
        marker_color=[colors["up"] if v >= 0 else colors["down"]
                      for v in macd_vals],
        hovertemplate="MACD %{y:.3f}<extra></extra>"), row=3, col=1)

    freq_label = _FREQ_LABEL.get(series.freq, series.freq)
    fig.update_layout(
        title=f"{series.symbol} 缠论结构（{freq_label}）",
        height=720, margin=dict(l=40, r=20, t=64, b=24),
        paper_bgcolor=colors["bg"], plot_bgcolor=colors["bg"],
        font=dict(color=colors["text"], size=12),
        legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0),
        xaxis_rangeslider_visible=False, hovermode="x unified",
    )
    for r in (1, 2, 3):
        fig.update_xaxes(type="date", showgrid=True, gridcolor=colors["grid"],
                         row=r, col=1)
        fig.update_yaxes(gridcolor=colors["grid"],
                         zerolinecolor=colors["grid"], row=r, col=1)
    return fig


class CzscAdapter:
    """缠论结构适配器。"""

    def __init__(self, min_bi_len: Optional[int] = None):
        self.min_bi_len = min_bi_len or _CZSC_MIN_BI_LEN

    def analyze(self, series: BarSeries) -> ChanStructure:
        """单周期分析：BarSeries → ChanStructure（czsc 唯一入口）。

        czsc 未安装时返回空结构 + engine_ver='unavailable'（不抛到顶层）。
        """
        try:
            from czsc import CZSC, Freq, format_standard_kline
        except ImportError:
            logger.warning("czsc 未安装（pip install -e '.[chan]'），缠论结构不可用")
            return ChanStructure(freq=series.freq, engine="czsc",
                                 engine_ver="unavailable")
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

    def _build_czsc(self, series: BarSeries):
        """BarSeries → CZSC 对象（失败返回 None；CZSC 只留在本 adapter 内）。"""
        try:
            from czsc import CZSC, Freq, format_standard_kline
        except ImportError:
            logger.warning("czsc 未安装，缠论图不可用（pip install -e '.[chan]'）")
            return None
        if not series.bars:
            return None
        df = _to_df(series)
        freq_name = _FREQ_MAP.get(series.freq, "D")
        try:
            bars = format_standard_kline(df, freq=getattr(Freq, freq_name))
            return CZSC(bars, min_bi_len=self.min_bi_len)
        except Exception as e:
            logger.warning(f"CZSC 构建失败({series.symbol}): {str(e)[:100]}")
            return None

    def plot_figure(self, series: BarSeries, tail_bars: Optional[int] = 400,
                    theme: str = "light",
                    ma_periods: tuple = _MA_PERIODS):
        """BarSeries → plotly Figure（K线 + MA + 分型/笔 + 中枢区间 + 量 + MACD）。

        W4 起自绘：czsc 的 ``plot_czsc`` 主图只画 SMA5/20 且不画中枢区间，
        无法满足 MA 5/10/20/55/233/610 与中枢矩形需求，故改用 plotly 直接渲染
        （涨红跌绿，与 czsc 历史配色一致）。画图所需 CZSC 对象只留在 adapter 内。
        失败（czsc 未装 / 数据不足 / 构建异常）→ None，调用方降级文本。
        """
        c = self._build_czsc(series)
        if c is None:
            return None
        try:
            return _build_chan_figure(c, series, tail_bars=tail_bars,
                                      theme=theme,
                                      ma_periods=tuple(ma_periods))
        except Exception as e:
            logger.warning(f"缠论图构建失败({series.symbol}): {str(e)[:100]}")
            return None

    def plot_lightweight_html(self, series: BarSeries,
                              tail_bars: Optional[int] = None,
                              theme: str = "light") -> str:
        """官方 lightweight 校验图（czsc plot_czsc(c, output='html')）。失败返回 ''。"""
        c = self._build_czsc(series)
        if c is None:
            return ""
        try:
            from czsc.utils.plotting.lightweight import plot_czsc
            html = plot_czsc(c, output="html", theme=theme, tail_bars=tail_bars)
            return html or ""
        except Exception as e:
            logger.warning(f"lightweight 校验图失败({series.symbol}): {str(e)[:100]}")
            return ""

    def plot_html(self, series: BarSeries, tail_bars: Optional[int] = 400,
                  theme: str = "light",
                  ma_periods: tuple = _MA_PERIODS) -> str:
        """自包含 HTML（主图=plotly 中枢盒 → 失败降级 lightweight → 失败 ''）。"""
        fig = self.plot_figure(series, tail_bars=tail_bars, theme=theme,
                               ma_periods=ma_periods)
        if fig is not None:
            try:
                return fig.to_html(full_html=True, include_plotlyjs="cdn")
            except Exception as e:
                logger.warning(f"plotly HTML 输出失败: {str(e)[:80]}")
        return self.plot_lightweight_html(series, tail_bars=tail_bars, theme=theme)

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
