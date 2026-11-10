"""mystery.services.analyze — 唯一分析入口。

对外只暴露 analyze_one_stock() → AnalysisResult。
Web / CLI / scan / 板块钻取全部走这里，保证同股同分（误差 ≤ 1）。
MYSTERY_CHAN_ENABLED 缺省 1（结构默认展示）；MYSTERY_CHAN_SCORE 缺省 0（混合分默认关），
评分仍与 stock_analyzer 1.22.30 完全兼容。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from ..adapters import market as _market
from ..adapters import sector as _sector
from ..adapters.codes import db_code_of
from ..config import load_config
from ..core.models import (AnalysisResult, BarSeries, ChanStructure,
                           MarketContext, MysteryBreakdown,
                           RULE_VER, RULE_VER_CHAN)
from ..core.mystery_rules import MysteryLogic
from ..core.patterns import PatternRecognition
from ..core import scorer as _scorer
# W76：覆盖层路径统一走共享模块（修 W74 Path("") 恒真导致路径退化成 "." 的缺陷）。
from .runtime_settings import RUNTIME_OVERRIDE_DIR, resolve_override_path

logger = logging.getLogger(__name__)

# W41 #18：降级路径可观测性——旧实现全走 debug，生产默认 WARNING 下整晚
# 数据源挂掉也不出现在管线日志（只有分数静默变 unknown）。这些"环境级
# 故障"升级为**每进程一次性 warning**（模块级标记防扫描逐票刷屏）。
_warned_once: set = set()


def _warn_once(key: str, msg: str) -> None:
    if key not in _warned_once:
        _warned_once.add(key)
        logger.warning(msg)

_INDEX_CODE = 'sh.000001'   # 上证指数


class MarketContextCache:
    """扫描批量预取的行业/财务缓存（W63）。

    在 _scan_worker 为整批 codes 一次性批量查询后构造，传入
    build_market_context 避免逐票查库。键为 db_code（与
    SectorClient.get_industry / AnalysisRepoMixin.get_financial 内部归一一致）。
    industries: {db_code: (sector_code, sector_name) 或 None(已知无主行业)}
    financials: {db_code: 财务 dict（可能为空 {}）}
    """
    __slots__ = ("industries", "financials")

    def __init__(self, industries=None, financials=None):
        self.industries = industries or {}
        self.financials = financials or {}


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


def multi_period_factor_enabled() -> bool:
    """多周期方向因子（G1）接入综合分开关：env MYSTERY_MPD_SCORE →
    config scoring.multi_period_factor（默认关，保持 1.22.30 金标可比）。"""
    v = os.environ.get('MYSTERY_MPD_SCORE')
    if v is not None:
        return v.strip().lower() in ('1', 'true', 'on', 'yes')
    return bool((load_config().get('scoring') or {}).get('multi_period_factor', False))


def pattern_weight_enabled() -> bool:
    """高信噪比形态轻加权（G4）开关：env MYSTERY_PATTERN_WEIGHT →
    config scoring.pattern_weight（默认关，保持 1.22.30 金标可比）。"""
    v = os.environ.get('MYSTERY_PATTERN_WEIGHT')
    if v is not None:
        return v.strip().lower() in ('1', 'true', 'on', 'yes')
    return bool((load_config().get('scoring') or {}).get('pattern_weight', False))


# W72/建议5+7：共振口径开关（service 层读 env，注入 core，保持 core 零 IO）。
# W73：生产默认口径由 PRODUCTION_RESONANCE_OPTIONS 定义（严格大盘腿 + 降阈值），
# core.DEFAULT_RESONANCE_OPTIONS 仍保留 W71 库级/离线基线，两者职责分离：
#   - core 默认值：离线单测、金标回放、库外调用方的稳定基线，绝不随生产策略漂移；
#   - service 生产默认：Web / CLI / scan / 日报实际生效口径，可经 env 逐项覆盖。
_RESONANCE_ENV_KEYS = (
    ('MYSTERY_RESONANCE_MARKET_MODE', 'market_mode'),          # ma | yearline
    ('MYSTERY_RESONANCE_TRUE_THRESHOLD', 'true_threshold'),
    ('MYSTERY_RESONANCE_CAPITAL_MIN', 'capital_min_score'),
    ('MYSTERY_RESONANCE_CAPITAL_VR', 'capital_min_volume_ratio'),
    ('MYSTERY_RESONANCE_INDEX_MAX_LAG_DAYS', 'index_max_lag_days'),
)

# W73 生产默认口径（用户拍板：严格大盘腿 + 降阈值）。取值依据见
# docs/W73_严格大盘腿与降阈值设为生产默认.md 的生产库金标对拍：
# yearline = 站年线 MA250 + MA5>MA20>MA60>MA250 多头顺次排列（更保守）；
# 80 = W72 已对拍过的降档，score 80~84 放行，且 gates 为 all() 不绕过高位硬门闩。
# 只需列出与 core 基线不同的项，其余由 resolve_resonance_options 补齐。
PRODUCTION_RESONANCE_OPTIONS: Dict[str, Any] = {
    "market_mode": "yearline",
    "true_threshold": 80.0,
    # 一键回退到 W71 口径：
    #   MYSTERY_RESONANCE_MARKET_MODE=ma MYSTERY_RESONANCE_TRUE_THRESHOLD=85
}


# W74：运行时覆盖层（网页「系统状态」面板 / CLI 可改，一键还原默认值）。
# 持久化到 JSON 文件；优先级：显式 env > 覆盖层文件 > 生产默认。
# 覆盖层为空/不存在时，effective 等于生产默认（与 W73 行为一致）。
# W76：修 W74 缺陷——原写法 Path("") 恒为 Path(".") 且布尔为真，
# 使生产路径退化成当前目录，exists() 恒 True、读取报 IsADirectoryError、
# 网页保存/还原失效。统一改用共享模块：包位置绝对目录 + 显式 env 重定向。
_OVERRIDE_DIR = RUNTIME_OVERRIDE_DIR
RESONANCE_OVERRIDE_PATH = resolve_override_path(
    "MYSTERY_RESONANCE_OVERRIDE", "resonance_override.json")

# 表单元数据：Web 面板与 CLI 复用，保证字段口径单一来源。
RESONANCE_FIELDS: tuple = (
    ("market_mode", "大盘腿口径", "select", ("ma", "yearline")),
    ("true_threshold", "真三振评分门闩", "number", (70.0, 100.0, 1.0)),
    ("capital_min_score", "资金活跃门槛(评分≥)", "number", (0.0, 20.0, 1.0)),
    ("capital_min_volume_ratio", "资金活跃门槛(量比≥)", "number", (0.0, 5.0, 0.1)),
    ("index_max_lag_days", "指数滞后降级天数(0=关闭)", "number", (0, 30, 1)),
)
RESONANCE_FIELDS_DICT: Dict[str, tuple] = {f[0]: f for f in RESONANCE_FIELDS}


def load_resonance_override() -> Dict[str, Any]:
    """W74：读取运行时覆盖层（不存在/损坏 → 空 dict，绝不抛错）。"""
    try:
        if not RESONANCE_OVERRIDE_PATH.exists():
            return {}
        data = json.loads(RESONANCE_OVERRIDE_PATH.read_text(encoding="utf-8"))
        return {k: v for k, v in (data or {}).items()
                if k in RESONANCE_FIELDS_DICT}
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取共振覆盖层失败，回退空: %s", exc)
        return {}


def save_resonance_override(opts: Dict[str, Any]) -> None:
    """W74：写入运行时覆盖层（只存已知字段，非法值由 core.resolve 兜底）。

    原子写（先 .tmp 再 replace），避免并发/崩溃留下半截 JSON 使下次读取失败。
    """
    clean: Dict[str, Any] = {k: opts[k] for k in RESONANCE_FIELDS_DICT if k in opts}
    RESONANCE_OVERRIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = RESONANCE_OVERRIDE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2, sort_keys=True),
                   encoding="utf-8")
    tmp.replace(RESONANCE_OVERRIDE_PATH)


def clear_resonance_override() -> None:
    """W74 一键还原默认值：删除覆盖层文件即可（下一次请求回到生产默认）。"""
    try:
        RESONANCE_OVERRIDE_PATH.unlink(missing_ok=True)
    except OSError as exc:  # noqa: BLE001
        logger.warning("删除共振覆盖层失败: %s", exc)


def resonance_override_exists() -> bool:
    """W74：覆盖层是否已自定义（供 Web 面板提示「已修改，可还原」）。"""
    return RESONANCE_OVERRIDE_PATH.exists()


def resonance_options_from_env() -> Dict[str, Any]:
    """读取 W72 共振口径 env（未设置的项不出现在返回 dict 中）。

    解析/合法性校验统一交给 ``core.resonance.resolve_resonance_options``，
    这里只做「env → dict」的搬运，避免在 service 层重复口径定义。
    W73：本函数仍只返回**显式覆盖项**；实际生效口径见
    ``resonance_options_effective``，保持 W72 的稀疏覆盖语义不变。
    """
    out: Dict[str, Any] = {}
    for env_key, opt_key in _RESONANCE_ENV_KEYS:
        v = os.environ.get(env_key)
        if v is not None and v.strip() != '':
            out[opt_key] = v.strip()
    return out


def resonance_options_effective() -> Dict[str, Any]:
    """W73/W74：实际生效口径 = 生产默认 + 覆盖层文件 + 显式 env（优先级递增）。

    - 未设覆盖层/env → 生产默认（yearline / 80）生效，Web/CLI/scan 全链路统一；
    - 网页「系统状态」面板保存 → 写入覆盖层文件，下次请求热生效、无需重启；
    - 显式 env（运维应急）最高优先，可覆盖网页设置；
    - 返回稀疏 dict，交由 core.resolve_resonance_options 归一化补齐，core 零 IO。
    """
    out = dict(PRODUCTION_RESONANCE_OPTIONS)
    out.update(load_resonance_override())          # W74 网页/CLI 自定义
    out.update(resonance_options_from_env())       # 显式 env 最高优先
    return out


def resonance_options_default() -> Dict[str, Any]:
    """W74：当前生产默认口径（网页面板「还原默认值」的目标态）。"""
    return dict(PRODUCTION_RESONANCE_OPTIONS)


def resonance_cache_tag() -> str:
    """W73：口径入缓存键（W23「改任何一项必 miss」契约）。

    W72 时「默认口径 → tag 为空」，用于保证默认零变化、既有缓存零失效。
    但 **生产默认一旦改变，空 tag 会让旧 ma/85 缓存被 yearline/80 命中**，
    属口径污染。故 W73 起改为：tag 恒基于「实际生效口径」生成、恒非空：

    - 旧缓存（W72 及以前，无 ``|R[...]`` 后缀）不会被新口径命中 → 自动失效重算；
    - 不同口径（含显式回退 ma/85）各有独立键空间，不互相污染；
    - 走 ``core.resolve_resonance_options`` 归一化，``80`` 与 ``80.0``、
      非法值回退到基线等都得到同一个 tag，避免无谓 miss。
    """
    from ..core.resonance import resolve_resonance_options
    norm = resolve_resonance_options(resonance_options_effective())
    sig = '|'.join(f'{k}={norm[k]}' for k in sorted(norm))
    return f'|R[{sig}]'


def market_env_summary(market) -> Dict[str, Any]:
    """大盘滤网快照（W37，单一实现，四方展示只读调用）。

    双锚（上证 000001.SH + 国证1000 399311.SZ；同花顺全A/中证全指实测
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
            _warn_once(f"anchor:{code}", f"[大盘锚] {code} 获取失败: {str(e)[:60]}（该锚 env 判定按缺数据降级）")
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
    W41：上界 80 显式判（与 _turn_opt/治理口径同源，防未来旁路构造的 Bar
    绕过 market 层清洗直接带脏值进均值）。
    W47（外部 review P0#1）：有效根数 < MYSTERY_TURNOVER_20_MIN_VALID（默认
    15）时返回 None——20 根里只有 2~3 根有效时均值由残值决定，W43 假 0 洗成
    NULL 后这类票更容易被误打 chip_low；宁缺毋假，消费方（scan_signals）本
    就按 None 走 chip_low_unknown，语义安全。
    """
    min_valid = max(1, int(os.environ.get('MYSTERY_TURNOVER_20_MIN_VALID',
                                          '15')))
    vals = []
    for b in daily.bars[-20:]:
        t = b.turnover
        if t is None:
            continue
        try:
            f = float(t)
        except (TypeError, ValueError):
            continue
        if 0 < f < 80:
            vals.append(f)
    if len(vals) < min_valid:
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


def _candlestick_result(daily, weekly) -> Dict[str, Any]:
    """W82：唯一分析入口内附加形态结论；失败返回空 dict，绝不影响评分链路。"""
    try:
        from .candlestick_scan import analyze_candlestick
        return analyze_candlestick(daily, weekly)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[candlestick] 形态分析跳过: {str(exc)[:80]}")
        return {}


def _result_from_payload(data: dict) -> AnalysisResult:
    """analysis_cache payload（AnalysisResult.to_dict 形态）→ AnalysisResult。

    chan 结构用 chan_from_dict 还原；任何组件缺失按默认值容错（缓存是加速
    层，宁可弱还原也不抛——消费方实际只用 to_dict 字段）。
    """
    from ..adapters.czsc_adapter import chan_from_dict
    chan = {f: chan_from_dict(d) for f, d in (data.get('chan') or {}).items()}
    # W47（外部 review P0#4）：payload 多未知键不再 TypeError（规则字段演进
    # 后旧缓存整池静默重算、只表现为偶发变慢）。只取 dataclass 已知字段。
    from dataclasses import fields as _dc_fields
    known = {f.name for f in _dc_fields(MysteryBreakdown)}
    mystery_data = data.get('mystery') or {}
    mystery = MysteryBreakdown(
        **{k: v for k, v in mystery_data.items() if k in known})
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
        mystery=mystery,
        chan=chan,
        sector=data.get('sector') or {},
        financial=data.get('financial') or {},
        rule_ver=data.get('rule_ver', ''),
        czsc_ver=data.get('czsc_ver', ''),
        bars_fingerprint=data.get('bars_fingerprint', ''),
        data_source=data.get('data_source', ''),
        candlestick=data.get('candlestick') or {},
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
                             fill_financial: bool = True,
                             ctx_cache: Optional['MarketContextCache'] = None
                             ) -> MarketContext:
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
            _warn_once("index", f"[ctx] 大盘指数获取失败: {str(e)[:60]}"                     "（共振判定按无指数降级）")
        # W63：批量预取路径（扫描链）直接取预计算，跳过逐票库查询；
        # 指数已有会话级缓存、行业 K 线已实例级缓存，均不重复查。
        db_code = (db_code_of(symbol) if not symbol.startswith(('sh.', 'sz.', 'bj.'))
                   else symbol)
        if ctx_cache is not None:
            ind = self.sector.get_industry(
                symbol, primary=ctx_cache.industries.get(db_code))
        else:
            ind = self.sector.get_industry(symbol)
        ctx.industry_name = ind.get('name') or '未知'
        ctx.industry_score = ind.get('score')
        ctx.industry_up = ind.get('up')
        if ctx_cache is not None:
            ctx.financial = ctx_cache.financials.get(db_code) or {}
        else:
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
                _warn_once("fin_fill", f"[ctx] 财务在线补齐失败: {str(e)[:60]}（后续票财务字段继续走本地，估值项按缺数降级）")
        return ctx

    # ---------------- 规则明细（收口到 core.pipeline 纯计算） ----------------
    def run_rules(self, daily: BarSeries, weekly: Optional[BarSeries],
                  monthly: Optional[BarSeries], ctx: MarketContext,
                  include_detail: bool) -> MysteryBreakdown:
        from ..core import pipeline as _pipe

        # W73：service 层把「生产默认口径 + 显式 env 覆盖」注入 core（core 零 IO）。
        # 未设任何 env 时即为 yearline/80 生产默认；显式 env 可逐项覆盖或完整回退。
        return _pipe.run_mystery(daily, weekly, monthly, ctx,
                                 include_detail=include_detail, logic=self.logic,
                                 patterns=self.patterns,
                                 resonance_options=resonance_options_effective())

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
                          fill_financial: bool = True,
                          ctx_cache: Optional['MarketContextCache'] = None
                          ) -> AnalysisResult:
        """单票完整分析（CLAUDE.md §7.4 伪代码）。

        W23：use_cache=True 时头尾走 analysis_cache（键含开关/版本/K线指纹，
        改任何一项必 miss；sync 写该票 K 线时同事务删除旧缓存）。
        W36 P1-1：fill_financial=False（扫描链）时不在线补财务，缺则 unknown。
        """
        daily = self.market.fetch_bars(symbol, '1d')
        if not daily.bars:
            raise RuntimeError(f"[{symbol}] 无日K数据（本地库/在线源均失败）")
        trade_date = str(daily.bars[-1].dt)[:10]
        # W优化⑥：日K指纹只算一次（O(n) vol_sum 遍历），缓存键/结果/落库
        # 三处复用，避免全市场冷缓存扫描时每票 3× 重复计算。
        from ..store.cache import bars_fingerprint
        fp = bars_fingerprint(daily.bars)
        # ---- 缓存读（W23）----
        ck = None
        # 010.md 6C：混合分路径（结构开+分开关）用新规则口径 ver；分关不变
        mix = chan_enabled() and chan_score_enabled()
        rule = RULE_VER_CHAN if mix else RULE_VER
        if use_cache and cache_allowed(daily):
            try:
                from ..store.cache import AnalysisCache, make_cache_key
                from ..adapters.czsc_adapter import czsc_version
                ver = czsc_version() if chan_enabled() else ''
                # W36 P1-1：F 位入键——扫描链(F=0)缺 ROE 的 unknown 结果
                # 不得被个股详情(F=1,在线补齐)误命中，反之亦然。
                flags = (f"D{int(include_detail)}|C{int(chan_enabled())}"
                         f"|S{int(chan_score_enabled())}|F{int(fill_financial)}")
                # W73：口径恒入缓存键（tag 非空）。旧 ma/85 缓存无此后缀，
                # 不会被 yearline/80 命中，避免切换默认后的口径污染。
                flags += resonance_cache_tag()
                # W82：形态口径恒入键（网页勾选/窗口/日周开关变更后旧缓存必 miss）
                from mystery.services.runtime_settings import candle_cache_tag
                flags += candle_cache_tag()
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
                                        fill_financial=fill_financial,
                                        ctx_cache=ctx_cache)

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
                                                  mix_enabled=mix,
                                                  mpd_enabled=multi_period_factor_enabled(),
                                                  pattern_weight=pattern_weight_enabled())
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
            rule_ver=rule,
            czsc_ver=czsc_ver,
            bars_fingerprint=fp[:8],   # W33 诊断展示（W优化⑥：复用单次计算）
            data_source=daily.source,               # W33 诊断展示
            # W82：日 K / 周 K 分别做典型组合形态识别（仅展示，不进评分）
            candlestick=_candlestick_result(daily, weekly),
        )
        # ---- 缓存写（W23）----
        if ck is not None:
            try:
                from ..store.cache import AnalysisCache
                AnalysisCache(self.market.db).put(
                    result.symbol, result.trade_date, ck, result.to_dict(),
                    adjust=daily.adjust, rule_ver=result.rule_ver,
                    chan_enabled=chan_enabled(),
                    chan_score=chan_score_enabled(),
                    czsc_ver=czsc_ver, include_detail=include_detail,
                    fingerprint=fp)
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
