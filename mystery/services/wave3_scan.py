"""mystery.services.wave3_scan — 三浪主升策略扫描编排（只读）。

职责边界（AGENTS.md §2.2 / §2.10）
----------------------------------
- **只读**：不写 ``scan_jobs`` / ``scan_results``，不参与 W25 扫描互斥锁，
  不触碰 ``analysis_cache``；与 ``czsc-mi scan`` 的写库职责完全隔离。
- **不评分**：只调用 ``mystery.core.wave3`` 的过滤/风控纯函数，不调用
  ``analyze_one_stock``、不产出综合评分，因此不构成第二套计分。
- **数据口径**：默认纯本地日 K（零网络、零写库），周线/月线由**本轮同一份
  日 K**经 ``MarketDataClient.resample_bars`` 派生（与 analyze/scoring 同口径，
  遵守「一轮分析只锁一种复权 qfq、不为规则另读一遍日 K」）。
  可选 ``offline=False`` 走统一多源出口 ``fetch_bars``（其在线命中回写 K 线
  缓存是既有数据层机制，非本策略新增写库）。
- 名称取自证券表，缺失显示「未知」（§2.9 有代码必有名称）。

输出为纯 dict（JSON 可序列化），由 CLI / 报表 / Web 展示层消费。
"""
from __future__ import annotations

import logging
import math
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..adapters.codes import db_code_of, normalize_symbol
from ..adapters.market import MarketDataClient
from ..core import wave3
from ..core.models import Bar, BarSeries
from .runtime_settings import scan_wave3_options_effective

logger = logging.getLogger(__name__)

DEFAULT_EQUITY = 1_000_000.0
DEFAULT_RISK_PCT = 0.01


def _num(v: Any) -> Optional[float]:
    """DataFrame 单元格 → float（NaN/Inf/空 → None）。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f


def _daily_from_db(db: Any, symbol: str) -> Optional[BarSeries]:
    """纯本地库日 K → BarSeries（不触发任何在线降级或缓存回写）。"""
    code = db_code_of(symbol)
    df = db.load_kline(code, 'daily')
    if df is None or getattr(df, "empty", True):
        return None
    n = len(df)

    def col(name: str) -> List[Any]:
        return df[name].tolist() if name in df.columns else [None] * n

    dts = [str(x)[:10] for x in col('date')]
    opens, highs, lows = col('open'), col('high'), col('low')
    closes, volumes = col('close'), col('volume')
    amounts, pcts = col('amount'), col('pctChg')
    bars = [
        Bar(dt=dts[i], open=_num(opens[i]), high=_num(highs[i]),
            low=_num(lows[i]), close=_num(closes[i]),
            volume=_num(volumes[i]) or 0.0,
            amount=_num(amounts[i]) or 0.0,
            turnover=None, pct_chg=_num(pcts[i]) or 0.0)
        for i in range(n)
    ]
    return BarSeries(symbol=normalize_symbol(symbol), freq='1d',
                     adjust='qfq', bars=bars, source='db')


def _records(series: Optional[BarSeries]) -> List[Dict[str, Any]]:
    """BarSeries → core 层可消费的 dict 列表。"""
    if series is None or not series.bars:
        return []
    return [b.to_dict() for b in series.bars]


def _returns(records: List[Dict[str, Any]]) -> Dict[str, float]:
    """由日 K 收盘价算日收益（按日期对齐，供相关性矩阵使用）。"""
    out: Dict[str, float] = {}
    prev: Optional[float] = None
    for r in records:
        c = wave3._f(r.get('close'))
        d = str(r.get('dt') or '')
        if c is None or c <= 0 or not d:
            continue
        if prev is not None and prev > 0:
            out[d] = c / prev - 1.0
        prev = c
    return out


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning(f"[wave3] 环境变量 {name} 非数值，使用默认值 {default}")
        return default


def _resolve_symbols(db: Any, symbols: Optional[List[str]],
                     watchlist: bool, market: bool,
                     limit: Optional[int]) -> List[str]:
    if symbols:
        out = [normalize_symbol(str(s)) for s in symbols]
    elif watchlist or not market:
        from . import watchlist as _wl
        out = [normalize_symbol(str(s)) for s in _wl.load_watchlist()]
    else:
        out = []
        for row in db.get_stock_list() or []:
            code = row.get('code') if isinstance(row, dict) else (row[0] if row else None)
            if code:
                try:
                    out.append(normalize_symbol(str(code)))
                except Exception:
                    continue
    if limit and limit > 0:
        out = out[:limit]
    return out


def scan_wave3(symbols: Optional[List[str]] = None,
               watchlist: bool = True, market: bool = False,
               limit: Optional[int] = None,
               equity: Optional[float] = None,
               risk_pct: Optional[float] = None,
               cfg: Optional[Dict] = None, strict: bool = False,
               offline: bool = True, lot_size: int = 100,
               max_position_pct: Optional[float] = None,
               max_total_risk_pct: float = wave3.MAX_PORTFOLIO_RISK_PCT,
               high_corr: float = wave3.HIGH_CORR,
               min_bars: int = 60) -> Dict[str, Any]:
    """执行三浪主升策略扫描，返回组合层评估结果（纯 dict）。

    :param watchlist: 默认扫自选股；``market=True`` 显式扫全市场（配合 limit）。
    :param offline: 默认 True = 只读本地 K 线；False = 走统一多源出口。
    :param equity/risk_pct: 缺省取 env ``MYSTERY_WAVE3_EQUITY`` /
        ``MYSTERY_WAVE3_RISK_PCT``，再回退 1,000,000 / 1%。
    """
    eq = float(equity) if equity is not None else _env_float(
        'MYSTERY_WAVE3_EQUITY', DEFAULT_EQUITY)
    rp = float(risk_pct) if risk_pct is not None else _env_float(
        'MYSTERY_WAVE3_RISK_PCT', DEFAULT_RISK_PCT)
    client = MarketDataClient(cfg)
    db = client.db
    codes = _resolve_symbols(db, symbols, watchlist, market, limit)
    default_min = int(wave3.DEFAULTS["MIN_BARS"])
    params: Optional[Dict] = ({"MIN_BARS": min_bars}
                              if min_bars != default_min else None)

    items: List[Dict[str, Any]] = []
    returns_by_symbol: Dict[str, Any] = {}
    failed: List[Dict[str, str]] = []
    for sym in codes:
        try:
            daily = (_daily_from_db(db, sym) if offline
                     else client.fetch_bars(sym, '1d'))
            if daily is None or not daily.bars:
                failed.append({"symbol": sym, "reason": "无日K数据"})
                continue
            daily_rec = _records(daily)
            if len(daily_rec) < min_bars:
                failed.append({"symbol": sym,
                               "reason": f"日K仅{len(daily_rec)}根，不足{min_bars}根"})
                continue
            weekly = client.resample_bars(daily, '1w')
            monthly = client.resample_bars(daily, '1M')
            try:
                name = db.get_stock_name(sym) or "未知"
            except Exception:
                name = "未知"
            items.append({
                "symbol": sym, "name": name,
                "daily": daily_rec,
                "weekly": _records(weekly),
                "monthly": _records(monthly),
                "params": params,
            })
            returns_by_symbol[sym] = _returns(daily_rec)
        except Exception as e:  # 单票失败不中断整轮扫描
            logger.warning(f"[wave3] {sym} 取数失败: {str(e)[:100]}")
            failed.append({"symbol": sym, "reason": str(e)[:100]})

    result = wave3.evaluate_portfolio(
        items, equity=eq, risk_pct=rp,
        returns_by_symbol=returns_by_symbol, strict=strict,
        lot_size=lot_size, max_position_pct=max_position_pct,
        max_total_risk_pct=max_total_risk_pct, high_corr=high_corr)
    result["meta"] = {
        "source": "db" if offline else "multi",
        "offline": bool(offline),
        "generated_at": datetime.now().isoformat(timespec='seconds'),
        "universe": "symbols" if symbols else ("watchlist" if (watchlist or not market)
                                               else "market"),
        "requested": len(codes), "loaded": len(items), "failed": failed,
        "min_bars": min_bars,
    }
    return result


# ---------------------------------------------------------------------------
# W69：把三浪主升结论合并进既有扫描结果（czsc-mi scan 的 results）
# ---------------------------------------------------------------------------

def _wave3_compact(a: Dict[str, Any]) -> Dict[str, Any]:
    """assessment → 精简字段（约 300B/票，避免 payload_json 被完整评估撑大）。"""
    pos = a.get("positions", {}) or {}
    wave = a.get("wave", {}) or {}
    sizing = a.get("sizing", {}) or {}
    rules = a.get("rules", {}) or {}
    wf = a.get("workflow", {}) or {}
    return {
        "verdict": a.get("verdict", "回避"),
        "phase": wave.get("phase"),
        "position": pos.get("位置摘要"),
        "resonance": pos.get("共振"),
        "trend_ok": bool(pos.get("趋势允许")),
        "zone": {k: (pos.get(k) or {}).get("位置")
                 for k in ("月线", "周线", "日线")},
        "stage": wf.get("stage"),
        "stage_name": wf.get("stage_name"),
        "hard": f"{rules.get('hard_pass', 0)}/{rules.get('hard_total', 0)}",
        "quality": rules.get("rule_quality"),
        "failed_hard": rules.get("failed_hard", []),
        "entry": wave.get("entry_price"),
        "stop": wave.get("stop_price"),
        "lots": sizing.get("suggested_lots"),
        "shares": sizing.get("suggested_shares"),
        "risk_amount": sizing.get("suggested_risk_amount"),
        "scale_down": sizing.get("scale_down"),
        "reason": a.get("verdict_reason"),
    }


def _wave3_portfolio_summary(result: Dict[str, Any]) -> Dict[str, Any]:
    """evaluate_portfolio → 组合摘要。

    同一份摘要挂到每条结果（``wave3_portfolio``），保证按信号过滤后任取
    一行仍能读到组合总风险；对象共享，JSON 序列化时重复但内存只占一份。
    """
    p = result.get("portfolio", {}) or {}
    acct = result.get("account", {}) or {}
    return {
        "scanned": result.get("scanned", 0),
        "candidates": result.get("candidates", 0),
        "equity": acct.get("equity"),
        "risk_pct": acct.get("risk_pct"),
        "sum_risk": p.get("sum_risk"),
        "portfolio_risk": p.get("portfolio_risk"),
        "total_risk_pct": p.get("total_risk_pct"),
        "scale_down": p.get("scale_down"),
        "high_corr_pairs": len(p.get("high_corr_pairs") or []),
        "assumed_pairs": len(p.get("assumed_pairs") or []),
        "matrix_psd": p.get("matrix_psd"),
        "max_total_risk_pct": p.get("max_total_risk_pct"),
        "note": p.get("note", ""),
        "disclaimer": wave3.DISCLAIMER,
    }


def attach_wave3_to_results(results: List[Dict[str, Any]],
                            cfg: Optional[Dict] = None,
                            *, db: Any = None,
                            client: Any = None,
                            progress_cb: Any = None) -> Dict[str, Any]:
    """W69：给 ``scan_market`` 的 results **原地**附加三浪主升字段（只读、失败降级）。

    在写库前调用，字段随 ``payload_json`` 一起落库，日报 / Web /
    ``scan_results_of`` 无需改动即可读到。整体异常硬隔离：任何失败都不得
    影响既有扫描结果（此时 results 保持未附加状态）。

    环境开关：

    - ``MYSTERY_SCAN_WAVE3``：默认 ``1`` 开启；置 ``0`` 跳过（回到旧行为）。
    - ``MYSTERY_SCAN_WAVE3_MAX``：默认 ``0`` 不限；>0 时只评估按分降序的
      前 N 只，其余标「未评估」，防止超大股票池拖慢扫描。**显式设置时完全
      尊重用户语义**（含 ``0``=强制全量）。
      W76：这些参数除 env 外，也可由网页「系统状态」面板写入覆盖层文件，
      统一经 ``runtime_settings.scan_wave3_options_effective()`` 读取，
      Web / CLI / 日报同口径、热生效；优先级 env > 覆盖层 > 代码默认。
    - ``MYSTERY_SCAN_WAVE3_AUTO_TRIGGER`` / ``MYSTERY_SCAN_WAVE3_AUTO_MAX``：
      W75 自动限流（仅当 ``MYSTERY_SCAN_WAVE3_MAX`` **未显式设置**时生效）。
      results 数 > trigger（默认 500）时，自动只评估按分降序前 auto_max
      （默认 200）只，其余标「未评估」。自选股等小池行为**完全不变**。
    - ``MYSTERY_SCAN_WAVE3_STRICT``：默认 ``0``；置 ``1`` 把第 4/6 条升硬项。
    - ``MYSTERY_WAVE3_EQUITY`` / ``MYSTERY_WAVE3_RISK_PCT``：复用 W68 口径。

    W75 背景：W69 默认 ``MAX=0`` 不限，全市场 5580 只扫描在逐票
    ``done=total`` 后会进入收尾——对全部标的逐只查库 + resample + 三浪
    评估、候选间再算 O(C²) 相关性矩阵，耗时十几分钟且内存数 GB，而收尾
    阶段不更新进度心跳，页面表现为「一直卡在最后」。自动限流把超大池
    收尾压到秒级，core 零改动、不触碰评分/信号语义。

    W88：改为**流式逐票评估**——每只票取数/评估后立即释放完整日周月 K，
    只保留精简 assessment，再由 ``evaluate_portfolio(evaluated=...)`` 统一
    做组合层。因此 ``MAX=0`` 强制全量 5579 只也不再爆内存，且组合层同一段
    代码、逐字段口径不变；自动限流从此只是「想省时间」的可选项，不再是
    避免 OOM 的必需手段。

    :return: 组合摘要 dict（关闭 / 无结果 / 异常时 ``enabled=False``）。
    """
    off: Dict[str, Any] = {"enabled": False, "scanned": 0, "candidates": 0}
    if not results:
        return off
    # W76：口径统一入口（网页覆盖层 + 显式 env），稀疏 dict；未设置项留空
    # 让下面各 ``eff.get(name, 默认)`` 回落到 W69/W75 代码默认，语义零变化。
    eff = scan_wave3_options_effective()
    if eff.get("MYSTERY_SCAN_WAVE3", "1").strip() in \
            ("0", "false", "False", "no", "off"):
        return off

    def _env_int(name: str, default: int) -> int:
        try:
            return int(eff.get(name, str(default)) or default)
        except (ValueError, TypeError):
            return default

    # W75：自动限流仅作用于「未显式设置 MAX」的超大池；显式设置完全尊重。
    # W76：MAX 可来自 env 或网页覆盖层，两者都算「显式」。
    explicit_raw = eff.get("MYSTERY_SCAN_WAVE3_MAX")
    auto_limited = False
    if explicit_raw is not None and explicit_raw.strip() != "":
        max_n = _env_int("MYSTERY_SCAN_WAVE3_MAX", 0)
        cap_reason = "explicit"
    else:
        trigger = _env_int("MYSTERY_SCAN_WAVE3_AUTO_TRIGGER", 500)
        auto_max = _env_int("MYSTERY_SCAN_WAVE3_AUTO_MAX", 200)
        # W88：auto_max >= 池大小时「取前 N 只」等于全量，不应标成限流。
        # 线上曾把 AUTO_TRIGGER/AUTO_MAX 都设成 5600 而池只有 5580，
        # 旧判断虽未限流却误报 auto_limited=True（UI 显示「仅评估前 5600 只」）。
        if len(results) > trigger and 0 < auto_max < len(results):
            max_n = auto_max
            auto_limited = True
            cap_reason = "auto"
        else:
            max_n = 0
            cap_reason = "none"
    strict = eff.get("MYSTERY_SCAN_WAVE3_STRICT", "0").strip() in \
        ("1", "true", "True", "yes")
    eq = _env_float("MYSTERY_WAVE3_EQUITY", DEFAULT_EQUITY)
    rp = _env_float("MYSTERY_WAVE3_RISK_PCT", DEFAULT_RISK_PCT)
    min_bars = int(wave3.DEFAULTS["MIN_BARS"])

    order = sorted(range(len(results)),
                   key=lambda i: (results[i].get("score") is not None,
                                  float(results[i].get("score") or -1)),
                   reverse=True)
    picked = order[:max_n] if max_n > 0 else order
    picked_set = set(picked)

    try:
        import warnings
        _client = client or MarketDataClient(cfg)
        _db = db or _client.db
        # W88 流式逐票评估：任意时刻只在内存保留「一只票」的日/周/月 K，
        # 评完立即释放大对象，只留下 KB 级 assessment；收益序列只为候选保留。
        # 此前把 5579 只完整 K 线全堆进 items 再一次性交给 evaluate_portfolio，
        # 峰值内存数 GB，是 11-10 / 11-11 / 11-12 三次全量扫描卡死在
        # 「收尾·三浪主升评估」的决定性原因。组合层仍走同一个函数，口径零变化。
        assessments: List[Dict[str, Any]] = []
        cand_returns: Dict[str, Any] = {}
        row_by_sym: Dict[str, Dict[str, Any]] = {}
        status: Dict[str, str] = {}
        n_picked = len(picked)
        n_loaded = 0
        for k, i in enumerate(picked):
            # W75：收尾阶段逐票上报，使页面进度条不再停在 100% 空转。
            # 上报的是「收尾」独立刻度（不与逐票扫描的 total 混用）。
            if progress_cb is not None:
                try:
                    progress_cb(k, n_picked, "收尾·三浪主升评估")
                except Exception:
                    pass
            d = results[i]
            raw = str(d.get("symbol") or "")
            try:
                sym = normalize_symbol(raw)
            except Exception:
                sym = raw
            row_by_sym[sym] = d
            daily = daily_rec = weekly = weekly_rec = None
            monthly = monthly_rec = assessment = None
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    daily = _daily_from_db(_db, sym)
                if daily is None or not daily.bars:
                    status[sym] = "无日K数据"
                    continue
                daily_rec = _records(daily)
                if len(daily_rec) < min_bars:
                    status[sym] = f"日K仅{len(daily_rec)}根，不足{min_bars}根"
                    continue
                weekly = _client.resample_bars(daily, "1w")
                monthly = _client.resample_bars(daily, "1M")
                weekly_rec = _records(weekly)
                monthly_rec = _records(monthly)
                assessment = wave3.evaluate(
                    daily_rec, weekly_rec, monthly_rec,
                    equity=eq, risk_pct=rp, symbol=sym,
                    name=str(d.get("name") or ""), strict=strict)
                # 完整 assessment 保留（约 KB 级/票，5579 只合计百 MB 内），
                # 交给组合层统一重算 scale_down 后的仓位，保证逐字段同口径。
                assessments.append(assessment)
                n_loaded += 1
                if assessment["verdict"] == "候选":
                    # 收益序列只为候选保留（相关性矩阵只统计候选），非候选的
                    # 数千个日期 dict 不再白白堆在内存里。
                    cand_returns[sym] = _returns(daily_rec)
            except Exception as e:  # 单票取数/评估失败不中断整轮
                status[sym] = str(e)[:80]
                continue
            finally:
                # 显式断开大对象引用：CPython 引用计数立即释放本票 K 线，
                # 峰值内存不再随股票池线性增长（5579×~1MB → 单票 ~1MB）。
                daily = daily_rec = weekly = weekly_rec = None
                monthly = monthly_rec = assessment = None

        # 组合层只记日志，不再额外发一次 progress_cb：W75 既有契约是
        # 「收尾逐票回调恰好 picked 次、stage 恒为三浪评估」，新增阶段回调会
        # 打破该契约（也让 600 只自动限流用例的 200 次断言失败）。流式逐票
        # 评估本身已持续上报心跳，不会 600s 静默，无需额外阶段上报。
        logger.info(f"[wave3] W88 流式收尾：{n_loaded}/{n_picked} 只已评估，"
                    f"候选 {len(cand_returns)} 只进入组合风险合成（K 线逐票释放）")
        # 组合层传入已评估的 assessment 列表（不含 K 线大对象），仍走同一个
        # evaluate_portfolio：筛候选 / 相关性 / 风险 / 等比缩放逐字段同口径，
        # 且被注入异常时（W69 全局降级回归）仍走同一个异常边界、行为一致。
        res = wave3.evaluate_portfolio(
            evaluated=assessments,
            equity=eq, risk_pct=rp, returns_by_symbol=cand_returns,
            strict=strict)
        summary = _wave3_portfolio_summary(res)
        summary.update({"enabled": True, "strict": strict,
                        "requested": len(picked), "loaded": n_loaded,
                        "failed": len(status),
                        # W75：限流可观测——UI/日报/日志能看出本轮是自动限流
                        "auto_limited": auto_limited, "cap_reason": cap_reason,
                        "cap": max_n if max_n > 0 else None,
                        "pool_size": len(results)})
        if auto_limited:
            logger.info(
                f"[wave3] W75 超大池自动限流生效：{len(results)} 只只评估"
                f"按分降序前 {max_n} 只（其余标未评估），避免收尾卡顿；"
                f"设 MYSTERY_SCAN_WAVE3_MAX=0 可强制全量")

        # 全部算完再统一赋值：中途异常不会污染部分行。evaluate_portfolio
        # 已对所有 assessment 原地重算 scale_down 后的仓位（含非候选置零）。
        compact_by_sym = {a["symbol"]: _wave3_compact(a)
                          for a in res.get("results", [])}
        assigns: List[tuple] = []
        for sym, compact in compact_by_sym.items():
            d = row_by_sym.get(sym)
            if d is not None:
                assigns.append((d, compact))
        for sym, reason in status.items():
            d = row_by_sym.get(sym)
            if d is not None:
                assigns.append((d, {"verdict": "回避", "reason": reason,
                                    "data_ok": False}))
        if max_n > 0:
            if auto_limited:
                cap_msg = (f"W75 自动限流：{len(results)} 只超大池仅评估前 "
                           f"{max_n} 只（MYSTERY_SCAN_WAVE3_MAX 未设置时自动）")
            else:
                cap_msg = f"超过 MYSTERY_SCAN_WAVE3_MAX={max_n} 上限"
            for i in range(len(results)):
                if i not in picked_set:
                    assigns.append((results[i], {
                        "verdict": "未评估", "data_ok": False,
                        "reason": cap_msg}))
        for d, compact in assigns:
            d["wave3"] = compact
            d["wave3_portfolio"] = summary
        return summary
    except Exception as e:  # noqa: BLE001 附加策略不得影响既有扫描
        logger.warning(f"[wave3] 合并到扫描结果失败（不影响既有扫描）: "
                       f"{str(e)[:120]}")
        return {**off, "error": str(e)[:120]}


def wave3_summary_line(results: List[Dict[str, Any]]) -> str:
    """W69：从扫描结果取组合摘要，生成一行可打印文本（旧任务无字段返回 ''）。

    供 CLI ``scan``、日报 QA 行、Web 扫描结果表共用，保证三处口径一致。
    """
    p = None
    for r in (results or []):
        if isinstance(r, dict) and r.get("wave3_portfolio"):
            p = r["wave3_portfolio"]
            break
    if not p or not p.get("enabled"):
        return ""
    pct = p.get("total_risk_pct")
    risk = p.get("portfolio_risk") or 0
    if pct is None and risk == 0:
        pct = 0.0  # 零敞口时明确 0.0%，避免显示含糊的 '-'
    parts = [
        f"候选 {p.get('candidates', 0)}/{p.get('scanned', 0)}",
        f"组合总风险 {p.get('portfolio_risk')} 元",
        f"占权益 {pct}%" if pct is not None else "占权益 -",
        f"仓位缩放 ×{p.get('scale_down')}",
    ]
    if p.get("high_corr_pairs"):
        parts.append(f"高相关 {p['high_corr_pairs']} 对")
    if p.get("matrix_psd") is False:
        parts.append("相关矩阵非半正定(风险已截断)")
    # W75：超大池自动限流可观测——日报/Web/CLI 明确显示本轮只评估了前 N 只，
    # 避免用户误以为「候选少=没信号」。
    if p.get("auto_limited"):
        parts.append(f"超大池自动限流(评估前 {p.get('cap')}/{p.get('pool_size')} 只)")
    cap = p.get("max_total_risk_pct")
    if (p.get("scale_down") or 1.0) < 1.0:
        cap_txt = f"{cap * 100:.1f}%" if isinstance(cap, (int, float)) else "风险预算"
        parts.append(f"超出 {cap_txt}，仓位已等比下调")
    # 不追加 portfolio.note：其中数字已由上列覆盖，避免重复冗长
    line = "三浪主升 · " + " · ".join(str(x) for x in parts)
    line += f" · {p.get('disclaimer') or wave3.DISCLAIMER}"
    return line
