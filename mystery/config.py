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
_DEFAULT_CONFIG = os.environ.get(
    "MYSTERY_CONFIG",
    os.path.join(_REPO_ROOT, "config", "config.yaml"),
)
_ENV_RE = re.compile(r"\$\{([^}]+)\}")


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
    """报表输出目录：MYSTERY_OUTPUT_DIR → config report.output_dir → <repo>/output。"""
    env = os.environ.get("MYSTERY_OUTPUT_DIR")
    if env:
        return env
    cfg = cfg or {}
    d = (cfg.get("report") or {}).get("output_dir") or ""
    if d:
        return d
    return os.path.join(_REPO_ROOT, "output")
