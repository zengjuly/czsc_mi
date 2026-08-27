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
                kline = self.db.get_sector_kline(s_code)
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
            kline = self.db.get_sector_kline(sector_code)
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
        return self.db.get_sector_kline(sector_code)
