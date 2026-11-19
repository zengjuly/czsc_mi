"""mystery.core.macd — MACD(12,26,9) 计算与金叉/死叉判定（W103）。

纯函数，只依赖标准库；输入为按时间升序的收盘价序列，输出 DIF/DEA/MACD 柱
及金叉/死叉状态。供分钟级 MACD 监控（30min/60min）与个股分析结果复用。

口径：
- EMA12/EMA26：首个值用第一根 close 初始化（SMA 种子），之后
  EMA_t = close_t * 2/(N+1) + EMA_{t-1} * (N-1)/(N+1)
- DIF = EMA12 - EMA26；DEA = DIF 的 9 日 EMA；MACD 柱 = (DIF - DEA) * 2
- 金叉：前一根 DIF<=DEA 且当根 DIF>DEA（由下往上穿）
- 死叉：前一根 DIF>=DEA 且当根 DIF<DEA（由上往下穿）
- 状态：最近一根 DIF>DEA 记 "金叉"（多头），否则 "死叉"（空头）

预热：MACD(12,26,9) 稳定约需 80 根（EMA26 收敛 60 + DEA 收敛 21），
20 天 30min=160 根、60min=80 根，恰好满足（见 W102 估算文档）。
"""
from __future__ import annotations

from typing import Dict, List, Sequence


def ema(values: Sequence[float], period: int) -> List[float]:
    """标准 EMA，首值用序列第一个值初始化。"""
    if not values:
        return []
    alpha = 2.0 / (period + 1)
    out = [float(values[0])]
    prev = out[0]
    for v in values[1:]:
        prev = float(v) * alpha + prev * (1 - alpha)
        out.append(prev)
    return out


def macd(closes: Sequence[float], fast: int = 12, slow: int = 26,
         signal: int = 9) -> Dict[str, object]:
    """计算 MACD 并返回最新状态 + 全序列（供判定交叉）。

    Returns:
        dict:
          dif_list/dea_list/hist_list: 全序列
          dif/dea/hist: 最新一根的值
          state: "金叉" | "死叉"（最新一根 DIF 与 DEA 相对位置）
          cross: "golden" | "death" | None（最新一根是否刚发生交叉）
          bars: 参与计算根数
    """
    closes = [float(x) for x in closes if x is not None]
    n = len(closes)
    if n < 2:
        return {"dif_list": [], "dea_list": [], "hist_list": [],
                "dif": None, "dea": None, "hist": None,
                "state": "未知", "cross": None, "bars": n}
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    dif_list = [f - s for f, s in zip(ema_fast, ema_slow)]
    dea_list = ema(dif_list, signal)
    hist_list = [(d - e) * 2 for d, e in zip(dif_list, dea_list)]

    dif, dea, hist = dif_list[-1], dea_list[-1], hist_list[-1]
    # 状态：最新一根相对位置
    state = "金叉" if dif > dea else ("死叉" if dif < dea else "粘合")
    # 交叉：需至少 2 根，比较前一根与当根
    cross = None
    if n >= 2:
        p_dif, p_dea = dif_list[-2], dea_list[-2]
        if p_dif <= p_dea and dif > dea:
            cross = "golden"
        elif p_dif >= p_dea and dif < dea:
            cross = "death"
    return {
        "dif_list": dif_list, "dea_list": dea_list, "hist_list": hist_list,
        "dif": round(dif, 4), "dea": round(dea, 4), "hist": round(hist, 4),
        "state": state, "cross": cross, "bars": n,
    }


def summarize(result: Dict[str, object]) -> Dict[str, object]:
    """精简对外输出（去全序列），供快照表/通知/分析结果使用。"""
    return {
        "dif": result.get("dif"),
        "dea": result.get("dea"),
        "hist": result.get("hist"),
        "state": result.get("state"),
        "cross": result.get("cross"),
        "bars": result.get("bars"),
    }
