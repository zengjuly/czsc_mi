"""mystery.services.analyze — 唯一分析入口。

对外只暴露 analyze_one_stock() → AnalysisResult。
Web / CLI / scan / 板块钻取全部走这里，保证同股同分（误差 ≤ 1）。
MYSTERY_CHAN_ENABLED 缺省 1（结构默认展示）；MYSTERY_CHAN_SCORE 缺省 0（混合分默认关），
评分仍与 stock_analyzer 1.22.30 完全兼容。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from ..adapters import market as _market
from ..adapters import sector as _sector
from ..config import load_config
from ..core.models import (AnalysisResult, BarSeries, ChanStructure,
                           MarketContext, MysteryBreakdown,
                           RULE_VER, RULE_VER_CHAN)
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


def market_env_summary(market) -> Dict[str, Any]:
    """大盘滤网快照（W37，单一实现，四方展示只读调用）。

    双锚（上证 000001.SH + 深证成指 399311.SZ；同花顺全A/中证全指实测
    fuyao 无数据，见 core/market_env 模块 docstring）走既有
    MarketDataClient.fetch_index 归一通道（会话级缓存 + 在线命中自动
    落库），无旁路取数。缺数据 → verdict「大盘未知」，不猜。
    **纯展示字段，不进 scorer、不改 rule_ver。**
    """
    from ..core.market_env import ANCHORS, anchor_env, market_env

    anchors = []
    for name, code in ANCHORS:
        closes = []
        last_dt = ''
        try:
            series = market.fetch_index(code, '1d')
            if series and series.bars:
                closes = [float(b.close) for b in series.bars]
                last_dt = str(series.bars[-1].dt)[:10]
        except Exception as e:
            logger.debug(f"大盘锚 {code} 获取失败: {str(e)[:60]}")
        a = anchor_env(name, code, closes)
        a["date"] = last_dt
        anchors.append(a)
    return market_env(anchors)


def market_env_line(env: Dict[str, Any]) -> str:
    """一行文字摘要（QA 行/终端/飞书共用；缺数据字段跳过）。"""
    parts = [env.get('verdict', '大盘未知')]
    for a in env.get('anchors', []):
        if a.get('close') is None:
            parts.append(f"{a['name']}=缺数据")
            continue
        tag = "站年线" if a.get('above') else "破年线"
        arr = "多头排列" if a.get('aligned') else "非多头"
        parts.append(f"{a['name']}{a['close']:.0f}({tag}/{arr},"
                     f"20日{a['chg_20d']:+.1f}%)")
    return "[大盘滤网] " + "；".join(parts)


def _avg_turnover_20(daily: BarSeries) -> Optional[float]:
    """近 20 根日 K 有效换手率均值(%)（W35 P0-A）。

    只计 is_valid 值（0 < turn < 80；None/0/越界一律跳过——历史上
    缺数写 0 会系统性拉低均值、污染 chip_low/换手标签），20 根全无效返回 None。
    """
    vals = []
    for b in daily.bars[-20:]:
        t = b.turnover
        if t is None:
            continue
        try:
            f = float(t)
        except (TypeError, ValueError):
            continue
        if f > 0:
            vals.append(f)
    if not vals:
        return None
    return round(sum(vals) / len(vals), 4)


def _high_120(daily: BarSeries) -> Optional[float]:
    """近 120 根日 K 最高价（chip_low 低位门闩判据）；无有效值则 None。"""
    vals = [float(b.high) for b in daily.bars[-120:] if b.high]
    if not vals:
        return None
    return round(max(vals), 4)


# ---------------- W23 分析结果缓存（006.md 阶段 3） ----------------

def cache_allowed(daily: BarSeries) -> bool:
    """只缓存「收盘后本地日 K」结果：在线源（ths/tdx_api/tdx_local）多为
    盘中或降级实时数据，指纹虽能兜底，但按 006「收盘后日级有效」保守不写。"""
    return str(getattr(daily, 'source', '') or '') in ('db', 'sqlite', 'duckdb')


def _result_from_payload(data: dict) -> AnalysisResult:
    """analysis_cache payload（AnalysisResult.to_dict 形态）→ AnalysisResult。

    chan 结构用 chan_from_dict 还原；任何组件缺失按默认值容错（缓存是加速
    层，宁可弱还原也不抛——消费方实际只用 to_dict 字段）。
    """
    from ..adapters.czsc_adapter import chan_from_dict
    chan = {f: chan_from_dict(d) for f, d in (data.get('chan') or {}).items()}
    return AnalysisResult(
        symbol=data.get('symbol', ''),
        name=data.get('name', ''),
        trade_date=data.get('trade_date', ''),
        price=data.get('price'),
        score=data.get('score'),
        advice=data.get('advice', ''),
        true_resonance=bool(data.get('true_resonance', False)),
        turnover_20=data.get('turnover_20'),
        high_120=data.get('high_120'),
        mystery=MysteryBreakdown(**(data.get('mystery') or {})),
        chan=chan,
        sector=data.get('sector') or {},
        financial=data.get('financial') or {},
        rule_ver=data.get('rule_ver', ''),
        czsc_ver=data.get('czsc_ver', ''),
        bars_fingerprint=data.get('bars_fingerprint', ''),
        data_source=data.get('data_source', ''),
    )


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
                             fill_financial: bool = True) -> MarketContext:
        """指数 + 行业（名称/强度分/趋势）+ 财务。

        W36 P1-1：fill_financial=False 时只用库内财务，缺则保持 None →
        规则 unknown 语义。全市场扫描链禁逐只打 THS 财务 API（86 只
        watchlist 实测库内 ROE 覆盖率 5510/5518，在线补齐只是兜底）；
        个股交互路径（CLI/Web 详情）保持 True，补齐后回填库供扫描复用。
        """
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
        # W9: 本地库缺财务（或只有 PE/PB 缺 ROE）→ ths 在线补齐并回填库。
        # valuations-snapshot(PE/PB/PS) + financials-indicators(扣非ROE/毛利率/净利率)
        # W36 P1-1：仅 fill_financial=True（个股交互路径）才允许外呼补齐。
        fin = ctx.financial or {}
        if fill_financial and not fin.get('roe'):
            try:
                val = self.market.ths.get_financial(symbol) or {}
                ind = self.market.ths.get_indicators(symbol) or {}
                if val or ind:
                    report = (ind.get('report_date')
                              or val.get('report_date') or '')
                    fin = {
                        'PE': val.get('pe'), 'PB': val.get('pb'),
                        'roe': ind.get('roe'), 'roe_avg': ind.get('roe_avg'),
                        'np_margin': ind.get('np_margin'),
                        'gp_margin': ind.get('gp_margin'),
                        'report_date': report,
                    }
                    ctx.financial = fin
                    # 回填本地库（下次直接命中；失败不影响本次分析）
                    try:
                        if report:
                            self.market.db.set_financial(
                                symbol, report, roe=fin.get('roe'),
                                roe_avg=fin.get('roe_avg'),
                                np_margin=fin.get('np_margin'),
                                gp_margin=fin.get('gp_margin'),
                                pe=val.get('pe'), pb=val.get('pb'))
                    except Exception as e:
                        logger.debug(f"财务回填失败: {str(e)[:60]}")
            except Exception as e:
                logger.debug(f"财务在线补齐失败: {str(e)[:60]}")
        return ctx

    # ---------------- 规则明细（收口到 core.pipeline 纯计算） ----------------
    def run_rules(self, daily: BarSeries, weekly: Optional[BarSeries],
                  monthly: Optional[BarSeries], ctx: MarketContext,
                  include_detail: bool) -> MysteryBreakdown:
        from ..core import pipeline as _pipe

        return _pipe.run_mystery(daily, weekly, monthly, ctx,
                                 include_detail=include_detail, logic=self.logic,
                                 patterns=self.patterns)

    def _analyze_chan(self, daily: BarSeries,
                      with_signals: bool = False) -> Dict[str, ChanStructure]:
        """缠论多周期分析（按配置 freqs，默认 1d/1w；日/周都走 chan_cache）。

        004.md：只算 config.chan.freqs（不无配置就算 1M）；行情日/版本变化才失效。
        010.md 6C：with_signals=True（仅混合分路径）时对日线附 czsc
        买卖点/背驰标签（adapter.signal_flags，失败当无标签）。
        注意：标签在 adapter.analyze 结果上补齐后再写 chan_cache，
        缓存键 ver 不含 score 开关——分关先跑过的票命中缓存时无标签，
        故这里对缓存命中的日线结构也按需补标签（标签本身幂等、成本低）。
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
                # W24（006.md 阶段4）：周/月一律由已取日 K 重采样派生，
                # 禁止再 fetch_bars 重复读库（口径同 resample_bars 纯函数）。
                series = daily if freq == daily.freq else \
                    self.market.resample_bars(daily, freq)
                if not series.bars:
                    continue
                cached_raw = self.market.db.get_chan_cache(
                    daily.symbol, freq, trade_date, ver)
                if cached_raw:
                    out[freq] = chan_from_dict(_json.loads(cached_raw))
                else:
                    s = adapter.analyze(series)
                    if s.engine_ver == "unavailable":
                        # W35 P0-B：引擎不可用的空结构**不写缓存**——否则
                        # czsc 缺装机器会把"不可用"固化到共享 chan_cache，
                        # 装好引擎后仍长期读到空结构。当次返回空结构仅影响
                        # 本票本轮展示，下次自然重试。
                        logger.error(
                            f"MYSTERY_CHAN_ENABLED=1 但 czsc 未安装："
                            f"pip install -e '.[chan]' 后重启（{daily.symbol}）")
                        out[freq] = s
                        continue
                    out[freq] = s
                    self.market.db.set_chan_cache(
                        daily.symbol, freq, trade_date, ver,
                        _json.dumps(s.to_dict(), ensure_ascii=False))
                # W34：日线/周线都补标签（周线供背驰共振展示）；
                # 标签仍只在 mix 路径进 scorer，分关零影响。
                if with_signals and freq in ('1d', '1w'):
                    st = out[freq]
                    if not st.bs_flag and not st.divergence:
                        bs, div, sig_ok = adapter.signal_flags(series)
                        # W30b（011.md）：空标签必须可见——signals_ok=False
                        # 说明 czsc 异常/版本漂移，当无标签进分但记 WARNING
                        if not sig_ok:
                            logger.warning(
                                f"czsc 信号异常({daily.symbol}): "
                                f"bs= div= signals_ok=False（当无标签计 0）")
                        elif not bs and not div:
                            logger.info(
                                f"czsc 信号无标签({daily.symbol}): "
                                f"bs= div= signals_ok=True")
                        if bs or div:
                            st.bs_flag, st.divergence = bs, div
            except Exception as e:
                logger.warning(f"缠论 {freq} 分析失败({daily.symbol}): {str(e)[:100]}")
        return out

    def analyze_one_stock(self, symbol: str,
                          include_detail: bool = True,
                          use_cache: bool = True,
                          fill_financial: bool = True) -> AnalysisResult:
        """单票完整分析（CLAUDE.md §7.4 伪代码）。

        W23：use_cache=True 时头尾走 analysis_cache（键含开关/版本/K线指纹，
        改任何一项必 miss；sync 写该票 K 线时同事务删除旧缓存）。
        W36 P1-1：fill_financial=False（扫描链）时不在线补财务，缺则 unknown。
        """
        daily = self.market.fetch_bars(symbol, '1d')
        if not daily.bars:
            raise RuntimeError(f"[{symbol}] 无日K数据（本地库/在线源均失败）")
        trade_date = str(daily.bars[-1].dt)[:10]
        # ---- 缓存读（W23）----
        ck = None
        # 010.md 6C：混合分路径（结构开+分开关）用新规则口径 ver；分关不变
        mix = chan_enabled() and chan_score_enabled()
        rule = RULE_VER_CHAN if mix else RULE_VER
        if use_cache and cache_allowed(daily):
            try:
                from ..store.cache import AnalysisCache, bars_fingerprint, \
                    make_cache_key
                from ..adapters.czsc_adapter import czsc_version
                ver = czsc_version() if chan_enabled() else ''
                # W36 P1-1：F 位入键——扫描链(F=0)缺 ROE 的 unknown 结果
                # 不得被个股详情(F=1,在线补齐)误命中，反之亦然。
                flags = (f"D{int(include_detail)}|C{int(chan_enabled())}"
                         f"|S{int(chan_score_enabled())}|F{int(fill_financial)}")
                fp = bars_fingerprint(daily.bars)
                ck = make_cache_key(
                    daily.adjust, rule, flags, ver, fp)
                hit = AnalysisCache(self.market.db).get(
                    daily.symbol, trade_date, ck)
                if hit is not None:
                    res = _result_from_payload(hit)
                    # W33：缓存行不含 K 线指纹（旧 payload），命中时按当前
                    # 序列补齐——指纹即缓存键成分，命中意味着指纹一致。
                    res.bars_fingerprint = fp[:8]
                    res.data_source = daily.source
                    return res
            except Exception as e:
                logger.debug(f"[cache] 读缓存失败({symbol}): {str(e)[:80]}")
                ck = None
        # W15：周/月由已取日 K 重采样派生（不再重复读库），口径与
        # fetch_bars('1w'/'1M') 完全一致（同 resample 纯函数 + 同 source 标记）。
        weekly = self.market.resample_bars(daily, '1w')
        monthly = self.market.resample_bars(daily, '1M')

        internal = daily.symbol
        ctx = self.build_market_context(internal,
                                        fill_financial=fill_financial)

        # 缠论（P2：只展示不进评分；MYSTERY_CHAN_ENABLED=0 时 Service 不调用 Adapter）
        # 010.md 6C：mix（结构开+分开关）时标签进 scorer；
        # W34：标签生成门控放宽到 chan_enabled——日/周背驰共振列在生产
        # 默认（分关）路径也要有值。安全性：scorer.combine 仅 mix 时读
        # chan 标签，分关分数逐字段不变；缓存键含 S 标志，两路径互不污染。
        chan: Dict[str, ChanStructure] = {}
        if chan_enabled():
            chan = self._analyze_chan(daily, with_signals=True)

        bd = self.run_rules(daily, weekly, monthly, ctx, include_detail)
        # 混合分开关：chan_enabled AND chan_score_enabled（结构展示 ≠ 混合分）
        score, advice, true_res = _scorer.combine(bd, chan,
                                                  chan_enabled=mix)
        last = daily.bars[-1]
        name = self.market.db.get_stock_name(internal) or ''
        turnover_20 = _avg_turnover_20(daily)
        high_120 = _high_120(daily)
        czsc_ver = ''
        if chan:
            from ..adapters.czsc_adapter import czsc_version
            czsc_ver = czsc_version()
        from ..store.cache import bars_fingerprint as _fp
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
            rule_ver=rule,
            czsc_ver=czsc_ver,
            bars_fingerprint=_fp(daily.bars)[:8],   # W33 诊断展示
            data_source=daily.source,               # W33 诊断展示
        )
        # ---- 缓存写（W23）----
        if ck is not None:
            try:
                from ..store.cache import AnalysisCache, bars_fingerprint
                AnalysisCache(self.market.db).put(
                    result.symbol, result.trade_date, ck, result.to_dict(),
                    adjust=daily.adjust, rule_ver=result.rule_ver,
                    chan_enabled=chan_enabled(),
                    chan_score=chan_score_enabled(),
                    czsc_ver=czsc_ver, include_detail=include_detail,
                    fingerprint=bars_fingerprint(daily.bars))
            except Exception as e:
                logger.debug(f"[cache] 写缓存失败({symbol}): {str(e)[:80]}")
        return result


def analyze_one_stock(symbol: str, include_detail: bool = True,
                      cfg: Optional[Dict] = None,
                      use_cache: bool = True,
                      fill_financial: bool = True) -> AnalysisResult:
    """唯一分析入口（模块级便捷函数）。"""
    return AnalysisService(cfg).analyze_one_stock(
        symbol, include_detail=include_detail, use_cache=use_cache,
        fill_financial=fill_financial)
