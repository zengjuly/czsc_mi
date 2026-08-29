#!/usr/bin/env bash
# daily_pipeline.sh - 每日一条龙：同步 → 日报（自选）；全市场扫描可选（005.md 分离）
#
# 路径全部来自环境变量；本机默认值只写在这里（scripts/ 允许），
# Python 业务代码禁止硬编码绝对路径。
#
# 用法:
#   scripts/daily_pipeline.sh
# 可覆盖:
#   VENV / CZSC_MI_ROOT / MYSTERY_DB_PATH / MYSTERY_OUTPUT_DIR
#   THS_FUYAO_SCRIPT / THS_MARKETDB_DIR / TDX_API_URL / TDX_VIPDOC_DIR
#   RUN_MARKET_SCAN（默认 0：不跑全市场扫描；=1 时用 SCAN_LIMIT 只数）
#   SCAN_LIMIT
set -euo pipefail

source "${VENV:-/home/ai/ai_runner/venv}/bin/activate"
cd "${CZSC_MI_ROOT:-$(dirname "$0")/..}"

export MYSTERY_DB_PATH="${MYSTERY_DB_PATH:-/home/ai/ai_runner/stock/data/db/mystery_cache.db}"
export THS_FUYAO_SCRIPT="${THS_FUYAO_SCRIPT:-/home/ai/ai_runner/stock/Financial-API/python/toolkit/fuyao/scripts/fuyao.py}"
export THS_MARKETDB_DIR="${THS_MARKETDB_DIR:-/home/ai/ai_runner/stock/Financial-API/data}"

echo "[daily_pipeline] 1/2 同步行情（日线 365 天）..."
czsc-mi sync --period daily --days 365

echo "[daily_pipeline] 2/2 生成日报（自选，Excel/HTML）..."
czsc-mi daily --watchlist

# 全市场扫描（可选，默认关）：真三振池页面只读 latest_scan_job，
# 不要用自选扫描冒充全市场；缺 job 时页面提示手动跑 scan --limit。
if [ "${RUN_MARKET_SCAN:-0}" = "1" ]; then
  echo "[daily_pipeline] 全市场扫描（写 scan_jobs/scan_results）..."
  czsc-mi scan --limit "${SCAN_LIMIT:-100}"
fi

echo "[daily_pipeline] ✅ 完成"
