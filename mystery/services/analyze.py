"""mystery.services.analyze — 唯一分析入口。

对外只暴露 analyze_one_stock() → AnalysisResult。
Web / CLI / scan / 板块钻取全部走这里，保证同股同分（误差 ≤ 1）。
MYSTERY_CHAN_ENABLED 缺省 1（结构默认展示）；MYSTERY_CHAN_SCORE 缺省 0（混合分默认关），
评分仍与 stock_analyzer 1.22.30 完全兼容。
"""
from __future__ import annotations

import logging
import os
from typing import Dict, Optional

from ..adapters import market as _market
from ..adapters import sector as _sector
from ..config import load_config
from ..core.models import AnalysisResult, BarSeries, ChanStructure, MarketContext, MysteryBreakdown
from ..core.mystery_rules import MysteryLogic
from ..core.patterns import PatternRecognition
from ..core import scorer as _scorer

logger = logging.getLogger(__name__)

_INDEX_CODE = 'sh.000001'   # 上证指数


def _env_flag(name: str) -> Optional[bool]:
    """读布尔环境变量；未设置返回 None（由调用方决定缺省）。"""
    v = os.environ.get(name)
    if v is None:
        return None
    return v.strip().lower() not in ('0', 'false', 'off', '')


def chan_enabled() -> bool:
    """缠论结构展示开关：env MYSTERY_CHAN_ENABLED → config chan.enabled（默认开）。"""
    flag = _env_flag('MYSTERY_CHAN_ENABLED')
    if flag is not None:
        return flag
    return bool((load_config().get('chan') or {}).get('enabled', True))


def chan_score_enabled() -> bool:
    """混合分开关：env MYSTERY_CHAN_SCORE → config chan.score（默认关）。"""
    v = os.environ.get('MYSTERY_CHAN_SCORE')
    if v is not None:
        return v.strip().lower() in ('1', 'true', 'on', 'yes')
    return bool((load_config().get('chan') or {}).get('score', False))


def _avg_turnover_20(daily: BarSeries) -> Optional[float]:
    """近 20 根日 K 换手率均值(%)；换手率 0 计入均值，20 根全无效值才返回 None。"""
    vals = []
    for b in daily.bars[-20:]:
        t = b.turnover
        if t is None:
            continue
        try:
            vals.append(float(t))
        except (TypeError, ValueError):
            continue
    if not vals:
        return None
    return round(sum(vals) / len(vals), 4)


def _high_120(daily: BarSeries) -> Optional[float]:
    """近 120 根日 K 最高价（chip_low 低位门闩判据）；无有效值则 None。"""
    vals = [float(b.high) for b in daily.bars[-120:] if b.high]
    if not vals:
        return None
    return round(max(vals), 4)


class AnalysisService:
    """分析服务（持有客户端与规则实例，线程安全可复用）。"""

    def __init__(self, cfg: Optional[Dict] = None):
        self.cfg = cfg or {}
        self.market = _market.MarketDataClient(self.cfg)
        self.sector = _sector.SectorClient(self.cfg)
        self.logic = MysteryLogic()
        self.patterns = PatternRecognition()

    # ---------------- 市场上下文 ----------------
    def build_market_context(self, symbol: str,
                             daily: BarSeries) -> MarketContext:
        """指数 + 行业（名称/强度分/趋势）+ 财务。"""
        ctx = MarketContext()
        try:
            index_series = self.market.fetch_index(_INDEX_CODE, '1d')
            if index_series.bars:
                ctx.index_bars = index_series
        except Exception as e:
            logger.debug(f"指数获取失败: {str(e)[:60]}")
        try:
            ind = self.sector.get_industry(symbol)
            ctx.industry_name = ind.get('name') or '未知'
            ctx.industry_score = ind.get('score')
            ctx.industry_up = ind.get('up')
        except Exception as e:
            logger.debug(f"行业获取失败: {str(e)[:60]}")
        try:
            ctx.financial = self.market.db.get_financial(symbol)
        except Exception as e:
            logger.debug(f"财务获取失败: {str(e)[:60]}")
        return ctx

    # ---------------- 规则明细（收口到 core.pipeline 纯计算） ----------------
    def run_rules(self, daily: BarSeries, weekly: Optional[BarSeries],
                  monthly: Optional[BarSeries], ctx: MarketContext,
                  include_detail: bool) -> MysteryBreakdown:
        from ..core import pipeline as _pipe

        return _pipe.run_mystery(daily, weekly, monthly, ctx,
                                 include_detail=include_detail, logic=self.logic,
                                 patterns=self.patterns)

    def _analyze_chan(self, daily: BarSeries) -> Dict[str, ChanStructure]:
        """缠论多周期分析（按配置 freqs，默认 1d/1w；日/周都走 chan_cache）。

        004.md：只算 config.chan.freqs（不无配置就算 1M）；行情日/版本变化才失效。
        """
        from ..adapters.czsc_adapter import CzscAdapter, chan_from_dict, czsc_version
        import json as _json

        ver = czsc_version()
        trade_date = str(daily.bars[-1].dt)[:10] if daily.bars else ''
        freqs = list((self.cfg.get('chan') or {}).get('freqs', ['1d', '1w']))
        adapter = CzscAdapter()
        out: Dict[str, ChanStructure] = {}
        for freq in freqs:
            try:
                series = daily if freq == daily.freq else self.market.fetch_bars(
                    daily.symbol, freq)
                if not series.bars:
                    continue
                cached_raw = self.market.db.get_chan_cache(
                    daily.symbol, freq, trade_date, ver)
                if cached_raw:
                    out[freq] = chan_from_dict(_json.loads(cached_raw))
                    continue
                s = adapter.analyze(series)
                out[freq] = s
                if s.engine_ver == "unavailable":
                    logger.error(
                        f"MYSTERY_CHAN_ENABLED=1 但 czsc 未安装："
                        f"pip install -e '.[chan]' 后重启（{daily.symbol}）")
                self.market.db.set_chan_cache(
                    daily.symbol, freq, trade_date, ver,
                    _json.dumps(s.to_dict(), ensure_ascii=False))
            except Exception as e:
                logger.warning(f"缠论 {freq} 分析失败({daily.symbol}): {str(e)[:100]}")
        return out

    def analyze_one_stock(self, symbol: str,
                          include_detail: bool = True) -> AnalysisResult:
        """单票完整分析（CLAUDE.md §7.4 伪代码）。"""
        daily = self.market.fetch_bars(symbol, '1d')
        if not daily.bars:
            raise RuntimeError(f"[{symbol}] 无日K数据（本地库/在线源均失败）")
        weekly = self.market.fetch_bars(symbol, '1w')
        monthly = self.market.fetch_bars(symbol, '1M')

        internal = daily.symbol
        ctx = self.build_market_context(internal, daily)

        # 缠论（P2：只展示不进评分；MYSTERY_CHAN_ENABLED=0 时 Service 不调用 Adapter）
        chan: Dict[str, ChanStructure] = {}
        if chan_enabled():
            chan = self._analyze_chan(daily)

        bd = self.run_rules(daily, weekly, monthly, ctx, include_detail)
        # 混合分开关：chan_enabled AND chan_score_enabled（结构展示 ≠ 混合分）
        mix_enabled = chan_enabled() and chan_score_enabled()
        score, advice, true_res = _scorer.combine(bd, chan,
                                                  chan_enabled=mix_enabled)
        last = daily.bars[-1]
        name = self.market.db.get_stock_name(internal) or ''
        turnover_20 = _avg_turnover_20(daily)
        high_120 = _high_120(daily)
        czsc_ver = ''
        if chan:
            from ..adapters.czsc_adapter import czsc_version
            czsc_ver = czsc_version()
        result = AnalysisResult(
            symbol=internal,
            name=name,
            trade_date=str(last.dt)[:10],
            price=float(last.close),
            score=score,
            advice=advice,
            true_resonance=true_res,
            turnover_20=turnover_20,
            high_120=high_120,
            mystery=bd,
            chan=chan,
            sector={'行业名称': ctx.industry_name, '行业趋势分': ctx.industry_score,
                    '行业趋势': ctx.industry_up},
            financial=ctx.financial,
            rule_ver='mystery-1.22.30-compat',
            czsc_ver=czsc_ver,
        )
        return result


def analyze_one_stock(symbol: str, include_detail: bool = True,
                      cfg: Optional[Dict] = None) -> AnalysisResult:
    """唯一分析入口（模块级便捷函数）。"""
    return AnalysisService(cfg).analyze_one_stock(symbol, include_detail=include_detail)
