"""mystery.services.minute_macd — 自选股分钟K线同步 + MACD金叉死叉监控（W103）。

设计要点（用户拍板）：
- 仅处理自选股（data/watchlist.json），不碰全市场；
- 只分析 MACD，不跑缠论/共振/评分等重逻辑，避免资源浪费；
- 只上报「金叉/死叉变更」：与上次状态比对，状态翻转才通知；首次运行
  全部上报当前状态（用户选「首次全部上报」）。
- 30min K线存独立表（最近20天）；60min 由 30min 聚合，不落盘。
- 通知走统一飞书 webhook（mystery.services.feishu，纯标准库），不依赖 Hermes/gateway。
- W113：通知按优先级分组——持仓死叉 > 持仓金叉 > 强势自选(站上5周均线)金叉 >
  其他自选金叉 > 其他自选死叉；5周均线由日K重采样派生，不读滞后的周线表。
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Dict, List, Optional

from ..adapters import minute_source as ms
from ..core.macd import macd, summarize
from ..services.feishu import send_text as _feishu_send
from ..store._repos.min30_repo import Min30Repo

logger = logging.getLogger(__name__)

_MIN_BARS = 60  # MACD 最少根数（<60 跳过，避免预热不足误报）


def _name_of(symbol: str, db=None) -> str:
    try:
        if db is None:
            from ..store.db import MysteryDB
            db = MysteryDB()
        return db.get_stock_name(symbol) or ""
    except Exception:
        return ""


def sync_watchlist_min30(repo: Optional[Min30Repo] = None,
                         codes: Optional[List[str]] = None,
                         verbose: bool = True) -> Dict:
    """同步自选股 30min K线到独立表（增量：只写 last_dt 之后的新bar）。"""
    repo = repo or Min30Repo()
    if codes is None:
        from . import watchlist as wl
        codes = wl.load_watchlist()
    ok, fail, written = 0, 0, 0
    t0 = time.time()
    for code in codes:
        try:
            got_any = False
            # 30m（保留20天）+ 60m（保留40天，MACD需~80根）
            for period, freq in (("30m", "minute30"), ("60m", "hour")):
                bars = ms.fetch_minute_bars(code, freq)
                if not bars:
                    continue
                last = repo.last_dt(code, period)
                if last:
                    bars = [b for b in bars if b["dt"] > last]
                written += repo.upsert_bars(code, bars, period)
                got_any = True
            if got_any:
                ok += 1
            else:
                fail += 1
        except Exception as e:
            logger.debug(f"[min30] sync {code} 失败: {str(e)[:80]}")
            fail += 1
    pruned30 = repo.prune(period="30m")
    pruned60 = repo.prune(period="60m")
    return {"codes_total": len(codes), "ok": ok, "fail": fail,
            "bars_written": written, "bars_pruned": pruned30 + pruned60,
            "elapsed_s": round(time.time() - t0, 1)}


def _analyze_one(repo: Min30Repo, code: str) -> Dict[str, Dict]:
    """对单票算 30min/60min MACD 摘要（各自读独立表，零HTTP）。数据不足返回空。"""
    out = {}
    for period in ("30m", "60m"):
        bars = repo.load_bars(code, period)
        if len(bars) < _MIN_BARS:
            continue
        r = macd([b["close"] for b in bars if b.get("close") is not None])
        out[period] = summarize(r)
    return out


def above_5week_ma(symbol: str, cfg: Optional[Dict] = None,
                   market_client=None) -> Optional[bool]:
    """判断最新价是否 > 5周均线（MA5周，最近5根周K收盘均值）。

    数据来源：日K多源退避（本地库最新）→ resample_bars('1w') 派生周K，
    避免直读 period='weekly' 的滞后表。返回 None 表示数据不足/异常。
    """
    try:
        if market_client is None:
            from ..adapters.market import MarketDataClient
            market_client = MarketDataClient(cfg or {})
        daily = market_client.fetch_bars(symbol, "1d")
        weekly = market_client.resample_bars(daily, "1w")
        closes = [b.close for b in weekly.bars if b.close is not None]
        if not closes:
            # 兜底：周K不足时直接用日K最后一根 vs 其自身 MA（近似）
            dcloses = [b.close for b in daily.bars if b.close is not None]
            if len(dcloses) < 5:
                return None
            return dcloses[-1] > (sum(dcloses[-5:]) / 5.0)
        if len(closes) < 5:
            return None
        ma5 = sum(closes[-5:]) / 5.0
        return closes[-1] > ma5
    except Exception as e:
        logger.debug(f"[ma5w] {symbol} 判定失败: {str(e)[:80]}")
        return None


def annotate_events(events: List[Dict], cfg: Optional[Dict] = None,
                    check_ma5w: bool = True) -> List[Dict]:
    """给事件附加 held（是否持仓）与 above_5w（是否站上5周均线）标记。

    只读一次 watchlist + 一次 market client，避免逐只重复开销；仅对金叉事件
    计算 5周均线（死叉分组不需要），无事件时零成本。
    """
    if not events:
        return events
    from . import watchlist as wl
    held = wl.load_held_map()
    mc = None
    if check_ma5w:
        try:
            from ..adapters.market import MarketDataClient
            mc = MarketDataClient(cfg or {})
        except Exception:
            mc = None
    for e in events:
        e["held"] = held.get(e["code"], False)
        e["above_5w"] = False
        if check_ma5w and mc is not None and e.get("to") == "金叉":
            e["above_5w"] = above_5week_ma(e["code"], cfg, market_client=mc) is True
    return events


def detect_crosses(repo: Optional[Min30Repo] = None,
                   codes: Optional[List[str]] = None,
                   first_run_all: bool = True,
                   cfg: Optional[Dict] = None,
                   annotate: bool = True) -> Dict:
    """检测 MACD 状态变更，返回变更事件（并更新状态表）。

    返回:
      events: [{code,name,freq,from,to,dif,dea,hist}] 仅状态翻转项
      first_run: 是否首次运行（状态表为空）
      detail: 各票当前状态摘要（供日志/调试）
    """
    repo = repo or Min30Repo()
    if codes is None:
        from . import watchlist as wl
        codes = wl.load_watchlist()
    # 首次判定：状态表无记录
    first_run = repo.stats().get("macd_states", 0) == 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    events: List[Dict] = []
    detail: Dict[str, Dict] = {}
    name_cache: Dict[str, str] = {}

    for code in codes:
        try:
            states = _analyze_one(repo, code)
        except Exception as e:
            logger.debug(f"[macd] analyze {code} 失败: {str(e)[:80]}")
            continue
        for freq, summ in states.items():
            detail.setdefault(code, {})[freq] = summ.get("state")
            cur = summ.get("state")
            if cur not in ("金叉", "死叉"):
                continue
            prev = repo.get_state(code, freq)
            if prev is None:
                # 首次：全部上报当前状态
                if first_run_all:
                    if code not in name_cache:
                        name_cache[code] = _name_of(code)
                    events.append({
                        "code": code, "name": name_cache[code], "freq": freq,
                        "from": "首次", "to": cur,
                        "dif": summ.get("dif"), "dea": summ.get("dea"),
                        "hist": summ.get("hist"), "ts": now,
                    })
            elif prev.get("state") != cur:
                # 状态翻转：金叉↔死叉
                if code not in name_cache:
                    name_cache[code] = _name_of(code)
                events.append({
                    "code": code, "name": name_cache[code], "freq": freq,
                    "from": prev.get("state"), "to": cur,
                    "dif": summ.get("dif"), "dea": summ.get("dea"),
                    "hist": summ.get("hist"), "ts": now,
                })
            # 更新状态表（无论是否上报）
            repo.put_state(code, freq, summ, now)
    if annotate:
        annotate_events(events, cfg=cfg)
    return {"events": events, "first_run": first_run, "detail": detail,
            "codes": len(codes)}


def format_notification(events: List[Dict], first_run: bool = False,
                        max_lines: int = 60) -> str:
    """把变更事件格式化为飞书文本，按用户指定优先级分组（W113）。

    分组顺序：
      ① 持仓股·死叉（含30m/60m，最高优先级，风险信号）
      ② 持仓股·60分钟金叉
      ③ 持仓股·30分钟金叉
      ④ 其他自选·站上5周均线·60分钟金叉（强势股）
      ⑤ 其他自选·60分钟金叉
      ⑥ 其他自选·30分钟金叉
      ⑦ 其他自选·死叉（含30m/60m，兜底，任何死叉都不漏）

    首次基线（first_run）沿用旧的两段式（金叉/死叉汇总），不分组，
    避免首次数百条时分组标题杂乱。
    """
    if not events:
        return ""
    lines = []
    if first_run:
        lines.append("【MACD监控·首次基线】自选股当前金叉/死叉状态（后续仅报变更）")
    else:
        lines.append("【MACD金叉/死叉变更】")
    lines.append(f"时间 {datetime.now().strftime('%Y-%m-%d %H:%M')}　共 {len(events)} 条")

    if first_run:
        golden = [e for e in events if e["to"] == "金叉"]
        death = [e for e in events if e["to"] == "死叉"]
        shown = 0

        def _section(group, label):
            nonlocal shown
            if not group:
                return
            lines.append(f"\n{label}（{len(group)} 条）：")
            n = 0
            for e in group:
                if shown >= max_lines:
                    lines.append(f"  … 本类另有 {len(group) - n} 条未展开")
                    break
                tag = f"{e['name']} " if e.get("name") else ""
                fr = f"（原{e['from']}）" if e.get("from") not in (None, "首次") else ""
                lines.append(f"  · {tag}{e['code']} {e['freq']} DIF={e['dif']} DEA={e['dea']}{fr}")
                shown += 1
                n += 1
            lines.append(f"  （本类展示 {n}/{len(group)} 条）")

        _section(golden, "🟢 金叉")
        _section(death, "🔴 死叉")
        return "\n".join(lines)

    # 非首次：按优先级分 7 组
    held = lambda e: bool(e.get("held"))
    golden = lambda e: e["to"] == "金叉"
    death = lambda e: e["to"] == "死叉"
    f30 = lambda e: e["freq"] == "30m"
    f60 = lambda e: e["freq"] == "60m"

    groups = [
        ("🔴 持仓股·死叉（最高优先级）", [e for e in events if held(e) and death(e)]),
        ("🟢 持仓股·60分钟金叉", [e for e in events if held(e) and golden(e) and f60(e)]),
        ("🟢 持仓股·30分钟金叉", [e for e in events if held(e) and golden(e) and f30(e)]),
        ("🟢 其他自选·站上5周均线·60分钟金叉（强势）",
         [e for e in events if not held(e) and golden(e) and f60(e) and e.get("above_5w")]),
        ("🟢 其他自选·60分钟金叉",
         [e for e in events if not held(e) and golden(e) and f60(e) and not e.get("above_5w")]),
        ("🟢 其他自选·30分钟金叉", [e for e in events if not held(e) and golden(e) and f30(e)]),
        ("🔴 其他自选·死叉", [e for e in events if not held(e) and death(e)]),
    ]

    shown = 0

    def _section(group, label):
        nonlocal shown
        if not group:
            return
        lines.append(f"\n{label}（{len(group)} 条）：")
        n = 0
        for e in group:
            if shown >= max_lines:
                lines.append(f"  … 本类另有 {len(group) - n} 条未展开")
                break
            tag = f"{e['name']} " if e.get("name") else ""
            hl = "⭐" if held(e) else ""
            fr = f"（原{e['from']}）" if e.get("from") not in (None, "首次") else ""
            lines.append(f"  · {hl}{tag}{e['code']} {e['freq']} DIF={e['dif']} DEA={e['dea']}{fr}")
            shown += 1
            n += 1
        lines.append(f"  （本类展示 {n}/{len(group)} 条）")

    for label, grp in groups:
        _section(grp, label)
    return "\n".join(lines)


def send_notification(text: str, target: Optional[str] = None) -> bool:
    """通过统一飞书 webhook 发送（不再依赖 Hermes）。target 参数保留兼容旧调用。"""
    return _feishu_send(text, prefix="[CZSC-MI MACD]\n")


def run_once(sync_first: bool = True) -> Dict:
    """一次完整流程：同步自选 → 检测变更 → 通知（有变更才发）。"""
    repo = Min30Repo()
    result: Dict = {}
    if sync_first:
        result["sync"] = sync_watchlist_min30(repo)
    det = detect_crosses(repo, cfg=None)
    result["first_run"] = det["first_run"]
    result["events"] = det["events"]
    result["event_count"] = len(det["events"])
    if det["events"]:
        text = format_notification(det["events"], det["first_run"])
        result["notified"] = send_notification(text)
    else:
        result["notified"] = False
    return result
