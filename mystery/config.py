"""mystery.config — 轻量配置加载（config/config.yaml，支持 ${ENV} 展开）。

业务代码禁止写死单机绝对路径：本机路径一律由环境变量注入，
config.yaml 只写 ${VAR} / ${VAR:-default} 占位。
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional

import yaml

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENV_RE = re.compile(r"\$\{([^}]+)\}")

# ---- 环境变量集中控制原则（2026-09-16）----
# 所有变量唯一事实源 = ~/.stockrc；所有进程（含裸环境启动的 Python）导入之。
# 已有 env 优先（setdefault 不覆盖），显式注入的变量仍可覆盖文件值。
_STOCKRC = os.path.expanduser("~/.stockrc")
_EXPORT_RE = re.compile(r"^\s*export\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def load_stockrc(path: str = _STOCKRC) -> int:
    """解析 ~/.stockrc 的 export 行 setdefault 进 os.environ；返回新注入数。

    文件缺失/不可读静默跳过（不阻塞导入）。值去外层引号，不做 shell 展开。
    """
    count = 0
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                m = _EXPORT_RE.match(line)
                if not m:
                    continue
                name, raw = m.group(1), m.group(2).strip()
                if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
                    raw = raw[1:-1]
                if name not in os.environ:
                    os.environ[name] = raw
                    count += 1
    except OSError:
        pass
    return count


load_stockrc()

_DEFAULT_CONFIG = os.environ.get(
    "MYSTERY_CONFIG",
    os.path.join(_REPO_ROOT, "config", "config.yaml"),
)


def _expand_env(value: str) -> str:
    """${VAR} / ${VAR:-default} 展开；未设置的 VAR 展开为空串。"""

    def _repl(m: "re.Match[str]") -> str:
        expr = m.group(1)
        if ":-" in expr:
            name, default = expr.split(":-", 1)
            return os.environ.get(name.strip(), default)
        return os.environ.get(expr.strip(), "")

    return _ENV_RE.sub(_repl, value)


def _expand_deep(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _expand_deep(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_deep(v) for v in value]
    if isinstance(value, str):
        return _expand_env(value)
    return value


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """加载 config/config.yaml（${ENV} 展开）。文件缺失/解析失败 → {}。"""
    path = path or _DEFAULT_CONFIG
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return _expand_deep(raw) if isinstance(raw, dict) else {}
    except Exception:
        return {}


def output_dir(cfg: Optional[Dict[str, Any]] = None) -> str:
    """报表输出目录：MYSTERY_OUTPUT_DIR → config report.output_dir → <repo>/output。

    cfg 省略时自行 load_config()（W44：修复 docstring 承诺的 config 回退
    实际从未生效——裸调用全落 repo/output，weekly_shares 状态文件因此崩溃、
    日报页脚管线/铺盘两行永远缺失）。"""
    env = os.environ.get("MYSTERY_OUTPUT_DIR")
    if env:
        return env
    if cfg is None:
        cfg = load_config()
    d = (cfg.get("report") or {}).get("output_dir") or ""
    if d:
        return d
    return os.path.join(_REPO_ROOT, "output")
