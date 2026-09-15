#!/usr/bin/env bash
# weekly_shares.sh — W27d（008.md P1）：周日低频铺流通股本快照。
#
# 覆盖率的真正分母是全市场股本快照（当前 86/5559）。AGENTS 允许低频全市场，
# 禁止的是每个交易日 18:00 打竞价 —— 本脚本单独 cron（周日白天），
# 不进 daily_pipeline.sh。
#
# 顺序：先自选（短、必成）→ 再全市场。--fresh-skip-days 6 让上周内已刷过
# （含除权事件票）的票跳过 HTTP，批次随覆盖率上升自然收敛。
# 断点友好：失败重跑时，本轮已写入 as_of=今天的票被 fresh-skip 跳过。
# 预计初跑 50-60 批 auction final；停牌/无竞价价允许缺。
# 首日后可对已有快照票人工跑一次：czsc-mi sync-turnover --backfill-days 20
#（仍禁止 date < 该票快照 as_of）。--force 仅空库/事故。
set -uo pipefail
source /home/ai/ai_runner/.stockrc
source "${VENV:-/home/ai/ai_runner/venv}/bin/activate"
cd "${CZSC_MI_ROOT:-$(dirname "$0")/..}"
export MYSTERY_DB_PATH="${MYSTERY_DB_PATH:-/home/ai/ai_runner/stock/data/db/mystery_cache.db}"

LOG="${CZSC_MI_STATE_DIR:-$HOME/.local/state/czsc_mi}/weekly_shares.log"
mkdir -p "$(dirname "$LOG")"
exec >>"$LOG" 2>&1
echo "===== $(date '+%F %T') weekly_shares 开始 ====="

echo "[weekly_shares] 1/2 自选对账..."
czsc-mi sync-shares --watchlist --fresh-skip-days 6 \
  || echo "[weekly_shares] ⚠️ 自选对账失败/零拉取（fetched=0 也算非零退出，正常）"

echo "[weekly_shares] 2/2 全市场铺快照（100/批 auction final）..."
czsc-mi sync-shares --fresh-skip-days 6 \
  || echo "[weekly_shares] ⚠️ 全市场对账中断 —— 直接重跑本脚本即可续（已刷票自动跳过）"

echo -n "[weekly_shares] QA: "
/home/ai/ai_runner/venv/bin/python - <<'EOF'
from mystery.store.db import MysteryDB
qa = MysteryDB().turnover_qa_stats(__import__('datetime').date.today().isoformat())
print(f"market_n={qa['n_symbols']} market_with_shares={qa['n_with_shares']} "
      f"coverage={qa['coverage']}")
EOF
echo "===== $(date '+%F %T') weekly_shares 结束 ====="
