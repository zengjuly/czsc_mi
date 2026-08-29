"""mystery.adapters.tdx_api — 通达信 HTTP API（tdx-api 本地容器，第二备用源）。

对照旧仓 stock_analyzer/data/tdx_api_client.py：
- 股票走 /api/kline-all，指数走 /api/index（代码带交易所前缀 SH600519/SZ000001）。
- 响应价格字段为原始价×1000（需 /1000 还原）；无换手率（不伪造）。
- 失败/异常返回空 DataFrame，由 market.py 降级链继续（不抛垮整次 sync）。

依赖：urllib（标准库），不引入 requests。
"""
from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from typing import Dict, Optional

import pandas as pd

logger = logging.getLogger(__name__)

_DEFAULT_API_URL = os.environ.get("TDX_API_URL", "http://localhost:8080/api")


class TdxApiClient:
    """tdx-api 本地容器 REST 客户端。"""

    def __init__(self, cfg: Optional[Dict] = None):
        self.cfg = cfg or {}
        tcfg = (self.cfg.get("data_source", {})
                .get("tdx_api_config", {})) or {}
        self.api_url = tcfg.get("api_url") or _DEFAULT_API_URL
        self.timeout = float(tcfg.get("timeout", 5))

    # ---------------- 接口 ---------------- 
    def _request(self, endpoint: str, code: str, params: dict) -> dict:
        url = f"{self.api_url}/{endpoint}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "czsc-mi/0.5"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    def fetch_daily(self, stock_code: str, days: int = 1100,
                    start_date: Optional[str] = None,
                    end_date: Optional[str] = None,
                    period: str = "daily") -> pd.DataFrame:
        """拉日K并统一为中文列（price×1000 → 元）。

        接口要求带交易所前缀大写代码（SZ000001/SH600519）；指数走 /api/index。
        """
        pure = str(stock_code).replace(".", "").replace("sh", "").replace("sz", "")
        pure = "".join(ch for ch in pure if ch.isdigit())
        mkt = "SH" if str(stock_code).startswith("sh") else (
            "SZ" if str(stock_code).startswith("sz") else "BJ")
        api_code = f"{mkt}{pure}"
        _is_idx = (
            (str(stock_code).startswith("sh") and pure.startswith("000"))
            or (str(stock_code).startswith("sz") and pure.startswith("399"))
            or (str(stock_code).startswith("bj") and pure.startswith("899"))
        )
        endpoint = "index" if _is_idx else "kline-all"
        try:
            res = self._request(endpoint, api_code, {"code": api_code, "type": "day"})
        except Exception as e:
            logger.debug(f"tdx-api 请求异常 [{stock_code}]: {str(e)[:80]}")
            return pd.DataFrame()
        data = (res or {}).get("data", {}) if isinstance(res, dict) else {}
        items = (data.get("list") if isinstance(data, dict) else None) \
            or (data.get("List") if isinstance(data, dict) else None) or []
        if not items:
            return pd.DataFrame()
        df = pd.DataFrame(items)
        out = pd.DataFrame()
        try:
            out["日期"] = pd.to_datetime(df["Time"], utc=True,
                                         errors="coerce").dt.tz_localize(None) \
                if "Time" in df.columns else pd.NaT
            out["开盘价"] = df.get("Open", 0).astype(float) / 1000.0
            out["最高价"] = df.get("High", 0).astype(float) / 1000.0
            out["最低价"] = df.get("Low", 0).astype(float) / 1000.0
            out["收盘价"] = df.get("Close", 0).astype(float) / 1000.0
            out["成交量"] = df.get("Volume", 0).astype(float)
            out["成交额"] = df.get("Amount", 0).astype(float)
            out["换手率"] = None  # 备用源缺换手率——不伪造
        except Exception as e:
            logger.debug(f"tdx-api 解析异常 [{stock_code}]: {str(e)[:80]}")
            return pd.DataFrame()
        out = out.dropna(subset=["日期"]).sort_values("日期")
        if start_date:
            out = out[out["日期"] >= pd.to_datetime(start_date)]
        if end_date:
            out = out[out["日期"] <= pd.to_datetime(end_date)]
        if len(out) > days:
            out = out.tail(days)
        return out.reset_index(drop=True)

    def get_daily(self, symbol: str, start: Optional[str] = None,
                  end: Optional[str] = None) -> Optional[pd.DataFrame]:
        """market.py 降级链接口（symbol 为 sh600519 / sh.600519 均可）。"""
        return self.fetch_daily(symbol, start_date=start, end_date=end)

    def get_index(self, code: str, start: Optional[str] = None,
                  end: Optional[str] = None) -> Optional[pd.DataFrame]:
        return self.fetch_daily(code, start_date=start, end_date=end)
