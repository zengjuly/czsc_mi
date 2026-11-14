"""W85：行业板块关注跟踪与板块分析（只读展示，不进评分、不改 rule_ver）。

提供：
- ``normalize_sector_code`` / ``list_sectors``：板块代码归一与可选板块清单
  （来自 ``sector_meta`` ∩ ``sector_kline``，即真实有指数 K 线的板块）。
- ``resolve_focus_codes``：按 W85 配置解析「关注板块」——``auto`` 跟随当期
  分析标的的主行业（自选股相关板块）、``all`` 全部、或显式 code 列表。
- ``analyze_sector`` / ``analyze_focus_sectors``：单板块 / 全部关注板块研判，
  输出可 JSON 序列化的 dict 列表，供日报 Excel/HTML、全量后台扫描报告与 Web 复用。
- ``sector_summary_line``：一行文字摘要（头部 QA / 飞书正文共用）。

板块研判口径（与 W84 各大指数一致 + 板块特有强度分）：
- 价格面：年线位置、MA 多头排列、5/20 日涨跌、日K/周K 典型组合形态；
- 板块强度分：复用 ``core.resonance.calculate_industry_score_from_sector``
  （0~25，>12.5 向上），取数只用真实 ``sector_kline`` 指数，**禁止成分股抽样**；
- 主力吸筹：``core.accumulation.analyze_accumulation``（W86，纯价格口径）；
- 强相关个股：板块指数与成分股近 N 日收益率 Pearson 相关系数，取 topN，
  并在当期分析结果里附带评分 / 建议 / 吸筹结论（不重算个股）。

设计约束（与 W82/W83/W84 一致）：
- 取数统一通过传入的 ``db``（MysteryDB）既有方法 + 只读 SQL，本模块不写库；
- core 零 IO 不变：所有纯计算（价格指标 / 相关系数）为模块级纯函数；
- 板块强度禁止成分股抽样，只用 ``sector_kline`` 真实指数；
- 缺数据 → ``has_data=False``，结论字段置 ``None``，绝不抛异常阻塞主流程。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from mystery.core import candlestick_patterns as _cp
from mystery.core.accumulation import analyze_accumulation, accumulation_label
from mystery.core.models import Bar, BarSeries
from mystery.core.resonance import calculate_industry_score_from_sector

logger = logging.getLogger(__name__)

# auto 模式下，单个板块最多取多少只成分股参与相关性计算（大板块性能护栏）
CORR_MAX_STOCKS = 250


def normalize_sector_code(code: str) -> str:
    """板块代码归一：``881101.TI`` / ``881101`` / ``ths_881101`` → ``ths_881101``。"""
    s = str(code or "").strip()
    if s.startswith("ths_"):
        return s
    return f"ths_{s.split('.')[0]}"


def _rel_code(sector_code: str) -> str:
    """``ths_881101`` → ``881101.TI``（stock_sector_rel 存储格式）。"""
    s = str(sector_code or "").strip()
    if s.startswith("ths_"):
        s = s[4:]
    if "." not in s:
        s = f"{s}.TI"
    return s


def _stock_db_code(symbol: str) -> str:
    """分析结果符号 ``600519.SH`` / ``sh600519`` → 库格式 ``sh.600519``。"""
    from mystery.adapters.codes import db_code_of
    try:
        return db_code_of(symbol)
    except (ValueError, TypeError):
        s = str(symbol or "").strip().lower().replace(".", "")
        return f"{s[:2]}.{s[2:]}" if len(s) > 2 else s


def _price_view(closes: Sequence[float]) -> Dict[str, Any]:
    """纯价格研判（与 W84 index_analysis 同口径）：年线 / MA 排列 / 涨跌。"""
    out: Dict[str, Any] = {"close": None, "chg_5d": None, "chg_20d": None,
                           "above_year": None, "year_dist_pct": None,
                           "ma_aligned": None}
    c = [float(x) for x in closes if x is not None and x == x]
    if len(c) < 6:
        return out
    out["close"] = round(c[-1], 2)

    def _pct(n: int) -> Optional[float]:
        if len(c) < n + 1 or c[-(n + 1)] == 0:
            return None
        return round((c[-1] - c[-(n + 1)]) / c[-(n + 1)] * 100, 2)

    out["chg_5d"], out["chg_20d"] = _pct(5), _pct(20)
    if len(c) >= 250:
        ma250 = sum(c[-250:]) / 250
        if ma250 > 0:
            out["above_year"] = c[-1] > ma250
            out["year_dist_pct"] = round((c[-1] - ma250) / ma250 * 100, 2)
    if len(c) >= 60:
        ma5 = sum(c[-5:]) / 5
        ma10 = sum(c[-10:]) / 10
        ma20 = sum(c[-20:]) / 20
        ma60 = sum(c[-60:]) / 60
        out["ma_aligned"] = ma5 >= ma10 >= ma20 >= ma60
    return out


def _df_to_bars(df: pd.DataFrame) -> List[Bar]:
    """中文列 DataFrame → ``List[Bar]``（升序），供形态/吸筹纯函数使用。"""
    bars: List[Bar] = []
    if df is None or df.empty:
        return bars
    for _, r in df.iterrows():
        try:
            bars.append(Bar(
                dt=str(r.get("日期") or r.get("date") or "")[:10],
                open=float(r.get("开盘价") or r.get("open") or 0),
                high=float(r.get("最高价") or r.get("high") or 0),
                low=float(r.get("最低价") or r.get("low") or 0),
                close=float(r.get("收盘价") or r.get("close") or 0),
                volume=float(r.get("成交量") or r.get("volume") or 0),
                amount=float(r.get("成交额") or r.get("amount") or 0),
            ))
        except (TypeError, ValueError):
            continue
    return bars


def _weekly_bars(daily: pd.DataFrame) -> List[Bar]:
    """日 K 按自然周聚合为周 K（真实数据重采样，不做成分股抽样、不造数）。"""
    try:
        if daily is None or daily.empty or "日期" not in daily.columns:
            return []
        d = daily.copy()
        d["日期"] = pd.to_datetime(d["日期"], errors="coerce")
        d = d.dropna(subset=["日期"]).set_index("日期").sort_index()
        agg = {"开盘价": "first", "最高价": "max", "最低价": "min",
               "收盘价": "last", "成交量": "sum"}
        if "成交额" in d.columns:
            agg["成交额"] = "sum"
        w = d.resample("W-FRI").agg(agg).dropna(subset=["收盘价"])
        w = w[w["收盘价"] > 0].reset_index()
        w["日期"] = w["日期"].dt.strftime("%Y-%m-%d")
        return _df_to_bars(w)
    except Exception as exc:  # noqa: BLE001 周K聚合失败不影响日K与主体
        logger.debug("板块周K聚合失败: %s", exc)
        return []


def _candle_summary(bars: Sequence[Bar], period: str) -> str:
    """日/周 K 组合形态摘要（只读，不进评分）。"""
    if not bars:
        return "—"
    try:
        return (_cp.detect_combo_patterns(
            BarSeries(symbol="", freq="1d" if period == "daily" else "1w",
                      bars=list(bars)),
            period=period, lookback=120).get("summary") or "—")
    except Exception:  # noqa: BLE001
        return "—"


def list_sectors(db) -> List[Dict[str, Any]]:
    """可选板块清单：仅返回 ``sector_kline`` 有真实指数数据的板块。

    :returns: ``[{'code','name','parent_type','n_stocks'}, ...]`` 按名称排序。
    """
    out: List[Dict[str, Any]] = []
    try:
        meta = {str(m[0]): (m[1], m[2]) for m in db.get_sector_meta(active_only=True)}
        conn = db._connect()
        try:
            # sector_meta 主要覆盖 885/886 概念板块；881 行业板块名称直接取
            # sector_kline.sector_name（真实指数自带名称），避免回退成代码。
            rows = conn.execute(
                "SELECT sector_code, MAX(sector_name), MAX(trade_date), COUNT(*) "
                "FROM sector_kline GROUP BY sector_code "
                "HAVING COUNT(*) >= 30").fetchall()
            rel_counts = dict(conn.execute(
                "SELECT sector_code, COUNT(*) FROM stock_sector_rel "
                "WHERE is_primary=1 GROUP BY sector_code").fetchall())
        finally:
            conn.close()
        for code, kline_name, last_date, n_bars in rows:
            meta_name, parent = meta.get(code, ("", "行业/概念"))
            name = meta_name or kline_name or str(code)
            out.append({
                "code": str(code), "name": str(name),
                "parent_type": str(parent or "行业/概念"),
                "last_date": str(last_date or ""),
                "n_bars": int(n_bars),
                "n_stocks": int(rel_counts.get(_rel_code(code), 0)),
            })
        out.sort(key=lambda d: (not d["code"].startswith("ths_881"), d["name"]))
    except Exception as exc:  # noqa: BLE001
        logger.warning("板块清单读取失败: %s", exc)
    return out


def resolve_focus_codes(db, focus: Any, *, results: Optional[List[Dict]] = None,
                         auto_max: int = 8) -> List[str]:
    """解析「关注板块」配置的 code 列表。

    - ``auto``：跟随当期分析标的（results）的主行业，按出现频次取前 auto_max；
      results 为空时退回自选股文件 ``data/watchlist.json`` 的主行业；
    - ``all``：全部有 K 线的板块（上限 auto_max*3，防止报告过长）；
    - ``list[str]``：显式 code 列表（网页勾选），归一后校验是否真有 K 线。
    """
    all_codes = {d["code"] for d in list_sectors(db)}
    if isinstance(focus, (list, tuple, set)):
        picked = [normalize_sector_code(c) for c in focus]
        return [c for c in picked if not all_codes or c in all_codes][:max(1, auto_max) * 3]
    f = str(focus or "auto").strip().lower()
    if f == "all":
        return sorted(all_codes)[:max(1, auto_max) * 3]
    # auto：跟随当期标的主行业
    codes: List[str] = []
    for r in (results or []):
        s = r.get("symbol")
        if s:
            codes.append(_stock_db_code(s))
    if not codes:
        try:
            from mystery.services import watchlist as _wl
            codes = [_stock_db_code(s) for s in _wl.load_watchlist()]
        except Exception:  # noqa: BLE001
            codes = []
    if not codes:
        return []
    try:
        conn = db._connect()
        try:
            ph = ",".join("?" for _ in codes)
            rows = conn.execute(
                f"SELECT sector_code, COUNT(*) FROM stock_sector_rel "
                f"WHERE is_primary=1 AND stock_code IN ({ph}) "
                f"GROUP BY sector_code ORDER BY COUNT(*) DESC", codes).fetchall()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("auto 板块解析失败: %s", exc)
        return []
    picked = [normalize_sector_code(r[0]) for r in rows]
    return [c for c in picked if c in all_codes][:max(1, int(auto_max))]


def _related_stocks(db, sector_code: str, sector_ret: pd.Series, *,
                    top_n: int, results: Optional[List[Dict]] = None
                    ) -> List[Dict[str, Any]]:
    """板块指数收益率 → 成分股相关系数 topN（真实个股日 K，只读）。

    相关性用近 ``len(sector_ret)`` 个交易日的日收益率 Pearson 系数；样本不足
    （有效重叠 <15 日）跳过该股，绝不编造相关度。当期分析结果里若有该股，
    附带评分 / 操作建议 / 吸筹等级，不重算个股。
    """
    empty: List[Dict[str, Any]] = []
    if sector_ret is None or len(sector_ret) < 15:
        return empty
    try:
        rel = _rel_code(sector_code)
        conn = db._connect()
        try:
            stock_rows = conn.execute(
                "SELECT stock_code FROM stock_sector_rel WHERE sector_code=? "
                "ORDER BY is_primary DESC LIMIT ?", (rel, CORR_MAX_STOCKS)).fetchall()
            db_codes = [r[0] for r in stock_rows]
            if not db_codes:
                return empty
            dates = list(sector_ret.index)
            ph = ",".join("?" for _ in db_codes)
            krows = conn.execute(
                f"SELECT code, date, close FROM stock_kline_data "
                f"WHERE period='daily' AND date>=? AND code IN ({ph})",
                [str(min(dates))[:10]] + db_codes).fetchall()
            name_rows = conn.execute(
                "SELECT code, code_name FROM stock_industry_info").fetchall()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.debug("板块 %s 相关个股取数失败: %s", sector_code, exc)
        return empty

    names = {r[0]: (r[1] or "") for r in name_rows}
    # 个股收盘价透视表
    frame: Dict[str, Dict[str, float]] = {}
    for code, date, close in krows:
        if close is None:
            continue
        frame.setdefault(code, {})[str(date)[:10]] = float(close)
    sec = sector_ret.dropna()
    sec_idx = [str(d)[:10] for d in sec.index]
    sec_vals = sec.to_numpy(dtype=float)
    scored: List[Dict[str, Any]] = []
    for code, series in frame.items():
        common = [d for d in sec_idx if d in series]
        if len(common) < 15:
            continue
        try:
            sv = np.array([series[d] for d in common], dtype=float)
            sr = np.array([sec_vals[sec_idx.index(d)] for d in common], dtype=float)
            if len(sv) < 15 or np.std(sv) == 0 or np.std(sr) == 0:
                continue
            corr = float(np.corrcoef(sv, sr)[0, 1])
            if corr != corr:  # NaN
                continue
        except Exception:  # noqa: BLE001
            continue
        scored.append({"code": code, "name": names.get(code, ""),
                       "corr": round(corr, 3)})
    scored.sort(key=lambda d: d["corr"], reverse=True)
    scored = scored[:max(1, int(top_n))]

    # 关联当期分析结果（只读，不重算个股）
    by_db: Dict[str, Dict] = {}
    for r in (results or []):
        s = r.get("symbol")
        if s:
            by_db[_stock_db_code(s)] = r
    from mystery.core.accumulation import accumulation_from_result
    out: List[Dict[str, Any]] = []
    for d in scored:
        r = by_db.get(d["code"])
        item = {
            "symbol": (r.get("symbol") if r else d["code"]),
            "name": d["name"] or (r.get("name") if r else d["code"]),
            "corr": d["corr"],
            "analyzed": bool(r),
            "score": (r.get("score") if r else None),
            "advice": (r.get("advice") if r else "—"),
        }
        if r:
            acc = accumulation_from_result(r, name=item["name"])
            item["accumulation"] = acc
            item["accumulation_level"] = accumulation_label(acc)
        else:
            item["accumulation_level"] = "—"
        out.append(item)
    return out


def analyze_sector(db, code: str, name: str = "", *,
                    top_n: int = 8, corr_window: int = 60,
                    results: Optional[List[Dict]] = None,
                    as_of: str = "") -> Dict[str, Any]:
    """单个板块研判（只读展示）；任何失败都降级 ``has_data=False``，不抛。

    W95/③：``as_of`` 非空时板块指数 K 线按该日截断（历史回放截面）。
    """
    c = normalize_sector_code(code)
    if not name:
        try:
            conn = db._connect()
            try:
                _sql = ("SELECT sector_name FROM sector_kline "
                        "WHERE sector_code=? ")
                _params = [c]
                if as_of:
                    _sql += "AND trade_date <= ? "
                    _params.append(str(as_of)[:10])
                _sql += "ORDER BY trade_date DESC LIMIT 1"
                row = conn.execute(_sql, tuple(_params)).fetchone()
                name = row[0] if row and row[0] else ""
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            name = ""
    base: Dict[str, Any] = {
        "name": name or c, "code": c, "date": "", "close": None,
        "chg_5d": None, "chg_20d": None, "above_year": None,
        "year_dist_pct": None, "ma_aligned": None,
        "strength_score": None, "strength_up": None,
        "candle_daily": "—", "candle_weekly": "—",
        "accumulation": None, "accumulation_level": "—",
        "related": [], "has_data": False,
    }
    try:
        # 仅在回放模式才传 end_date：正式扫描走旧签名，保证既有 FakeDB
        # （只实现 get_sector_kline(code)）与 W85 用例零回归。
        df = (db.get_sector_kline(c, end_date=as_of) if as_of
              else db.get_sector_kline(c))
        if df is None or df.empty or "收盘价" not in df.columns:
            return base
        closes = pd.to_numeric(df["收盘价"], errors="coerce").dropna().tolist()
        if len(closes) < 30:
            return base
        view = _price_view(closes)
        daily_bars = _df_to_bars(df)
        weekly_bars = _weekly_bars(df)
        strength = calculate_industry_score_from_sector(df)
        # 收益率序列：与个股日 K 对齐到 trade_date
        ret = pd.to_numeric(df["收盘价"], errors="coerce")
        dates = pd.to_datetime(df["日期"], errors="coerce") \
            if "日期" in df.columns else pd.Series(range(len(ret)))
        sec_ret = pd.Series(ret.values, index=dates).pct_change().dropna()
        sec_ret = sec_ret.tail(max(15, int(corr_window)))
        acc = analyze_accumulation(daily_bars, period="daily",
                                   name=base["name"])
        related = _related_stocks(db, c, sec_ret, top_n=top_n, results=results)
        base.update({
            **view,
            "date": str(df["日期"].iloc[-1])[:10] if "日期" in df.columns else "",
            "strength_score": round(float(strength), 2),
            "strength_up": bool(strength > 12.5),
            "candle_daily": _candle_summary(daily_bars, "daily"),
            "candle_weekly": _candle_summary(weekly_bars, "weekly"),
            "accumulation": acc,
            "accumulation_level": accumulation_label(acc),
            "related": related, "has_data": True,
        })
    except Exception as exc:  # noqa: BLE001 板块分析失败不阻塞报告
        logger.warning("板块 %s(%s) 分析失败: %s", name, c, exc)
    return base


def analyze_focus_sectors(db, *, opts: Optional[Dict[str, Any]] = None,
                          results: Optional[List[Dict]] = None,
                          as_of: Optional[str] = None
                          ) -> List[Dict[str, Any]]:
    """全部「关注板块」研判（W85 日报/扫描报告统一入口）。

    :param db: MysteryDB 实例（取数通道，只读）。
    :param opts: ``runtime_settings.sector_options_normalized()`` 结果；
                 None 时内部读取（保证 CLI/Web 口径一致）。
    :param results: 当期个股分析结果（用于 auto 板块解析 + 相关个股附结论）。
    """
    try:
        # W95/③：未显式传 as_of 时从 env 读（CLI 回放已设 MYSTERY_AS_OF），
        # 正式扫描无该变量 → 空串，行为零变化。
        import os
        if as_of is None:
            as_of = (os.environ.get("MYSTERY_AS_OF") or "")[:10]
        if opts is None:
            from mystery.services import runtime_settings as rts
            opts = rts.sector_options_normalized()
        if not opts.get("enabled", True):
            return []
        codes = resolve_focus_codes(db, opts.get("focus", "auto"),
                                   results=results,
                                   auto_max=opts.get("auto_max", 8))
        if not codes:
            return []
        name_map = {d["code"]: d["name"] for d in list_sectors(db)}
        return [analyze_sector(db, c, name_map.get(c, c),
                               top_n=opts.get("top_n", 8),
                               corr_window=opts.get("corr_window", 60),
                               results=results, as_of=as_of or "") for c in codes]
    except Exception as exc:  # noqa: BLE001 配置/取数异常不阻塞日报
        logger.warning("关注板块分析失败: %s", exc)
        return []


def sector_summary_line(sectors: List[Dict[str, Any]]) -> str:
    """一行文字摘要：板块(强度分/年线/20日%·吸筹)。缺数据跳过。"""
    parts = []
    for d in sectors:
        if not d.get("has_data"):
            continue
        st = d.get("strength_score")
        arrow = "偏强" if d.get("strength_up") else "偏弱"
        chg = d.get("chg_20d")
        chg_s = f"{chg:+.1f}%" if chg is not None else "—"
        acc = d.get("accumulation_level") or "—"
        parts.append(f"{d['name']}(强度{st:.1f}{arrow}/20日{chg_s}·{acc})"
                     if st is not None else f"{d['name']}(强度—/20日{chg_s}·{acc})")
    return "[行业板块] " + "；".join(parts) if parts else "[行业板块] 暂无数据"


__all__ = [
    "normalize_sector_code", "list_sectors", "resolve_focus_codes",
    "analyze_sector", "analyze_focus_sectors", "sector_summary_line",
]
