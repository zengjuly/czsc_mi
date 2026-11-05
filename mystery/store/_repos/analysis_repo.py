"""mystery.store._repos.analysis_repo - AnalysisRepoMixin（按职责从 MysteryDB 抽取，公开 API 不变）。"""
from __future__ import annotations
import logging
import os
import sqlite3
import threading
from typing import Any, Dict, List, Optional
import pandas as pd
from ._base import BaseRepoMixin
from ._helpers import *

class AnalysisRepoMixin(BaseRepoMixin):
    def set_financial(self, code: str, report_date: str,
                          roe=None, roe_avg=None, np_margin=None, gp_margin=None,
                          pe=None, pb=None, eps_ttm=None, divid_cash=None) -> None:
            """upsert 财务快照（code 归一为 sh.600519，与 get_financial 一致）。

            W36 P1-2：改列级合并（旧实现整行 INSERT OR REPLACE，调用方只传
            部分列时会把 net_profit/eps_ttm/divid_cash 洗成 NULL）。
            非 None 新值才覆盖；None 保留库内旧值。
            """
            from mystery.adapters.codes import db_code_of
            db_code = db_code_of(code) if not code.startswith(('sh.', 'sz.', 'bj.')) \
                else code
            cols = {"roe": roe, "roe_avg": roe_avg, "np_margin": np_margin,
                    "gp_margin": gp_margin, "PE": pe, "PB": pb,
                    "eps_ttm": eps_ttm, "divid_cash": divid_cash}
            names = ", ".join(cols)
            upd = ", ".join(f"{c}=COALESCE(excluded.{c}, {c})" for c in cols)
            with self._lock:
                conn = self._connect()
                try:
                    conn.execute(
                        f"INSERT INTO stock_financial_data "
                        f"(code, report_date, {names}) "
                        f"VALUES (?,?,{','.join(['?'] * len(cols))}) "
                        f"ON CONFLICT(code, report_date) DO UPDATE SET {upd}",
                        (db_code, report_date, *cols.values()))
                    conn.commit()
                finally:
                    conn.close()
    def get_financial(self, code: str) -> Dict[str, Any]:
            """最新财报快照 {pe, pb, roe, roe_avg, eps_ttm, divid_cash, report_date...}。"""
            from mystery.adapters.codes import db_code_of
            db_code = db_code_of(code) if not code.startswith(('sh.', 'sz.', 'bj.')) else code
            try:
                with self._lock:
                    conn = self._connect()
                    try:
                        row = conn.execute(
                            "SELECT report_date, roe, roe_avg, np_margin, gp_margin, "
                            "net_profit, eps_ttm, PB, PE, divid_cash "
                            "FROM stock_financial_data WHERE code=? "
                            "ORDER BY report_date DESC LIMIT 1", (db_code,)).fetchone()
                        if not row:
                            return {}
                        # report_date 是 'YYYY-Q'/'YYYY-MM-DD' 字符串，绝不能 float()——
                        # float('2026-2') 抛 ValueError 曾让本函数整体 except 返回 {}，
                        # 86 只自选每天逐股在线补财务（W12）。
                        fin = {'report_date': row[0]}
                        for k, v in zip(['roe', 'roe_avg', 'np_margin', 'gp_margin',
                                         'net_profit', 'eps_ttm', 'PB', 'PE',
                                         'divid_cash'], row[1:]):
                            fin[k] = float(v) if v is not None else None
                        return fin
                    finally:
                        conn.close()
            except Exception as e:
                logger.debug(f"财务数据读取失败: {e}")
                return {}
    def get_financials(self, codes: List[str]) -> Dict[str, Dict[str, Any]]:
            """批量最新财报快照 → {db_code: {pe,pb,roe,...}}（无财务不含 key）。

            与 get_financial 同字段、同归一（db_code_of）；每个 code 取
            report_date 最新一条（窗口聚合），一次 IN 查询替代逐票查询，
            供扫描上下文批量预取（W63）。
            """
            out: Dict[str, Dict[str, Any]] = {}
            if not codes:
                return out
            from mystery.adapters.codes import db_code_of
            db_codes = [db_code_of(c) if not c.startswith(('sh.', 'sz.', 'bj.'))
                        else c for c in codes]
            with self._lock:
                conn = self._connect()
                try:
                    ph = ",".join("?" * len(db_codes))
                    sql = ("SELECT f.code, f.report_date, f.roe, f.roe_avg, "
                           "f.np_margin, f.gp_margin, f.net_profit, f.eps_ttm, "
                           "f.PB, f.PE, f.divid_cash "
                           "FROM stock_financial_data f "
                           "JOIN (SELECT code, MAX(report_date) AS mr "
                           "FROM stock_financial_data WHERE code IN (%s) "
                           "GROUP BY code) latest "
                           "ON f.code=latest.code AND f.report_date=latest.mr "
                           "WHERE f.code IN (%s)" % (ph, ph))
                    rows = conn.execute(sql, tuple(db_codes + db_codes)).fetchall()
                    for row in rows:
                        db_code = row[0]
                        fin = {'report_date': row[1]}
                        for k, v in zip(['roe', 'roe_avg', 'np_margin', 'gp_margin',
                                         'net_profit', 'eps_ttm', 'PB', 'PE',
                                         'divid_cash'], row[2:]):
                            fin[k] = float(v) if v is not None else None
                        out[db_code] = fin
                finally:
                    conn.close()
            return out

    def get_chan_cache(self, symbol: str, freq: str, trade_date: str,
                           czsc_ver: str) -> Optional[str]:
            with self._lock:
                conn = self._connect()
                try:
                    row = conn.execute(
                        "SELECT payload_json FROM chan_cache "
                        "WHERE symbol=? AND freq=? AND trade_date=? AND czsc_ver=?",
                        (symbol, freq, trade_date, czsc_ver)).fetchone()
                    return row[0] if row else None
                finally:
                    conn.close()
    def set_chan_cache(self, symbol: str, freq: str, trade_date: str,
                           czsc_ver: str, payload_json: str) -> None:
            with self._lock:
                conn = self._connect()
                try:
                    conn.execute(
                        "INSERT OR REPLACE INTO chan_cache "
                        "(symbol, freq, trade_date, czsc_ver, payload_json) "
                        "VALUES (?,?,?,?,?)",
                        (symbol, freq, trade_date, czsc_ver, payload_json))
                    conn.commit()
                finally:
                    conn.close()
