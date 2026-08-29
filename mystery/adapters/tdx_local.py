"""mystery.adapters.tdx_local — 通达信本地数据（.day 文件，自研 struct 解析）。

迁自 tdx_incremental.py + tdx_local_client.py（指数豁免换手率/落后检查）。
行情目录优先级：TDX_HOME(/mnt/new_tdx) → TDX_VIPDOC_DIR。
换手率从 SQLite 缓存补齐（无则 None，与旧仓一致）。
"""
from __future__ import annotations

import logging
import os
import struct
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

DAY_RECORD_SIZE = 32
DAY_RECORD_FMT = '<IIIIIfII'

STANDARD_COLS = ['日期', '代码', '开盘价', '最高价', '最低价', '收盘价',
                 '成交量', '成交额', '换手率', '涨跌幅']


def resolve_kline_dirs() -> List[str]:
    """行情目录优先级：TDX_HOME/vipdoc → TDX_VIPDOC_DIR（本机路径由环境变量注入）。"""
    dirs: List[str] = []
    home = os.environ.get('TDX_HOME')
    if home:
        vip = os.path.join(home, 'vipdoc')
        if os.path.isdir(vip) and _has_lday(vip):
            dirs.append(vip)
    extra = os.environ.get('TDX_VIPDOC_DIR', '')
    if extra and os.path.isdir(extra) and _has_lday(extra) and extra not in dirs:
        dirs.append(extra)
    return dirs


def _has_lday(vipdoc: str) -> bool:
    return any(os.path.isdir(os.path.join(vipdoc, m, 'lday'))
               for m in ('sh', 'sz', 'bj'))


def get_market(code6: str) -> str:
    if code6.startswith(('6', '9', '5')):
        return 'sh'
    if code6.startswith(('0', '2', '3')):
        return 'sz'
    if code6.startswith(('4', '8')):
        return 'bj'
    return 'sh'


class TdxLocalClient:
    """通达信本地客户端。"""

    def __init__(self, cfg: Optional[Dict] = None):
        self.cfg = cfg or {}
        self.kline_dirs = resolve_kline_dirs()
        self.vipdoc_dir = self.kline_dirs[0] if self.kline_dirs else None

    def _normalize_code(self, stock_code: str) -> str:
        code = str(stock_code).strip().lower()
        for prefix in ['sh.', 'sz.', 'bj.', 'sh', 'sz', 'bj']:
            code = code.replace(prefix, '')
        digits = ''.join(ch for ch in code if ch.isdigit())
        return digits.zfill(6)[-6:]

    def _day_file_path(self, code6: str) -> Optional[str]:
        mkt = get_market(code6)
        for d in self.kline_dirs:
            std = os.path.join(d, mkt, 'lday', f'{mkt}{code6}.day')
            if os.path.exists(std):
                return std
        return None

    def last_date_of(self, code: str) -> Optional[str]:
        """.day 文件最后交易日（用于新鲜度对比，读最后一条记录）。"""
        code6 = self._normalize_code(code)
        fp = self._day_file_path(code6)
        if not fp or os.path.getsize(fp) < DAY_RECORD_SIZE:
            return None
        try:
            with open(fp, 'rb') as f:
                f.seek(-DAY_RECORD_SIZE, 2)
                date_int, = struct.unpack('<I', f.read(4))
            s = str(date_int)
            return f'{s[:4]}-{s[4:6]}-{s[6:8]}' if len(s) == 8 else None
        except Exception:
            return None

    def _read_day_file(self, code6: str) -> pd.DataFrame:
        """读全量 .day（标准中文列；换手率 None，涨跌幅 pct_change）。"""
        fp = self._day_file_path(code6)
        if not fp:
            return pd.DataFrame()
        records = []
        try:
            with open(fp, 'rb') as f:
                while True:
                    chunk = f.read(DAY_RECORD_SIZE)
                    if not chunk or len(chunk) < DAY_RECORD_SIZE:
                        break
                    date, open_, high, low, close, amount, volume, _ = \
                        struct.unpack(DAY_RECORD_FMT, chunk)
                    date_str = str(date)
                    if date_str == '0' or date_str < '19900101':
                        continue
                    records.append({
                        '日期': f'{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}',
                        '代码': code6,
                        '开盘价': open_ / 100.0,
                        '最高价': high / 100.0,
                        '最低价': low / 100.0,
                        '收盘价': close / 100.0,
                        '成交量': volume / 100.0,   # 股 → 手
                        '成交额': amount,
                        '换手率': None,
                        '涨跌幅': None,
                    })
        except Exception as e:
            logger.error(f"❌ 读取 {fp} 失败: {e}")
            return pd.DataFrame()
        if not records:
            return pd.DataFrame()
        df = pd.DataFrame(records)
        df['日期'] = pd.to_datetime(df['日期'])
        df = df.sort_values('日期')
        df['涨跌幅'] = df['收盘价'].pct_change() * 100
        return df

    def _fill_turnover(self, df: pd.DataFrame, code6: str) -> pd.DataFrame:
        """换手率从 SQLite 缓存补齐（.day 无此字段），未覆盖最新行 ffill。"""
        if df.empty or df['换手率'].notna().any():
            return df
        try:
            from ..store.db import MysteryDB
            db = MysteryDB(db_path=self.cfg.get('db_path') or None)
            mkt = get_market(code6)
            cached = db.load_kline(f'{mkt}.{code6}', 'daily')
            if not cached.empty and 'turn' in cached.columns:
                turn_map = dict(zip(cached['date'], cached['turn']))
                df['换手率'] = df['日期'].dt.strftime('%Y-%m-%d').map(turn_map)
                df['换手率'] = df['换手率'].ffill()
        except Exception as e:
            logger.debug(f"换手率补齐失败 {code6}: {str(e)[:60]}")
        return df

    def get_daily(self, symbol: str, start: Optional[str] = None,
                  end: Optional[str] = None,
                  is_index: bool = False) -> Optional[pd.DataFrame]:
        """读本地日K（中文列，升序）。"""
        code6 = self._normalize_code(symbol)
        df = self._read_day_file(code6)
        if df is None or df.empty:
            return pd.DataFrame(columns=STANDARD_COLS)
        df = self._fill_turnover(df, code6)
        df['日期'] = df['日期'].dt.strftime('%Y-%m-%d')
        if start:
            df = df[df['日期'] >= str(start)]
        if end:
            df = df[df['日期'] <= str(end)]
        df = df.sort_values('日期').reset_index(drop=True)
        for col in STANDARD_COLS:
            if col not in df.columns:
                df[col] = None
        return df[STANDARD_COLS].copy()

    def get_index(self, code: str, start: Optional[str] = None,
                  end: Optional[str] = None) -> Optional[pd.DataFrame]:
        """指数：豁免换手率检查（.day 即权威）。"""
        return self.get_daily(code, start, end, is_index=True)

    def get_weekly(self, symbol: str, start: Optional[str] = None,
                   end: Optional[str] = None) -> Optional[pd.DataFrame]:
        return self.get_daily(symbol, start, end)
