"""mystery.adapters.market — 多源行情 + 缓存（统一出口 BarSeries）。

取数顺序：本地库未过期 → ths_official(MarketDB本地+fuyao) → tdx_api → tdx_local。
在线源（ths_official/tdx_api/tdx_local）命中后自动回写 SQLite 缓存，
保证"不论哪个入口更新过行情，后面不重复获取"。
指数：DB 优先（允许 3 天滞后，通达信本地未同步属正常）→ ths → tdx_local。
周/月：日 K 重采样（resample_engine: mystery，口径与旧仓 kline_resampler 一致）。
"""
from __future__ import annotations

import logging
import os
import threading
import time
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
        # 在线源命中计数（观测"是否还在重复在线拉取"）
        self.fetch_stats = {'db': 0, 'duckdb': 0, 'ths_official': 0,
                            'tdx_api': 0, 'tdx_local': 0, 'cache_write': 0}
        # _freshness_ref 会话级 memo（10s）：扫描逐股调用时避免重复查日历/探针
        self._ref_cache: Dict[str, Optional[str]] = {}
        self._ref_ts = 0.0
        self._ref_lock = threading.Lock()
        # 全源皆空负缓存（30min）：不存在的票/停牌无数据票，同进程不重复探测
        self._empty_cache: Dict[str, float] = {}
        self._EMPTY_TTL = 1800

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

        优先级：SQLite → DuckDB(v_daily_qfq) → ths_official(fuyao在线) → tdx_api → tdx_local。
        SQLite 过期时优先 DuckDB，避免走在线降级链。
        在线命中自动回写 SQLite（_cache_online_bars）。
        全源皆空的票进会话级负缓存（_empty_cache）：同进程不重复探测。
        """
        db_code = _codes.db_code_of(internal)

        # 负缓存命中：此前全源皆空，直接返回（避免每次扫描重复走整条降级链）
        now = time.time()
        with self._ref_lock:
            hit = self._empty_cache.get(internal)
            if hit and now - hit < self._EMPTY_TTL:
                return None, ''

        df = self.db.load_kline(db_code, 'daily', start, end)
        ref = self._freshness_ref(internal) if (df is not None and not df.empty) \
            else None

        # SQLite 未过期 → 直接用
        if df is not None and not df.empty:
            db_last = str(df['date'].max())[:10]
            if not (ref and db_last < ref):
                self.fetch_stats['db'] += 1
                return to_cn_columns(df), 'db'
            # SQLite 过期 → 优先 DuckDB（_fetch_from_duckdb 内部自动缓存到 SQLite）
            logger.info(f"[db→DuckDB] {internal} SQLite过期({db_last}<{ref})，尝试DuckDB")
            duckdb_df = self._fetch_from_duckdb(internal, start, end, cache_to_sqlite=True)
            if duckdb_df is not None and not duckdb_df.empty:
                return to_cn_columns(duckdb_df), 'db'
            logger.warning(f"[DuckDB→降级] {internal} DuckDB无数据，切 ths_official")

        # SQLite 为空 → 也先查 DuckDB（_fetch_from_duckdb 内部自动缓存到 SQLite）
        if df is None or df.empty:
            duckdb_df = self._fetch_from_duckdb(internal, start, end, cache_to_sqlite=True)
            if duckdb_df is not None and not duckdb_df.empty:
                return to_cn_columns(duckdb_df), 'db'
        
        # DuckDB 也没有 → 走在线（ths_official / tdx_api / tdx_local）
        # 在线命中后回写 SQLite 缓存：下次直接命中本地，不再重复在线拉取
        try:
            raw = self.ths.get_daily(internal, start, end)
            if raw is not None and not raw.empty:
                raw_last = str(raw['日期'].max())[:10]
                if not (ref and raw_last < ref):
                    self.fetch_stats['ths_official'] += 1
                    self._cache_online_bars(internal, raw, 'ths_official')
                    return raw, 'ths_official'
                logger.warning(
                    f"[ths_official] {internal} 返回数据落后({raw_last}<{ref})，"
                    f"继续尝试 tdx_api/tdx_local")
            else:
                logger.warning(f"[ths_official→降级] {internal} 返回空，切 tdx_api")
        except Exception as e:
            logger.warning(f"[ths_official→降级] {internal} 异常 {type(e).__name__}: {str(e)[:60]}，切 tdx_api")
        # 3. tdx_api（W2-A：本地 tdx-api 容器，带交易所前缀、价格×1000 还原）
        try:
            raw = self.tdx_api.get_daily(internal, start, end)
            if raw is not None and not raw.empty:
                self.fetch_stats['tdx_api'] += 1
                self._cache_online_bars(internal, raw, 'tdx_api')
                return raw, 'tdx_api'
            logger.warning(f"[tdx_api→降级] {internal} 返回空，切 tdx_local")
        except Exception as e:
            logger.warning(f"[tdx_api→降级] {internal} 异常 {type(e).__name__}: {str(e)[:60]}，切 tdx_local")
        # 4. tdx_local
        try:
            raw = self.tdx_local.get_daily(internal, start, end)
            if raw is not None and not raw.empty:
                self.fetch_stats['tdx_local'] += 1
                self._cache_online_bars(internal, raw, 'tdx_local')
                return raw, 'tdx_local'
            logger.warning(f"[tdx_local→降级] {internal} 返回空")
        except Exception as e:
            logger.warning(f"[tdx_local→降级] {internal} 异常 {type(e).__name__}: {str(e)[:60]}")
        # 全源皆空 → 负缓存（30min 内不再重复探测）
        with self._ref_lock:
            self._empty_cache[internal] = time.time()
        logger.info(f"[全源为空] {internal} 进入负缓存（30min）")
        return None, ''

    def _cache_online_bars(self, internal: str, raw: pd.DataFrame,
                           source: str) -> None:
        """在线源（ths_official/tdx_api/tdx_local）命中后回写 SQLite 缓存。

        upsert 幂等；turn/pctChg 用 COALESCE 不覆盖库内旧值。
        tdx_api 的 日期 列可能是 datetime——统一转 YYYY-MM-DD 字符串再写。
        失败只 debug，不影响本次返回。
        """
        self.fetch_stats[source] = self.fetch_stats.get(source, 0) + 1
        try:
            raw = raw.copy()
            if '日期' in raw.columns:
                raw['日期'] = pd.to_datetime(raw['日期']).dt.strftime('%Y-%m-%d')
            db_code = _codes.db_code_of(internal)
            self.db.upsert_kline(raw, db_code, 'daily')
            self.fetch_stats['cache_write'] += 1
            logger.info(f"[在线→SQLite] {internal}({source}) 缓存至 "
                        f"{str(raw['日期'].max())[:10]}")
        except Exception as e:
            logger.debug(f"[在线→SQLite] {internal} 回写失败: {str(e)[:80]}")

    def _fetch_from_duckdb(self, internal: str, start: Optional[str],
                           end: Optional[str], cache_to_sqlite: bool = True):
        """从 DuckDB (v_daily_qfq 前复权视图) 读取日K数据。

        返回英文列 DataFrame（date/open/high/low/close/volume/amount），失败返回 None。
        cache_to_sqlite=True 时自动写入 SQLite 缓存。
        """
        if not self.ths.marketdb_path or not os.path.exists(self.ths.marketdb_path):
            return None
        try:
            import duckdb
            from .codes import normalize_symbol, db_code_of
            # internal: 600519.SH → DuckDB thscode 也是 600519.SH
            thscode = normalize_symbol(internal)
            conn = duckdb.connect(self.ths.marketdb_path, read_only=True)
            try:
                sql = ("SELECT thscode, date, open, high, low, close, volume, turnover "
                       "FROM v_daily_qfq WHERE thscode = ?")
                params = [thscode]
                if start:
                    sql += " AND date >= ?"
                    params.append(start)
                if end:
                    sql += " AND date <= ?"
                    params.append(end)
                sql += " ORDER BY date ASC"
                df = conn.execute(sql, params).fetchdf()
                if df is not None and not df.empty:
                    self.fetch_stats['duckdb'] += 1
                    # 重命名列：turnover → amount，保留英文列
                    df = df.rename(columns={'turnover': 'amount'})
                    # 只保留需要的列
                    keep_cols = ['date', 'open', 'high', 'low', 'close', 'volume', 'amount']
                    result = df[[c for c in keep_cols if c in df.columns]]
                    
                    # 自动写入 SQLite 缓存
                    if cache_to_sqlite:
                        try:
                            db_code = db_code_of(internal)
                            self.db.upsert_kline(result, db_code, 'daily')
                            dk_last = str(result['date'].max())[:10]
                            logger.info(f"[DuckDB→SQLite] {internal} 缓存到 {dk_last}")
                        except Exception as e:
                            logger.debug(f"[DuckDB→SQLite] 写入失败: {str(e)[:60]}")
                    
                    return result
            finally:
                conn.close()
        except Exception as e:
            logger.debug(f"[DuckDB] {internal} 读取失败: {str(e)[:80]}")
        return None

    def _freshness_ref(self, internal: str) -> Optional[str]:
        """新鲜度参照：在线交易日历最新交易日 → MarketDB → .day 文件。

        会话级 memo（10s）：全市场扫描逐股调用时只查一次，避免重复日历/探针开销。
        """
        now = time.time()
        with self._ref_lock:
            if self._ref_ts and now - self._ref_ts < 10 and self._ref_cache:
                return self._ref_cache.get(internal, self._ref_cache.get('__global__'))
        try:
            from .calendar import get_latest_trade_date
            latest = get_latest_trade_date()
        except Exception as e:
            logger.debug(f"交易日历获取失败: {str(e)[:60]}")
            latest = None
        if latest is None:
            latest = self.ths.probe_last_date(internal) \
                or self.tdx_local.last_date_of(internal)
        with self._ref_lock:
            self._ref_cache = {'__global__': latest}
            self._ref_ts = now
        return latest

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
                # 指数也落库（含 resample 用的日K基底），后续直接命中本地
                self._cache_online_bars(internal, raw, 'ths_official')
                return _df_to_series(raw, internal, freq, self.adjust, 'ths_official')
        except Exception as e:
            logger.debug(f"[ths] 指数 {internal} 失败: {str(e)[:60]}")
        try:
            raw = self.tdx_local.get_daily(internal, start, end)
            if raw is not None and not raw.empty:
                self._cache_online_bars(internal, raw, 'tdx_local')
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
    """中文列 DataFrame → BarSeries（容忍 日期/收盘价 列缺失）。

    列一次性提取为 Python list 再 zip 组装（比 iterrows 快约 10 倍——
    全市场扫描时这里曾是最大单体开销，实测 0.67s/只 → ~0.03s/只）。
    """
    dt_col = '日期' if '日期' in df.columns else ('date' if 'date' in df.columns else None)
    c_col = '收盘价' if '收盘价' in df.columns else ('close' if 'close' in df.columns else None)
    if dt_col is None or c_col is None:
        return BarSeries(symbol=symbol, freq=freq, adjust=adjust, source=source)

    def _col(cn: str, en: str) -> List:
        for c in (cn, en):
            if c in df.columns:
                return df[c].tolist()
        return [None] * len(df)

    dts = [str(d)[:10] for d in df[dt_col].tolist()]
    closes = df[c_col].tolist()
    opens = _col('开盘价', 'open')
    highs = _col('最高价', 'high')
    lows = _col('最低价', 'low')
    volumes = _col('成交量', 'volume')
    amounts = _col('成交额', 'amount')
    turnovers = _col('换手率', 'turn')
    pcts = _col('涨跌幅', 'pctChg')
    bars = [Bar(dt=d, open=_num(o), high=_num(h), low=_num(l), close=_num(cl),
                volume=_num(v), amount=_num(a), turnover=_num(t), pct_chg=_num(p))
            for d, o, h, l, cl, v, a, t, p in
            zip(dts, opens, highs, lows, closes, volumes, amounts, turnovers, pcts)]
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
