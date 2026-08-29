#!/usr/bin/env python
"""scripts/feishu_notify.py — 从 DB 提取最新扫描摘要并发送飞书（纯标准库，无 LLM）。

用法:
  python feishu_notify.py                 # 成功消息（读最新 scan_job + Top N + 真三振）
  python feishu_notify.py --error "..."   # 失败告警（附 pipeline 错误片段）

webhook 来源（按优先级）:
  1. 环境变量 FEISHU_WEBHOOK
  2. 文件 ~/.config/czsc_mi/feishu_webhook（一行 URL）
未配置 → 打印"未配置飞书 webhook，跳过发送"，exit 0（不阻塞定时任务）。

环境: MYSTERY_DB_PATH / MYSTERY_REPORT_DIR（可缺省，用默认路径）。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

_DEFAULT_DB = "/home/ai/ai_runner/stock/data/db/mystery_cache.db"
_WEBHOOK_FILE = Path.home() / ".config" / "czsc_mi" / "feishu_webhook"


def _webhook() -> str:
    url = os.environ.get("FEISHU_WEBHOOK", "").strip()
    if url:
        return url
    try:
        if _WEBHOOK_FILE.exists():
            return _WEBHOOK_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return ""


def _db_path() -> str:
    return os.environ.get("MYSTERY_DB_PATH", "").strip() or _DEFAULT_DB


def _report_dir() -> str:
    d = os.environ.get("MYSTERY_REPORT_DIR", "").strip()
    if d:
        return d
    # 回退：czsc_mi/output（与 config.output_dir 默认一致）
    return str(Path(__file__).resolve().parents[1] / "output")


def _build_ok_message() -> str:
    db = _db_path()
    conn = sqlite3.connect(db)
    try:
        job = conn.execute(
            "SELECT id, trade_date, started_at, n_ok, n_fail "
            "FROM scan_jobs ORDER BY id DESC LIMIT 1").fetchone()
        if not job:
            return "⚠️ 定时任务完成，但库中无 scan_jobs 记录（扫描未写库？）"
        job_id, trade_date, started_at, n_ok, n_fail = job
        rows = conn.execute(
            "SELECT symbol, score, true_resonance, payload_json "
            "FROM scan_results WHERE job_id=? ORDER BY "
            "COALESCE(score, -1) DESC LIMIT 8", (job_id,)).fetchall()
        tr_rows = conn.execute(
            "SELECT symbol, score, payload_json FROM scan_results "
            "WHERE job_id=? AND true_resonance=1 ORDER BY COALESCE(score, -1) DESC",
            (job_id,)).fetchall()
    finally:
        conn.close()

    def _name(payload_json: str) -> str:
        try:
            return str(json.loads(payload_json).get("name") or "")
        except Exception:
            return ""

    lines = [f"📊 Mistery 趋势交易日报 {trade_date or ''}",
             f"✅ 扫描 {n_ok} 只（失败 {n_fail}）"]
    if rows:
        lines.append("🏆 评分 Top 8：")
        for i, (sym, score, _tr, payload) in enumerate(rows, 1):
            nm = _name(payload)
            lines.append(f"  {i}. {sym} {nm} 分={score}")
    if tr_rows:
        lines.append(f"🔔 真三振 {len(tr_rows)} 只：")
        for sym, score, payload in tr_rows:
            nm = _name(payload)
            lines.append(f"  · {sym} {nm} 分={score}")

    # 最新日报文件（xlsx/html 任取其一）
    try:
        files = sorted(Path(_report_dir()).glob("每日股票分析报告_*.xlsx"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        if files:
            lines.append(f"📁 报告：{files[0]}")
    except Exception:
        pass
    return "\n".join(lines)


def _send(text: str) -> bool:
    url = _webhook()
    if not url:
        print("未配置飞书 webhook（FEISHU_WEBHOOK 或 ~/.config/czsc_mi/feishu_webhook），跳过发送")
        print("---- 消息预览 ----")
        print(text)
        print("------------------")
        return False
    payload = json.dumps({"msg_type": "text", "content": {"text": text}},
                          ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        ok = "Success" in body or '"code":0' in body
        print(f"飞书发送: {'✅' if ok else '❌'} {body[:200]}")
        return ok
    except Exception as e:
        print(f"飞书发送异常: {e}")
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--error", default="", help="失败告警文案（否则发成功摘要）")
    args = ap.parse_args()

    if args.error:
        text = f"❌ Mistery 日报任务失败（{datetime.now():%Y-%m-%d %H:%M}）\n{args.error[:1500]}"
    else:
        text = _build_ok_message()
    _send(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
