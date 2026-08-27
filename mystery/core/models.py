"""mystery/core/models.py — 跨层数据模型（零 IO，禁止 import czsc / 数据客户端）。

约定：
- 字段名保持稳定，Web / Excel / 扫描 / CLI 全部消费这些模型。
- to_dict() 保证 JSON 可序列化（datetime/date → iso 字符串，NaN → None）。
- core 层禁止 import czsc 与任何数据客户端。
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Union

# 周期归一值
FREQ_1D = "1d"
FREQ_1W = "1w"
FREQ_1M = "1M"

RULE_VER = "mystery-1.22.30-compat"


def _clean(v: Any) -> Any:
    """递归清洗：datetime/date → iso；numpy 标量 → python 标量；NaN/Inf → None。"""
    if v is None or isinstance(v, (str, bool)):
        return v
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    if isinstance(v, (int, float)):
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return None
        return v
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    # numpy 标量（np.float64 / np.int64 …）
    if hasattr(v, "item"):
        try:
            return _clean(v.item())
        except Exception:
            pass
    try:
        return float(v)
    except Exception:
        return str(v)


def _todict(obj: Any) -> Dict[str, Any]:
    return _clean(asdict(obj))


@dataclass
class Bar:
    """一根 K 线。dt 可为 datetime 或 iso 字符串。"""
    dt: Union[dt.datetime, str]
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    amount: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return _todict(self)


@dataclass
class BarSeries:
    """归一化后的 K 线序列。adjust 锁定一种复权（如 qfq）。"""
    symbol: str                 # 内部代码 600519.SH
    freq: str                   # "1d" | "1w" | "1M"
    adjust: str = "qfq"
    bars: List[Bar] = field(default_factory=list)
    source: str = ""            # ths_official / tdx_api / tdx_local / db

    def to_dict(self) -> Dict[str, Any]:
        return _todict(self)


@dataclass
class ChanBi:
    """缠论笔。"""
    direction: str              # "up" | "down"
    sdt: str                    # 起点日期 iso
    edt: str                    # 终点日期 iso
    high: float
    low: float
    confirmed: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return _todict(self)


@dataclass
class ChanZs:
    """缠论中枢。"""
    zg: float
    zd: float
    gg: float
    dd: float
    sdt: str
    edt: str
    n_bi: int = 0
    finished: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return _todict(self)


@dataclass
class ChanStructure:
    """单周期缠论结构摘要（只含展示所需字段，不携带 czsc 内部对象）。"""
    freq: str
    n_fx: int = 0
    bis: List[ChanBi] = field(default_factory=list)
    zss: List[ChanZs] = field(default_factory=list)
    last_bi_dir: str = ""       # "up" | "down" | ""
    last_bi_confirmed: bool = False
    in_zs: bool = False         # 当前是否位于中枢内
    engine: str = "czsc"
    engine_ver: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return _todict(self)


@dataclass
class MarketContext:
    """市场上下文：指数行情 + 行业 + 财务。"""
    index_bars: Optional[BarSeries] = None
    industry_name: str = "未知"
    industry_score: Optional[float] = None    # 0~25，>12.5 向上
    industry_up: Optional[bool] = None        # 行业趋势
    financial: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return _todict(self)


@dataclass
class MysteryBreakdown:
    """Mystery 规则明细（dict 结构对齐 stock_analyzer 旧报告键）。"""
    resonance: Dict[str, Any] = field(default_factory=dict)   # 三振共振
    main_wave: Dict[str, Any] = field(default_factory=dict)   # 主升浪
    platform: Dict[str, Any] = field(default_factory=dict)    # 平台突破
    vap_atr: Dict[str, Any] = field(default_factory=dict)     # 自适应 VAP-ATR 平台
    patterns: Dict[str, Any] = field(default_factory=dict)    # 形态识别
    checklist8: List[Dict[str, Any]] = field(default_factory=list)  # 主升浪8项

    def to_dict(self) -> Dict[str, Any]:
        return _todict(self)


@dataclass
class AnalysisResult:
    """唯一分析入口的产出。to_dict() 必须 JSON 可序列化。"""
    symbol: str                 # 600519.SH
    name: str = ""
    trade_date: str = ""        # 最新交易日 iso
    price: Optional[float] = None
    score: Optional[float] = None
    advice: str = ""
    true_resonance: bool = False
    mystery: MysteryBreakdown = field(default_factory=MysteryBreakdown)
    chan: Dict[str, ChanStructure] = field(default_factory=dict)  # freq -> ChanStructure
    sector: Dict[str, Any] = field(default_factory=dict)
    financial: Dict[str, Any] = field(default_factory=dict)
    rule_ver: str = RULE_VER
    czsc_ver: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return _todict(self)
