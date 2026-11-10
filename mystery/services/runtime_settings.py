"""W76：运行时配置覆盖层（网页 / CLI 可改、一键还原、热生效）。

职责边界
--------
- 只读写 ``<repo>/data/*.json`` 配置覆盖层文件；**不写业务库**
  （scan_jobs / scan_results / analysis_cache / 自选股一律不碰）。
- core 层零 IO 不变：本模块属 service 层，core 只接受注入后的稀疏 dict。
- 优先级：**显式 env（运维应急）> 覆盖层文件（网页保存）> 代码默认**。
- 覆盖层为「稀疏 dict」：只存用户显式改过的键。没存的键保持代码默认，
  因此「一键还原 = 删除文件」即可，且不会把默认值固化进文件。

W74 缺陷修复
------------
W74 原实现 ``Path(os.environ.get(...) or "") or 默认路径``：``Path("")`` 恒为
``Path(".")`` 且布尔为真，导致生产覆盖层路径退化成当前目录 ``.``，
``exists()`` 恒 True、读取报 ``IsADirectoryError``、保存/还原失效。
本模块统一用 ``resolve_override_path()``：显式 env 非空才用，否则落到
**包位置推导的绝对目录** ``<repo>/data``（与 watchlist.json 同目录），
不受 cwd / ``MYSTERY_DB_PATH`` 影响，保证 Web / CLI / 日报读到同一份文件。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger(__name__)

# 统一目录：<repo>/data（mystery/services/xxx.py → parents[2] = 仓库根）。
# 可由 MYSTERY_RUNTIME_OVERRIDE_DIR 整体重定向（测试/多实例部署用）。
_dir_raw = (os.environ.get("MYSTERY_RUNTIME_OVERRIDE_DIR") or "").strip()
RUNTIME_OVERRIDE_DIR: Path = (Path(_dir_raw) if _dir_raw
                              else Path(__file__).resolve().parents[2] / "data")


def resolve_override_path(env_key: str, filename: str) -> Path:
    """覆盖层路径：显式 env（非空）优先，否则落到统一目录。

    注意不能用 ``Path(x or "") or default`` —— Path("") 是 Path(".") 且恒真。
    """
    raw = (os.environ.get(env_key) or "").strip()
    return Path(raw) if raw else (RUNTIME_OVERRIDE_DIR / filename)


def load_override(path: Path, allowed_keys: Iterable[str]) -> Dict[str, Any]:
    """读取覆盖层；不存在 / 是目录 / 损坏 → 空 dict，绝不抛错。"""
    allowed = set(allowed_keys)
    try:
        if not path.exists() or path.is_dir():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return {k: v for k, v in (data or {}).items() if k in allowed}
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取运行时覆盖层失败(%s)，回退空: %s", path, exc)
        return {}


def save_override(path: Path, clean: Dict[str, Any]) -> None:
    """原子写（先 .tmp 再 replace），避免并发/崩溃留下半截 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2,
                              sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def clear_override(path: Path) -> None:
    """一键还原：删除覆盖层文件即可。"""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:  # noqa: BLE001
        logger.warning("删除运行时覆盖层失败(%s): %s", path, exc)


# ---------------------------------------------------------------------------
# W76：三浪扫描参数（评估上限可在网页改 + 一键还原）
# ---------------------------------------------------------------------------
# 键名沿用 W69/W75 既有 env 名，网页覆盖层与 env 完全等价、可互相覆盖。
SCAN_WAVE3_ENV_KEYS: tuple = (
    "MYSTERY_SCAN_WAVE3",                  # 三浪策略开关 1/0
    "MYSTERY_SCAN_WAVE3_MAX",              # 显式上限：0=强制全量，>0=固定前 N 只
    "MYSTERY_SCAN_WAVE3_AUTO_TRIGGER",     # 自动限流触发阈值：池 > N 只才限
    "MYSTERY_SCAN_WAVE3_AUTO_MAX",         # 自动限流评估上限：仅评前 N 只
    "MYSTERY_SCAN_WAVE3_STRICT",           # 九铁律严格模式 1/0
)

# 生产默认（W75 行为不变）。MAX 默认「未设置」→ 走自动限流，绝不能固化成 0。
SCAN_WAVE3_DEFAULTS: Dict[str, Optional[str]] = {
    "MYSTERY_SCAN_WAVE3": "1",
    "MYSTERY_SCAN_WAVE3_MAX": None,
    "MYSTERY_SCAN_WAVE3_AUTO_TRIGGER": "500",
    "MYSTERY_SCAN_WAVE3_AUTO_MAX": "200",
    "MYSTERY_SCAN_WAVE3_STRICT": "0",
}

# 表单元数据（Web 面板与 CLI 复用，字段口径单一来源）。
SCAN_WAVE3_FIELDS: tuple = (
    ("MYSTERY_SCAN_WAVE3", "三浪策略开关", "select", ("1", "0")),
    ("MYSTERY_SCAN_WAVE3_AUTO_TRIGGER", "自动限流触发阈值(池>N只)",
     "number", (0, 100000, 100)),
    ("MYSTERY_SCAN_WAVE3_AUTO_MAX", "三浪评估上限(前N只)",
     "number", (0, 10000, 50)),
    ("MYSTERY_SCAN_WAVE3_STRICT", "三浪铁律严格模式", "select", ("0", "1")),
)

SCAN_WAVE3_OVERRIDE_PATH: Path = resolve_override_path(
    "MYSTERY_SCAN_WAVE3_OVERRIDE", "scan_wave3_override.json")


def load_scan_wave3_override() -> Dict[str, str]:
    """网页保存的稀疏覆盖层（只含显式设置项）。"""
    return load_override(SCAN_WAVE3_OVERRIDE_PATH, SCAN_WAVE3_ENV_KEYS)


def save_scan_wave3_override(opts: Dict[str, Any]) -> None:
    """保存网页表单：只存已知键、跳过 None/空串（保持稀疏语义）。

    ``MYSTERY_SCAN_WAVE3_MAX="0"`` 是合法显式值（强制全量），必须保留。
    """
    clean: Dict[str, str] = {}
    for k, v in (opts or {}).items():
        if k not in SCAN_WAVE3_ENV_KEYS or v is None:
            continue
        s = str(v).strip()
        if s != "":
            clean[k] = s
    save_override(SCAN_WAVE3_OVERRIDE_PATH, clean)


def clear_scan_wave3_override() -> None:
    """一键还原默认值：删除覆盖层，回到 W75 自动限流 (500/200)。"""
    clear_override(SCAN_WAVE3_OVERRIDE_PATH)


def scan_wave3_override_exists() -> bool:
    """是否已自定义（供面板提示「已修改，可还原」）。"""
    return SCAN_WAVE3_OVERRIDE_PATH.is_file()


def scan_wave3_options_effective() -> Dict[str, str]:
    """实际生效的**稀疏**口径 = 覆盖层文件 + 显式 env（优先级递增）。

    - 不含代码默认值：未设置的键不出现，保持 W75「MAX 未显式设置 →
      自动限流生效」的稀疏语义；
    - Web / CLI / 日报 / 扫描收尾全部经此函数读取，口径绝对一致、热生效；
    - 返回字符串值，解析/兜底仍由 ``wave3_scan._env_int`` 负责。
    """
    out: Dict[str, str] = dict(load_scan_wave3_override())
    for k in SCAN_WAVE3_ENV_KEYS:
        v = os.environ.get(k)
        if v is not None and str(v).strip() != "":
            out[k] = str(v).strip()
    return out


def scan_wave3_options_display() -> Dict[str, str]:
    """面板展示用**完整**生效值 = 生产默认 + 覆盖层 + env。"""
    out: Dict[str, str] = {k: v for k, v in SCAN_WAVE3_DEFAULTS.items()
                           if v is not None}
    out.update(scan_wave3_options_effective())
    return out


def scan_wave3_mode(effective: Optional[Dict[str, str]] = None) -> str:
    """当前评估上限模式：自动限流 / 固定上限 / 强制全量。"""
    eff = effective if effective is not None else scan_wave3_options_effective()
    raw = eff.get("MYSTERY_SCAN_WAVE3_MAX")
    if raw is None or str(raw).strip() == "":
        return "auto"
    try:
        return "full" if int(raw) == 0 else "fixed"
    except (TypeError, ValueError):
        return "fixed"


# ---------------------------------------------------------------------------
# W82：典型 K 线组合形态（网页可勾选/调窗口/日周开关 + 一键还原）
# ---------------------------------------------------------------------------
# 键名即 env 名，网页覆盖层与 env 完全等价、显式 env 优先。
CANDLE_ENV_KEYS: tuple = (
    "MYSTERY_CANDLE_ENABLED",       # 形态分析总开关 1/0
    "MYSTERY_CANDLE_DAILY",         # 日 K 单独分析 1/0
    "MYSTERY_CANDLE_WEEKLY",        # 周 K 单独分析 1/0
    "MYSTERY_CANDLE_LOOKBACK",      # 回溯窗口（根数）
    "MYSTERY_CANDLE_PATTERNS",      # 启用形态 code 逗号分隔，all=全部
)

CANDLE_DEFAULTS: Dict[str, str] = {
    "MYSTERY_CANDLE_ENABLED": "1",
    "MYSTERY_CANDLE_DAILY": "1",
    "MYSTERY_CANDLE_WEEKLY": "1",
    "MYSTERY_CANDLE_LOOKBACK": "60",
    "MYSTERY_CANDLE_PATTERNS": "all",
}

CANDLE_OVERRIDE_PATH: Path = resolve_override_path(
    "MYSTERY_CANDLE_OVERRIDE", "candlestick_override.json")


def load_candle_override() -> Dict[str, str]:
    """网页保存的稀疏覆盖层（只含显式设置项）。"""
    return load_override(CANDLE_OVERRIDE_PATH, CANDLE_ENV_KEYS)


def save_candle_override(opts: Dict[str, Any]) -> None:
    """保存网页表单：只存已知键、跳过 None/空串（保持稀疏语义）。"""
    clean: Dict[str, str] = {}
    for k, v in (opts or {}).items():
        if k not in CANDLE_ENV_KEYS or v is None:
            continue
        if isinstance(v, (list, tuple, set)):
            s = ",".join(str(x).strip() for x in v if str(x).strip()) or "all"
        else:
            s = str(v).strip()
        if s != "":
            clean[k] = s
    save_override(CANDLE_OVERRIDE_PATH, clean)


def clear_candle_override() -> None:
    """一键还原默认值：删除覆盖层文件。"""
    clear_override(CANDLE_OVERRIDE_PATH)


def candle_override_exists() -> bool:
    return CANDLE_OVERRIDE_PATH.is_file()


def candle_options_effective() -> Dict[str, str]:
    """实际生效的**稀疏**口径 = 覆盖层文件 + 显式 env（优先级递增）。"""
    out: Dict[str, str] = dict(load_candle_override())
    for k in CANDLE_ENV_KEYS:
        v = os.environ.get(k)
        if v is not None and str(v).strip() != "":
            out[k] = str(v).strip()
    return out


def candle_options_display() -> Dict[str, str]:
    """面板展示用**完整**生效值 = 生产默认 + 覆盖层 + env。"""
    out: Dict[str, str] = dict(CANDLE_DEFAULTS)
    out.update(candle_options_effective())
    return out


def candle_options_normalized() -> Dict[str, Any]:
    """归一化后的完整配置（service / Web / 缓存 tag 共用单一口径）。

    :return: ``{'enabled': bool, 'daily': bool, 'weekly': bool,
                'lookback': int, 'patterns': list[str]}``
    """
    from ..core.candlestick_patterns import PATTERN_CODES, resolve_enabled
    eff = candle_options_display()

    def _flag(key: str) -> bool:
        return str(eff.get(key, "1")).strip().lower() not in ("0", "false", "off", "no")

    try:
        lb = int(float(eff.get("MYSTERY_CANDLE_LOOKBACK", "60")))
    except (TypeError, ValueError):
        lb = 60
    lb = max(5, min(500, lb))
    raw = str(eff.get("MYSTERY_CANDLE_PATTERNS", "all")).strip()
    if not raw or raw.lower() in ("all", "*", "全部"):
        patterns = list(PATTERN_CODES)
    else:
        patterns = resolve_enabled([x.strip() for x in raw.split(",")])
    return {"enabled": _flag("MYSTERY_CANDLE_ENABLED"),
            "daily": _flag("MYSTERY_CANDLE_DAILY"),
            "weekly": _flag("MYSTERY_CANDLE_WEEKLY"),
            "lookback": lb, "patterns": patterns}


def candle_cache_tag() -> str:
    """W82：形态口径入分析缓存键（改任何一项必 miss，W73 同款护栏）。

    恒非空：首次上线即让旧缓存（无 |C[...] 后缀）整体失效一次，避免
    「改了勾选却仍读到不含形态字段的旧 payload」。
    """
    n = candle_options_normalized()
    sig = (f"E{int(n['enabled'])}|D{int(n['daily'])}|W{int(n['weekly'])}"
           f"|L{n['lookback']}|P{','.join(n['patterns'])}")
    return f"|C[{sig}]"


__all__ = [
    "RUNTIME_OVERRIDE_DIR", "resolve_override_path",
    "load_override", "save_override", "clear_override",
    "SCAN_WAVE3_ENV_KEYS", "SCAN_WAVE3_DEFAULTS", "SCAN_WAVE3_FIELDS",
    "SCAN_WAVE3_OVERRIDE_PATH",
    "load_scan_wave3_override", "save_scan_wave3_override",
    "clear_scan_wave3_override", "scan_wave3_override_exists",
    "scan_wave3_options_effective", "scan_wave3_options_display",
    "scan_wave3_mode",
    "CANDLE_ENV_KEYS", "CANDLE_DEFAULTS", "CANDLE_OVERRIDE_PATH",
    "load_candle_override", "save_candle_override", "clear_candle_override",
    "candle_override_exists", "candle_options_effective",
    "candle_options_display", "candle_options_normalized", "candle_cache_tag",
]
