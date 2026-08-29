#!/usr/bin/env bash
# daily_pipeline.sh - 每日一条龙：同步 → 扫描 → 日报（002.md W3-B）
#
# 路径全部来自环境变量；本机默认值只写在这里（scripts/ 允许），
# Python 业务代码禁止硬编码绝对路径。
#
# 用法:
#   scripts/daily_pipeline.sh
# 可覆盖:
#   VENV / CZSC_MI_ROOT / MYSTERY_DB_PATH / MYSTERY_OUTPUT_DIR
#   THS_FUYAO_SCRIPT / THS_MARKETDB_DIR / TDX_API_URL / TDX_VIPDOC_DIR
set -euo pipefail

source "${VENV:-/home/ai/ai_runner/venv}/bin/activate"
cd "${CZSC_MI_ROOT:-$(dirname "$0")/..}"

export MYSTERY_DB_PATH="${MYSTERY_DB_PATH:-/home/ai/ai_runner/stock/data/db/mystery_cache.db}"
export THS_FUYAO_SCRIPT="${THS_FUYAO_SCRIPT:-/home/ai/ai_runner/stock/Financial-API/python/toolkit/fuyao/scripts/fuyao.py}"
export THS_MARKETDB_DIR="${THS_MARKETDB_DIR:-/home/ai/ai_runner/stock/Financial-API/data}"

echo "[daily_pipeline] 1/3 同步行情（日线 365 天）..."
czsc-mi sync --period daily --days 365

echo "[daily_pipeline] 2/3 全市场扫描（写 scan_jobs/scan_results）..."
czsc-mi scan

echo "[daily_pipeline] 3/3 生成日报（Excel/HTML）..."
czsc-mi daily --watchlist

echo "[daily_pipeline] ✅ 完成"
