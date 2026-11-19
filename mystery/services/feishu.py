"""mystery.services.feishu — 统一的飞书通知出口（纯标准库，零外部依赖）。

设计：
- 走飞书群机器人 webhook（urllib + json），不依赖 Hermes / gateway / 任何第三方库；
- webhook 来源优先级：环境变量 FEISHU_WEBHOOK > ~/.config/czsc_mi/feishu_webhook；
- 发送成功判据兼容飞书两种返回（"Success" 或 "code":0）。

这样 czsc_mi 的通知链路收敛为「Python 标准库 + 飞书 webhook」一条，
O2 等内存受限节点无需再安装/维护 Hermes（省几百 MB + 独立 venv）。
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_WEBHOOK_FILE = Path.home() / ".config" / "czsc_mi" / "feishu_webhook"


def webhook_url() -> str:
    """获取飞书 webhook：环境变量优先，其次配置文件。"""
    url = os.environ.get("FEISHU_WEBHOOK", "").strip()
    if url:
        return url
    try:
        if _WEBHOOK_FILE.exists():
            return _WEBHOOK_FILE.read_text(encoding="utf-8").strip()
    except Exception as e:  # pragma: no cover - 文件异常极罕见
        logger.warning(f"[feishu] 读取 webhook 配置失败: {str(e)[:80]}")
    return ""


def send_text(text: str, *, prefix: str = "") -> bool:
    """发送纯文本消息到飞书 webhook。返回是否成功。"""
    if not text:
        return False
    url = webhook_url()
    if not url:
        logger.warning("[feishu] 未配置 FEISHU_WEBHOOK 或 ~/.config/czsc_mi/feishu_webhook，跳过发送")
        return False
    body_text = f"{prefix}{text}" if prefix else text
    payload = json.dumps(
        {"msg_type": "text", "content": {"text": body_text}},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        ok = "Success" in body or '"code":0' in body or '"code": 0' in body
        logger.info(f"[feishu] send ok={ok} resp={body[:120]}")
        return ok
    except Exception as e:
        logger.warning(f"[feishu] send 失败: {str(e)[:120]}")
        return False


__all__ = ["webhook_url", "send_text"]
