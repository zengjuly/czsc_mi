"""mystery.adapters.ths — 同花顺扶摇数据源（迁自 stock_analyzer/data/ths_client.py）。

策略：本地 MarketDB(DuckDB) 秒级读取 → fuyao.py 子进程兜底。
API key 只走环境变量 HITHINK_FINANCE_API_KEY 或 config 注入（不入库）。
列：日期/开盘价/最高价/最低价/收盘价/成交量/成交额/换手率（换手率 None，与旧仓一致）。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

CN_COLS = ['日期', '开盘价', '最高价', '最低价', '收盘价', '成交量', '成交额', '换手率']


class ThsClient:
    """同花顺扶摇金融 API 客户端（第一主源）。"""

    def __init__(self, cfg: Optional[Dict] = None):
        cfg = cfg or {}
        self.cfg = cfg.get('data_source', {}).get('ths_config', {}) or {}
        # 本机路径一律环境变量注入（scripts/ 导出）；业务代码不写绝对路径
        self.script_path = (self.cfg.get('script_path')
                            or os.environ.get('THS_FUYAO_SCRIPT')
                            or '')
        self.adjust = self.cfg.get('adjust', 'forward')
        md_dir = (self.cfg.get('marketdb_dir')
                  or os.environ.get('THS_MARKETDB_DIR')
                  or '')
        self.marketdb_path = os.environ.get('MARKETDB_DB_PATH') or (
            os.path.join(md_dir, 'market.duckdb')
            if md_dir and os.path.isdir(md_dir) else md_dir)
        # API Key：环境变量优先，其次 config（顶层 hithink_api_key / ths_config）
        if not os.environ.get('HITHINK_FINANCE_API_KEY'):
            _key = (cfg.get('hithink_api_key') or self.cfg.get('hithink_api_key') or '')
            if _key:
                os.environ['HITHINK_FINANCE_API_KEY'] = str(_key)
        self._fuyao_python = self.cfg.get('python_path') or sys.executable

    # ---------------- 本地 MarketDB 秒级读取 ----------------
    def _fetch_daily_local(self, ths_code: str, start_date: str,
                           end_date: str, check_fresh: bool = False) -> pd.DataFrame:
        """本地 DuckDB 缓存（v_daily_qfq 前复权视图）。

        check_fresh=True 时做新鲜度检查：落后于最新交易日 → 返回空（走 fuyao 在线）。
        """
        if not self.marketdb_path or not os.path.exists(self.marketdb_path):
            return pd.DataFrame()
        try:
            from marketdb import MarketDB
            with MarketDB.open(self.marketdb_path) as db:
                df = db.get_daily(ths_code, start=start_date, end=end_date,
                                  adjust='forward')
            if df is not None and not df.empty:
                out = pd.DataFrame()
                out['日期'] = pd.to_datetime(df['date'])
                out['开盘价'] = df['open'].astype(float)
                out['最高价'] = df['high'].astype(float)
                out['最低价'] = df['low'].astype(float)
                out['收盘价'] = df['close'].astype(float)
                out['成交量'] = df['volume'].astype(float)
                if 'turnover' in df.columns:
                    out['成交额'] = df['turnover'].astype(float)
                elif 'amount' in df.columns:
                    out['成交额'] = df['amount'].astype(float)
                else:
                    out['成交额'] = 0.0
                out['换手率'] = None
                out = out.sort_values('日期').reset_index(drop=True)
                if check_fresh:
                    try:
                        from .calendar import get_latest_trade_date
                        latest = get_latest_trade_date()
                        if latest and not out.empty:
                            last_local = str(out['日期'].max())[:10]
                            if last_local < str(latest)[:10]:
                                logger.info(
                                    f"[ths本地MarketDB] {ths_code} 数据落后"
                                    f"(本地{last_local}<最新{latest})，走fuyao在线拉最新")
                                return pd.DataFrame()
                    except Exception as e:
                        logger.debug(f"MarketDB 新鲜度检查跳过: {str(e)[:60]}")
                return out
        except Exception as e:
            logger.debug(f"MarketDB 本地读取失败: {str(e)[:80]}")
        return pd.DataFrame()

    # ---------------- fuyao 子进程 ----------------
    def _run_fuyao(self, args: list, timeout: int = 30) -> list:
        """拉起 fuyao.py 子进程，提取纯净 JSON 流。"""
        if not os.path.exists(self.script_path):
            logger.warning(f"❌ 找不到同花顺 SDK: {self.script_path}")
            return []
        cmd = [self._fuyao_python, self.script_path, '--compact'] + args
        try:
            res = subprocess.run(cmd, capture_output=True, text=True,
                                 check=True, encoding='utf-8', timeout=timeout)
            out = res.stdout.strip()
            if out:
                data = json.loads(out)
                return data if isinstance(data, list) else [data]
        except Exception as e:
            logger.debug(f"fuyao 调用失败 {args[:2]}: {str(e)[:80]}")
        return []

    @staticmethod
    def _to_ths_code(stock_code: str) -> str:
        """sh600519 → 600519.SH；600519.SH → 600519.SH。"""
        code = str(stock_code).replace('.', '')
        if code[:2].upper() in ('SH', 'SZ', 'BJ'):
            return f"{code[2:]}.{code[:2].upper()}"
        return str(stock_code).upper()

    def probe_last_date(self, symbol: str) -> Optional[str]:
        """MarketDB 本地最新交易日（新鲜度参照，秒级）。无 MarketDB 返回 None。"""
        if not self.marketdb_path or not os.path.exists(self.marketdb_path):
            return None
        try:
            ths_code = self._to_ths_code(symbol)
            df = self._fetch_daily_local(ths_code,
                                         (datetime.now() - timedelta(days=45)).strftime('%Y-%m-%d'),
                                         datetime.now().strftime('%Y-%m-%d'))
            if df is not None and not df.empty:
                return str(df['日期'].max())[:10]
        except Exception as e:
            logger.debug(f"probe_last_date 失败: {str(e)[:60]}")
        return None

    # ---------------- 行情 ----------------
    def get_daily(self, symbol: str, start: Optional[str] = None,
                  end: Optional[str] = None,
                  days: int = 1100) -> Optional[pd.DataFrame]:
        """个股前复权日K（MarketDB 本地 → fuyao 在线兜底）。"""
        ths_code = self._to_ths_code(symbol)
        end_dt = pd.to_datetime(end) if end else datetime.now()
        start_dt = pd.to_datetime(start) if start else end_dt - timedelta(days=days)
        local_df = self._fetch_daily_local(ths_code,
                                           start_dt.strftime('%Y-%m-%d'),
                                           end_dt.strftime('%Y-%m-%d'),
                                           check_fresh=True)
        if local_df is not None and not local_df.empty:
            return local_df
        args = ['prices-historical', '--thscode', ths_code,
                '--start-ms', str(int(start_dt.timestamp() * 1000)),
                '--end-ms', str(int(end_dt.timestamp() * 1000)),
                '--adjust', self.adjust]
        raw = self._run_fuyao(args)
        if not raw:
            return pd.DataFrame()
        df = pd.DataFrame(raw)
        if df.empty or 'date_ms' not in df.columns:
            return pd.DataFrame()
        out = pd.DataFrame()
        out['日期'] = pd.to_datetime(df['date_ms'], unit='ms')
        out['开盘价'] = df.get('open_price', 0).astype(float)
        out['最高价'] = df.get('high_price', 0).astype(float)
        out['最低价'] = df.get('low_price', 0).astype(float)
        out['收盘价'] = df.get('close_price', 0).astype(float)
        out['成交量'] = df.get('volume', 0).astype(float)
        out['成交额'] = df.get('turnover', 0).astype(float)
        out['换手率'] = None
        return out.sort_values('日期').reset_index(drop=True)

    def get_weekly(self, symbol: str, start: Optional[str] = None,
                   end: Optional[str] = None) -> Optional[pd.DataFrame]:
        return self.get_daily(symbol, start, end)

    def get_financial(self, symbol: str) -> Dict:
        """估值快照（valuations-snapshot，PE/PB/PS）。失败返回空。"""
        ths_code = self._to_ths_code(symbol)
        raw = self._run_fuyao(['valuations-snapshot', '--thscodes', ths_code])
        if not raw:
            raw = self._run_fuyao(['valuations-snapshot', '--thscode', ths_code])
        if raw:
            item = raw[0].get('item', []) if isinstance(raw[0], dict) else raw
            if item and isinstance(item[0], dict):
                fin = item[0]
                return {'pe': fin.get('pe_ttm') or 0, 'pb': fin.get('pb_mrq') or 0,
                        'pe_mrq': fin.get('pe_mrq') or 0, 'ps': fin.get('ps_ttm') or 0}
        return {}

    @staticmethod
    def _latest_report_quarters(n: int = 4) -> List[str]:
        """最近 N 个已披露财报期（YYYY-Q，倒序；Q1末→Q4按自然季回推）。"""
        from datetime import date
        y, q = date.today().year, (date.today().month - 1) // 3 + 1
        out = []
        for _ in range(n):
            q -= 1
            if q == 0:
                q = 4
                y -= 1
            out.append(f"{y}-{q}")
        return out

    def get_indicators(self, symbol: str) -> Dict:
        """财报指标（financials-indicators，最近已披露季度优先）。

        取 扣非加权ROE/加权ROE/毛利率/净利率。失败返回 {}。
        """
        ths_code = self._to_ths_code(symbol)
        for report in self._latest_report_quarters(3):
            raw = self._run_fuyao(['financials-indicators',
                                   '--thscode', ths_code,
                                   '--report', report])
            if not raw:
                continue
            item = raw[0] if isinstance(raw[0], dict) else {}
            abilities = item.get('abilities') or []
            ind_map = {}
            for ab in (abilities or []):
                for ind in (ab.get('indicators') or []):
                    ind_map[ind.get('index_id')] = ind.get('value')
            if not ind_map:
                continue

            def _f(key):
                v = ind_map.get(key)
                try:
                    return float(v) if v not in (None, '') else None
                except (TypeError, ValueError):
                    return None

            out = {
                'roe': _f('index_deduct_weighted_avg_roe'),
                'roe_avg': _f('index_weighted_avg_roe'),
                'gp_margin': _f('sale_gross_margin'),
                'np_margin': _f('sale_net_interest_ratio'),
                'report_date': report,
            }
            if out['roe'] is not None or out['roe_avg'] is not None:
                return out
        return {}

    # ---------------- 证券列表 ----------------
    def get_stock_list(self) -> List[Dict]:
        """[{code, name}]：本地缓存优先（tickers-cache.json，7天内），网络兜底。"""
        cache_path = os.path.join(
            os.path.dirname(self.script_path), '..', 'docs', 'tickers-cache.json')
        cache_path = os.path.abspath(cache_path)
        try:
            if os.path.exists(cache_path) and \
                    time.time() - os.path.getmtime(cache_path) < 7 * 24 * 3600:
                with open(cache_path, encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, list) and len(data) >= 1000:
                    return [{'code': self._to_prefixed(x.get('thscode', '')),
                             'name': str(x.get('name', ''))} for x in data]
        except Exception as e:
            logger.debug(f"证券列表缓存读取失败: {e}")
        last_err = ''
        for attempt in range(3):
            try:
                args = ['tickers-list', '--all']
                if attempt == 0:
                    args.append('--refresh-cache')
                raw = self._run_fuyao(args, timeout=120)
                if raw:
                    try:
                        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                        with open(cache_path, 'w', encoding='utf-8') as f:
                            json.dump(raw, f, ensure_ascii=False)
                    except Exception as e:
                        logger.debug(f"证券列表缓存写入失败: {e}")
                    return [{'code': self._to_prefixed(x.get('thscode', '')),
                             'name': str(x.get('name', ''))} for x in raw]
                last_err = '空结果'
            except Exception as e:
                last_err = str(e)[:80]
        logger.warning(f"⚠️ fuyao tickers-list 全部失败: {last_err}")
        return []

    @staticmethod
    def _to_prefixed(thscode: str) -> str:
        """600519.SH → sh600519（扫描/分析统一输入格式）。"""
        s = str(thscode)
        if '.' in s:
            digits, mkt = s.split('.')
            return f"{mkt.lower()}{digits}"
        return s
