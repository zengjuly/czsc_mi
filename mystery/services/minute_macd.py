"""mystery.services.minute_macd — 自选股分钟K线同步 + MACD金叉死叉监控（W103）。

设计要点（用户拍板）：
- 仅处理自选股（data/watchlist.json），不碰全市场；
- 只分析 MACD，不跑缠论/共振/评分等重逻辑，避免资源浪费；
- 只上报「金叉/死叉变更」：与上次状态比对，状态翻转才通知；首次运行
  全部上报当前状态（用户选「首次全部上报」）。
- 30min K线存独立表（最近20天）；60min 由 30min 聚合，不落盘。
- 通知走 Hermes 飞速通道（hermes send → 飞书 DM），不依赖 gateway 服务。
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from datetime import datetime
from typing import Dict, List, Optional

from ..adapters import minute_source as ms
from ..core.macd import macd, summarize
from ..store._repos.min30_repo import Min30Repo

logger = logging.getLogger(__name__)

_HERMES = os.environ.get("HERMES_BIN", "/home/ai/ai_runner/.local/bin/hermes")
_HERMES_TARGET = os.environ.get(
    "MYSTERY_MACD_HERMES_TARGET", "feishu:oc_76e8dd2b391dd080b344157b00de696e")
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


def detect_crosses(repo: Optional[Min30Repo] = None,
                   codes: Optional[List[str]] = None,
                   first_run_all: bool = True) -> Dict:
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
    return {"events": events, "first_run": first_run, "detail": detail,
            "codes": len(codes)}


def format_notification(events: List[Dict], first_run: bool = False,
                        max_lines: int = 40) -> str:
    """把变更事件格式化为飞书文本（只含金叉/死叉变更）。

    首次基线可能达数百条，用 max_lines 限制明细行数，超出按金叉/死叉汇总，
    避免飞书刷屏/超长消息被截断。
    """
    if not events:
        return ""
    lines = []
    if first_run:
        lines.append("【MACD监控·首次基线】自选股当前金叉/死叉状态（后续仅报变更）")
    else:
        lines.append("【MACD金叉/死叉变更】")
    lines.append(f"时间 {datetime.now().strftime('%Y-%m-%d %H:%M')}　共 {len(events)} 条")
    golden = [e for e in events if e["to"] == "金叉"]
    death = [e for e in events if e["to"] == "死叉"]

    shown = 0  # 全局明细行数上限

    def _section(group, label):
        nonlocal shown
        if not group:
            return
        lines.append(f"\n{label}（{len(group)} 条）：")
        section_shown = 0
        for e in group:
            if shown >= max_lines:
                lines.append(f"  … 本类另有 {len(group) - section_shown} 条未展开")
                break
            tag = f"{e['name']} " if e.get("name") else ""
            fr = f"（原{e['from']}）" if not first_run and e.get("from") not in (None, "首次") else ""
            lines.append(f"  · {tag}{e['code']} {e['freq']} DIF={e['dif']} DEA={e['dea']}{fr}")
            shown += 1
            section_shown += 1
        lines.append(f"  （本类展示 {section_shown}/{len(group)} 条）")

    _section(golden, "🟢 金叉")
    _section(death, "🔴 死叉")
    return "\n".join(lines)


def send_notification(text: str, target: Optional[str] = None) -> bool:
    """通过 Hermes 飞速通道发送（hermes send，无需 gateway 服务）。"""
    if not text:
        return False
    tgt = target or _HERMES_TARGET
    try:
        p = subprocess.run(
            [_HERMES, "send", "--to", tgt, "--subject", "[CZSC-MI MACD]", text],
            capture_output=True, text=True, timeout=60)
        ok = p.returncode == 0
        logger.info(f"[hermes] send rc={ok} stdout={p.stdout.strip()[:100]} "
                    f"stderr={p.stderr.strip()[:100]}")
        return ok
    except Exception as e:
        logger.warning(f"[hermes] send 失败: {str(e)[:120]}")
        return False


def run_once(sync_first: bool = True) -> Dict:
    """一次完整流程：同步自选 → 检测变更 → 通知（有变更才发）。"""
    repo = Min30Repo()
    result: Dict = {}
    if sync_first:
        result["sync"] = sync_watchlist_min30(repo)
    det = detect_crosses(repo)
    result["first_run"] = det["first_run"]
    result["events"] = det["events"]
    result["event_count"] = len(det["events"])
    if det["events"]:
        text = format_notification(det["events"], det["first_run"])
        result["notified"] = send_notification(text)
    else:
        result["notified"] = False
    return result
