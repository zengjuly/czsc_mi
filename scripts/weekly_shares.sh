#!/usr/bin/env bash
# weekly_shares.sh — W27d/W28b（008.md P1 + 009.md W28b）：
# 周日低频铺流通股本快照，跑完可验收、铺不够非零退出。
#
# 覆盖率的真正分母是全市场股本快照。AGENTS 允许低频全市场，禁止的是每个
# 交易日 18:00 打竞价 —— 本脚本单独 cron（周日白天），不进 daily_pipeline.sh。
#
# 顺序：先自选（短、必成）→ 再全市场。--fresh-skip-days 6 让本周内已刷过
# （含除权事件票豁免）的票跳过 HTTP，批次随覆盖率上升自然收敛。
# 断点友好：失败重跑时，本轮已写入票被 fresh-skip 跳过。--force 不进本脚本。
#
# W28b 退出码（防「只跑完自选却报成功」）：
#   自选 with_shares < 自选只数×0.9           → exit 1
#   全市场 market_with_shares < 阈值           → exit 1
#     阈值默认 1000（停牌/无竞价价允许缺票），环境变量
#     WEEKLY_SHARES_MARKET_MIN 可配（首铺后按实际量级上调趋近 5559×0.9）。
# 状态文件 <output>/weekly_shares_status.json 供周一日报页脚展示。
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
  || echo "[weekly_shares] ⚠️ 自选对账有失败批次（fetched=0 也算非零退出，正常）"

echo "[weekly_shares] 2/2 全市场铺快照（100/批 auction final）..."
czsc-mi sync-shares --fresh-skip-days 6 \
  || echo "[weekly_shares] ⚠️ 全市场对账中断 —— 直接重跑本脚本即可续（已刷票自动跳过）"

# W28b：双口径 QA + 阈值判定 + 状态文件。python 退出码即脚本退出码。
/home/ai/ai_runner/venv/bin/python - <<'PYEOF'
import json
import os
from datetime import date

from mystery.config import output_dir
from mystery.store.db import MysteryDB
from mystery.services.watchlist import load_watchlist
from mystery.adapters.codes import db_code_of

today = date.today().isoformat()
db = MysteryDB()
try:
    wl_codes = [db_code_of(s) for s in load_watchlist()]
except Exception:
    wl_codes = []
w = db.turnover_qa_stats(today, codes=wl_codes or None)
m = db.turnover_qa_stats(today)
fmt = lambda s: (f"n={s['n_symbols']} with_shares={s['n_with_shares']} "
                 f"coverage={s['coverage']} fillable={s.get('fillable_coverage')}")
print(f"[weekly_shares] QA 自选: {fmt(w)}")
print(f"[weekly_shares] QA 全市场: {fmt(m)}")

market_min = int(os.environ.get('WEEKLY_SHARES_MARKET_MIN', '1000'))
wl_min = max(1, int(len(wl_codes) * 0.9)) if wl_codes else 1
exit_code = 0
reasons = []
if w['n_with_shares'] < wl_min:
    exit_code = 1
    reasons.append(f"自选有快照 {w['n_with_shares']} < {wl_min}"
                   f"（自选 {len(wl_codes)} 只×0.9）")
if m['n_with_shares'] < market_min:
    exit_code = 1
    reasons.append(f"全市场有快照 {m['n_with_shares']} < 阈值 {market_min}"
                   "（WEEKLY_SHARES_MARKET_MIN；首铺未完成或被限流中断）")

status = {'date': today,
          'watchlist_symbols': w['n_symbols'],
          'watchlist_with_shares': w['n_with_shares'],
          'watchlist_coverage': w['coverage'],
          'watchlist_fillable_coverage': w.get('fillable_coverage'),
          'market_symbols': m['n_symbols'],
          'market_with_shares': m['n_with_shares'],
          'market_coverage': m['coverage'],
          'market_min': market_min, 'exit': exit_code,
          'fail_reasons': reasons}
sp = os.path.join(output_dir(), 'weekly_shares_status.json')
with open(sp, 'w', encoding='utf-8') as f:
    json.dump(status, f, ensure_ascii=False, indent=1)
for r in reasons:
    print(f"[weekly_shares] ❌ {r}")
print(f"[weekly_shares] 状态已写 {sp}")
raise SystemExit(exit_code)
PYEOF
rc=$?
# 观测补丁（009 后续）：铺盘达标后自动做一次合法近端回填（纯本地计算，
# 不越 as_of、无 HTTP），省掉「周日铺完还得人工 backfill」一步。
if [ "$rc" -eq 0 ]; then
  echo "[weekly_shares] 3/3 近端回填 turn（backfill-days 20，不越 as_of）..."
  czsc-mi sync-turnover --backfill-days 20 | tail -c 300 \
    || echo "[weekly_shares] ⚠️ 回填步骤失败（不影响铺盘结果，可人工重跑）"
fi
echo "===== $(date '+%F %T') weekly_shares 结束 exit=$rc ====="
exit $rc
