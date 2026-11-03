"""mystery.adapters.sector — 板块数据（真实指数 K 线 / 主行业归属）。

板块强度禁止成分股抽样；只用 sector_kline 真实指数（ths_ 前缀）。
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

import pandas as pd

from ..core import resonance as _res
from ..store.db import MysteryDB

logger = logging.getLogger(__name__)


class SectorClient:
    """板块客户端。"""

    def __init__(self, cfg: Optional[Dict] = None):
        self.cfg = cfg or {}
        self.db = MysteryDB(db_path=self.cfg.get('db_path') or None)
        # W优化⑦：板块指数 K 线按 sector_code 记忆化。同一行业被成百上千只
        # 个股共享，全市场扫描若不缓存会重复读库 O(个股数) 次；缓存后降为
        # O(行业数)。生命周期跟随本实例（扫描 worker / 单次分析各一份），
        # 板块 K 线属低频参考量，实例级缓存无脏读风险。
        self._sector_kline_cache: Dict[str, Optional[pd.DataFrame]] = {}

    def _sector_kline(self, sector_code: str) -> Optional[pd.DataFrame]:
        """板块 K 线记忆化读取（W优化⑦）：与 db.get_sector_kline 同归一规则，
        结果按归一后 sector_code 缓存，避免同行业被多只个股重复读库。"""
        code = str(sector_code)
        if not code.startswith('ths_'):
            code = f'ths_{code.split(".")[0]}'
        cache = self._sector_kline_cache
        if code in cache:
            return cache[code]
        df = self.db.get_sector_kline(sector_code)
        cache[code] = df
        return df

    def get_industry(self, symbol: str) -> Dict:
        """主行业：{code, name, score, up}。score 为 0~25 精确分（>12.5 向上）。"""
        from ..adapters.codes import db_code_of
        db_code = db_code_of(symbol) if not symbol.startswith(('sh.', 'sz.', 'bj.')) else symbol
        out = {'code': None, 'name': '未知', 'score': None, 'up': None}
        try:
            primary = self.db.get_primary_industry(db_code)
            if primary:
                s_code, s_name = primary[0], primary[1]
                out['code'] = s_code
                if s_name:
                    out['name'] = s_name
                kline = self._sector_kline(s_code)
                if kline is not None and not kline.empty:
                    score = _res.calculate_industry_score_from_sector(kline)
                    out['score'] = float(score)
                    out['up'] = bool(score > 12.5)
        except Exception as e:
            logger.debug(f"get_industry({symbol}) 失败: {str(e)[:80]}")
        return out

    def get_sector(self, sector_code: str) -> Dict:
        """板块直查：{code, name, score, up}（ths_886109 等板块代码）。"""
        out = {'code': str(sector_code), 'name': '', 'score': None, 'up': None}
        try:
            kline = self._sector_kline(sector_code)
            if kline is not None and not kline.empty:
                score = _res.calculate_industry_score_from_sector(kline)
                out['score'] = float(score)
                out['up'] = bool(score > 12.5)
            meta = self.db.get_sector_meta(active_only=False)
            for m in meta:
                if str(m[0]) == str(sector_code):
                    out['name'] = m[1] or ''
                    break
        except Exception as e:
            logger.debug(f"get_sector({sector_code}) 失败: {str(e)[:80]}")
        return out

    def get_sector_kline(self, sector_code: str) -> Optional[pd.DataFrame]:
        """板块指数 K 线（升序，收盘价/成交额…），ths_ 前缀自动归一。"""
        return self._sector_kline(sector_code)
