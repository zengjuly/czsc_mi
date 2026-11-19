#!/bin/bash
# minute_macd_watch.sh - 盘中自选股30/60min MACD金叉死叉监控（W103）
#
# cron 在每小时 :01/:31 触发，脚本内精确校验交易时段（9:31-11:31 / 13:31-15:31）：
#   1,31 9-15 * * 1-5  （配合脚本内时段窗口校验，非窗口时间自动跳过）
#
# 流程：同步自选30m+60m K线 → 检测状态变更 → 有金叉/死叉变更才发Hermes飞书通知。
# 只处理自选股、只分析MACD、只报变更，避免资源浪费。
# 防重入：flock 锁，避免上一轮未完又触发。

set -uo pipefail

BASE=/home/ai/ai_runner/stock/czsc_mi
CFG=/home/ai/ai_runner/.stockrc
VENV=/home/ai/ai_runner/venv/bin/czsc-mi
LOG=/home/ai/ai_runner/stock/czsc_mi/logs/minute_macd_watch.log
LOCK=/tmp/czsc_minute_macd.lock
mkdir -p "$(dirname "$LOG")"

# 防重入：已有一轮在跑则跳过
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "[$(date '+%F %T')] 上一轮仍在运行，跳过" >> "$LOG"
  exit 0
fi

# 精确时段校验：仅 9:31-11:31 / 13:31-15:31（cron 整点/半点触发）
HHMM=$(date '+%H%M')
in_window=0
if [ "$HHMM" -ge 931 ] && [ "$HHMM" -le 1131 ]; then in_window=1; fi
if [ "$HHMM" -ge 1331 ] && [ "$HHMM" -le 1531 ]; then in_window=1; fi
if [ "$in_window" -ne 1 ]; then
  echo "[$(date '+%F %T')] 非监控时段($HHMM)，跳过" >> "$LOG"
  exit 0
fi

# 交易日校验（周末跳过；节假日交由 tdx 数据源返回空自然无变更）
DOW=$(date '+%u')
if [ "$DOW" -gt 5 ]; then
  echo "[$(date '+%F %T')] 周末跳过" >> "$LOG"
  exit 0
fi

set -a; source "$CFG"; set +a
export MYSTERY_DB_PATH="${MYSTERY_DB_PATH:-/home/ai/ai_runner/stock/data/db/mystery_cache.db}"

echo "[$(date '+%F %T')] ===== MACD监控开始 =====" >> "$LOG"
cd "$BASE" || exit 1
# run = sync + detect + 有变更才通知（首次全量上报基线）
"$VENV" minute-macd run >> "$LOG" 2>&1
rc=$?
echo "[$(date '+%F %T')] MACD监控结束 rc=$rc =====" >> "$LOG"
exit $rc
