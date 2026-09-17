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
source /home/ai/ai_runner/.stockrc
source "${VENV:-/home/ai/ai_runner/venv}/bin/activate"
cd "${CZSC_MI_ROOT:-$(dirname "$0")/..}"

export MYSTERY_DB_PATH="${MYSTERY_DB_PATH:-/home/ai/ai_runner/stock/data/db/mystery_cache.db}"
# 两个缠论开关显式声明（W20）：结构展示默认开，混合分默认关（综合分=Mystery 1.22.30）
export MYSTERY_CHAN_ENABLED="${MYSTERY_CHAN_ENABLED:-1}"
export MYSTERY_CHAN_SCORE="${MYSTERY_CHAN_SCORE:-0}"
export THS_FUYAO_SCRIPT="${THS_FUYAO_SCRIPT:-/home/ai/ai_runner/stock/Financial-API/python/toolkit/fuyao/scripts/fuyao.py}"
export THS_MARKETDB_DIR="${THS_MARKETDB_DIR:-/home/ai/ai_runner/stock/Financial-API/data}"

echo "[daily_pipeline] 1/5 同步行情（日线 365 天）..."
czsc-mi sync --period daily --days 365

# W39 板块指数日K增量（三振行业腿数据源）：fuyao index-historical 归一通道，
# 每板块从库内断点前扩 15 天幂等 UPSERT；失败不阻塞主流程。
echo "[daily_pipeline] 2/5 同步板块指数日K（sector-sync-kline）..."
if czsc-mi sector-sync-kline --days 15; then SECTOR_OK=ok; else \
  echo "[daily_pipeline] ⚠️ sector-sync-kline 失败（不阻塞主流程）"; fi

# W26c（007.md）股本事件触发重拉：本地 MarketDB raw_adjustment_events 筛
# 送转/增发事件票，无事件零 HTTP 秒回（可安全每日跑）；有事件只重拉命中票，
# 事件票即使股本变化 ≤3% 也写新 as_of 锚点。周对账（3% 门槛）仍单独 cron。
# W27b（008.md P3）：2/3 步失败仍不阻塞 scan，但退出码写入 pipeline_status.json
# （报告目录），日报页脚与飞书正文读取展示，不再静默。
STATUS_JSON="${MYSTERY_REPORT_DIR:-$(pwd)/output}/pipeline_status.json"
mkdir -p "$(dirname "${STATUS_JSON}")"
SHARES_OK=fail; TURNOVER_OK=fail; SECTOR_OK=${SECTOR_OK:-fail}
echo "[daily_pipeline] 3/5 除权事件股本重拉（无事件零 HTTP）..."
if czsc-mi sync-shares --from-adjustments; then SHARES_OK=ok; else \
  echo "[daily_pipeline] ⚠️ sync-shares --from-adjustments 失败（不阻塞主流程）"; fi

# W22 换手派生：纯本地（读股本快照 → 回算当日空 turn + ≤5日洞），不打 HTTP。
# 股本快照本身低频：每周单独 cron 跑 `czsc-mi sync-shares --watchlist`
#（全市场加 --force），不进 18:00 主链。`--backfill-days` 只挂 CLI 手动，不进管线。
echo "[daily_pipeline] 4/5 回算换手率（turn IS NULL → calc_float/ffill）..."
if czsc-mi sync-turnover --date "$(date '+%F')"; then TURNOVER_OK=ok; else \
  echo "[daily_pipeline] ⚠️ sync-turnover 失败（不阻塞主流程）"; fi

# W27b：状态落地（scan --report 页脚与 feishu_notify 读取同一文件）
cat > "${STATUS_JSON}" <<EOF
{"date": "$(date '+%F')", "sector_kline_sync": "${SECTOR_OK}", "shares_refresh": "${SHARES_OK}", "turnover_derive": "${TURNOVER_OK}"}
EOF

echo "[daily_pipeline] 5/5 后台扫描自选股（落 scan_jobs/scan_results）+ 生成日报（Excel/HTML）..."
# W17：从 `daily --watchlist`（只出报告不落库）改为 `scan --watchlist --report`——
# 自选股走 scan_market 落库，Web 真三振池/扫描页可查，同时生成 Excel/HTML 日报
# （文件名与 daily 一致，飞书 xlsx 链接与 git push 段无需改动）。
czsc-mi scan --watchlist --report

# 全市场扫描（可选，默认关）：真三振池页面只读 latest_scan_job，
# 不要用自选扫描冒充全市场；缺 job 时页面提示手动跑 scan --limit。
if [ "${RUN_MARKET_SCAN:-0}" = "1" ]; then
  echo "[daily_pipeline] 全市场扫描（写 scan_jobs/scan_results）..."
  czsc-mi scan --limit "${SCAN_LIMIT:-100}"
fi

echo "[daily_pipeline] ✅ 完成"

# 报告入 git（xlsx 经 GitHub raw 链接发给用户；失败不阻塞主流程）
REPORT_GIT_DIR="${MYSTERY_REPORT_DIR:-/home/ai/ai_runner/stock/output}"
git -C "${REPORT_GIT_DIR}" add -A 2>/dev/null || true
git -C "${REPORT_GIT_DIR}" commit -m "daily report $(date +%Y%m%d)" 2>/dev/null || true
git -C "${REPORT_GIT_DIR}" push origin main 2>/dev/null \
  || echo "[daily_pipeline] ⚠️ git push 失败（报告仍在本地 ${REPORT_GIT_DIR}）"
