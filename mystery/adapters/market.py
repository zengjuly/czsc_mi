"""mystery.adapters.market — 多源行情 + 缓存（统一出口 BarSeries）。

取数顺序：本地库未过期 → ths_official(MarketDB本地+fuyao) → tdx_api → tdx_local。
指数：DB 优先（允许 3 天滞后，通达信本地未同步属正常）→ ths → tdx_local。
周/月：日 K 重采样（resample_engine: mystery，口径与旧仓 kline_resampler 一致）。
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

import pandas as pd

from ..core.models import Bar, BarSeries
from . import codes as _codes
from .tdx_api import TdxApiClient
from .tdx_local import TdxLocalClient
from .ths import ThsClient
from ..store.db import MysteryDB, to_cn_columns

logger = logging.getLogger(__name__)

_PERIOD = {'1d': 'daily', '1w': 'weekly', '1M': 'monthly'}

_AGG = {'开盘价': 'first', '最高价': 'max', '最低价': 'min', '收盘价': 'last',
        '成交量': 'sum', '成交额': 'sum', '换手率': 'sum'}
_RULE = {'1w': 'W-FRI', '1M': 'ME'}
_MIN_BARS = {'1w': 3, '1M': 10}


class MarketDataClient:
    """多源行情客户端（统一出口 BarSeries）。"""

    def __init__(self, cfg: Optional[Dict] = None):
        self.cfg = cfg or {}
        ds = self.cfg.get('data_source') or {}
        self.adjust = _codes.normalize_adjust(ds.get('adjust', 'qfq'))
        self.db = MysteryDB(db_path=self.cfg.get('db_path') or None)
        self.ths = ThsClient(self.cfg)
        self.tdx_api = TdxApiClient(self.cfg)
        self.tdx_local = TdxLocalClient(self.cfg)

    # ---------------- 主入口 ----------------
    def fetch_bars(self, symbol: str, freq: str = "1d",
                   start: Optional[str] = None,
                   end: Optional[str] = None) -> BarSeries:
        """统一出口：本地库未过期 → ths_official → tdx_api → tdx_local。"""
        internal = _codes.normalize_symbol(symbol)
        freq = _codes.normalize_freq(freq)
        # 周/月：日 K 重采样（一期统一口径，与旧仓 prefer_resample=true 一致）
        if freq != '1d':
            daily = self.fetch_bars(internal, '1d', start, end)
            if not daily.bars:
                return BarSeries(symbol=internal, freq=freq, adjust=self.adjust, source='')
            df = resample(self.to_df(daily), freq)
            return _df_to_series(df, internal, freq, self.adjust,
                                 f"{daily.source}:resample")
        df, source = self._fetch_daily(internal, start, end)
        if df is None or df.empty:
            return BarSeries(symbol=internal, freq=freq, adjust=self.adjust, source='')
        df = _slice(df, start, end)
        return _df_to_series(df, internal, freq, self.adjust, source)

    def _fetch_daily(self, internal: str, start: Optional[str],
                     end: Optional[str]):
        """日K多源退避（返回 (df, source)）。

        新鲜度参照优先级：MarketDB 本地最新交易日 → .day 文件最新日期。
        DB 未过期 → 直接用；过期 → ths_official(MarketDB→fuyao) → tdx_local。
        """
        db_code = _codes.db_code_of(internal)
        df = self.db.load_kline(db_code, 'daily', start, end)
        if df is not None and not df.empty:
            db_last = str(df['date'].max())[:10]
            ref = self._freshness_ref(internal)
            if not (ref and db_last < ref):
                return to_cn_columns(df), 'db'
            logger.warning(f"[db→降级] {internal} 本地库过期({db_last}<最新{ref})，切 ths_official")
        # 2. ths_official（MarketDB 本地秒读 → fuyao 兜底）
        try:
            raw = self.ths.get_daily(internal, start, end)
            if raw is not None and not raw.empty:
                return raw, 'ths_official'
            logger.warning(f"[ths_official→降级] {internal} 返回空，切 tdx_api")
        except Exception as e:
            logger.warning(f"[ths_official→降级] {internal} 异常 {type(e).__name__}: {str(e)[:60]}，切 tdx_api")
        # 3. tdx_api（W2-A：本地 tdx-api 容器，带交易所前缀、价格×1000 还原）
        try:
            raw = self.tdx_api.get_daily(internal, start, end)
            if raw is not None and not raw.empty:
                return raw, 'tdx_api'
            logger.warning(f"[tdx_api→降级] {internal} 返回空，切 tdx_local")
        except Exception as e:
            logger.warning(f"[tdx_api→降级] {internal} 异常 {type(e).__name__}: {str(e)[:60]}，切 tdx_local")
        # 4. tdx_local
        try:
            raw = self.tdx_local.get_daily(internal, start, end)
            if raw is not None and not raw.empty:
                return raw, 'tdx_local'
            logger.warning(f"[tdx_local→降级] {internal} 返回空")
        except Exception as e:
            logger.warning(f"[tdx_local→降级] {internal} 异常 {type(e).__name__}: {str(e)[:60]}")
        return None, ''

    def _freshness_ref(self, internal: str) -> Optional[str]:
        """新鲜度参照：在线交易日历最新交易日 → MarketDB → .day 文件。"""
        try:
            from .calendar import get_latest_trade_date
            latest = get_latest_trade_date()
            if latest:
                return latest
        except Exception as e:
            logger.debug(f"交易日历获取失败: {str(e)[:60]}")
        return self.ths.probe_last_date(internal) \
            or self.tdx_local.last_date_of(internal)

    def fetch_index(self, code: str, freq: str = "1d",
                    start: Optional[str] = None,
                    end: Optional[str] = None) -> BarSeries:
        """指数：DB 优先（允许 3 天滞后）→ ths → tdx_local。"""
        internal = _codes.normalize_symbol(code)
        freq = _codes.normalize_freq(freq)
        if freq != '1d':
            daily = self.fetch_index(internal, '1d', start, end)
            if not daily.bars:
                return BarSeries(symbol=internal, freq=freq, adjust=self.adjust, source='')
            df = resample(self.to_df(daily), freq)
            return _df_to_series(df, internal, freq, self.adjust,
                                 f"{daily.source}:resample")
        db_code = _codes.db_code_of(internal)
        df = self.db.load_kline(db_code, 'daily', start, end)
        source = 'db'
        if df is not None and not df.empty:
            db_last = str(df['date'].max())[:10]
            ref = self._freshness_ref(internal)
            # 指数豁免严格新鲜度：本地文件滞后 1-3 天属正常（通达信未同步）
            lag_ok = True
            if ref:
                try:
                    from datetime import datetime, timedelta
                    thr = datetime.strptime(ref, '%Y-%m-%d') - timedelta(days=3)
                    lag_ok = db_last >= thr.strftime('%Y-%m-%d')
                except Exception:
                    lag_ok = True
            if lag_ok:
                return _df_to_series(to_cn_columns(df), internal, freq,
                                     self.adjust, source)
        try:
            raw = self.ths.get_daily(internal, start, end)
            if raw is not None and not raw.empty:
                return _df_to_series(raw, internal, freq, self.adjust, 'ths_official')
        except Exception as e:
            logger.debug(f"[ths] 指数 {internal} 失败: {str(e)[:60]}")
        try:
            raw = self.tdx_local.get_daily(internal, start, end)
            if raw is not None and not raw.empty:
                return _df_to_series(raw, internal, freq, self.adjust, 'tdx_local')
        except Exception as e:
            logger.debug(f"[tdx_local] 指数 {internal} 失败: {str(e)[:60]}")
        if df is not None and not df.empty:
            return _df_to_series(to_cn_columns(df), internal, freq, self.adjust, 'db')
        return BarSeries(symbol=internal, freq=freq, adjust=self.adjust, source='')

    def fetch_stock_list(self) -> List[Dict]:
        """[{code, name}]，ths_official 优先，本地缓存兜底。"""
        try:
            lst = self.ths.get_stock_list()
            if lst:
                return lst
        except Exception as e:
            logger.debug(f"[ths] 证券列表失败，走本地库: {str(e)[:60]}")
        return self.db.get_stock_list()

    def to_df(self, series: BarSeries) -> pd.DataFrame:
        """BarSeries → DataFrame（中文列，供 core 规则消费）。"""
        rows = [{'日期': str(b.dt)[:10], '开盘价': b.open, '最高价': b.high,
                 '最低价': b.low, '收盘价': b.close, '成交量': b.volume,
                 '成交额': b.amount, '换手率': b.turnover, '涨跌幅': b.pct_chg}
                for b in series.bars]
        return pd.DataFrame(rows)


def _slice(df: pd.DataFrame, start_date: Optional[str],
           end_date: Optional[str]) -> pd.DataFrame:
    if start_date:
        df = df[df['日期'].astype(str) >= str(start_date)]
    if end_date:
        df = df[df['日期'].astype(str) <= str(end_date)]
    return df.reset_index(drop=True)


def _df_to_series(df: pd.DataFrame, symbol: str, freq: str, adjust: str,
                  source: str) -> BarSeries:
    """中文列 DataFrame → BarSeries（容忍 日期/收盘价 列缺失）。"""
    dt_col = '日期' if '日期' in df.columns else ('date' if 'date' in df.columns else None)
    c_col = '收盘价' if '收盘价' in df.columns else ('close' if 'close' in df.columns else None)
    if dt_col is None or c_col is None:
        return BarSeries(symbol=symbol, freq=freq, adjust=adjust, source=source)
    bars = []
    for _, r in df.iterrows():
        bars.append(Bar(
            dt=str(r[dt_col])[:10],
            open=_num(r.get('开盘价', r.get('open'))),
            high=_num(r.get('最高价', r.get('high'))),
            low=_num(r.get('最低价', r.get('low'))),
            close=_num(r[c_col]),
            volume=_num(r.get('成交量', r.get('volume'))),
            amount=_num(r.get('成交额', r.get('amount'))),
            turnover=_num(r.get('换手率', r.get('turn'))),
            pct_chg=_num(r.get('涨跌幅', r.get('pctChg'))),
        ))
    return BarSeries(symbol=symbol, freq=freq, adjust=adjust, bars=bars, source=source)


def _num(v) -> float:
    try:
        f = float(v)
        return 0.0 if pd.isna(f) else f
    except Exception:
        return 0.0


def resample(daily: pd.DataFrame, freq: str) -> pd.DataFrame:
    """日 K（中文列）→ 周/月 K（口径与旧仓 kline_resampler 一致）。"""
    if daily is None or daily.empty:
        return daily
    df = daily.copy()
    df['日期'] = pd.to_datetime(df['日期'])
    df = df.sort_values('日期').drop_duplicates(subset=['日期'], keep='last')
    # 无交易日历 → 剔除周末（旧仓无日历时的兜底）
    df = df[df['日期'].dt.dayofweek < 5]
    for col in ['开盘价', '最高价', '最低价', '收盘价', '成交量', '成交额', '换手率']:
        if col not in df.columns:
            df[col] = None
    counts = df.set_index('日期').resample(_RULE[freq]).size()
    resampled = df.set_index('日期').resample(_RULE[freq]).agg(_AGG)
    resampled = resampled.dropna(subset=['收盘价'])
    keep = counts >= _MIN_BARS[freq]
    keep = keep.reindex(resampled.index, fill_value=False)
    if len(keep) > 0:
        keep.iloc[-1] = True  # 进行中的最新周期必须保留
    resampled = resampled[keep]
    resampled['涨跌幅'] = resampled['收盘价'].pct_change() * 100
    out = resampled.reset_index()
    out['日期'] = out['日期'].dt.strftime('%Y-%m-%d')
    return out
