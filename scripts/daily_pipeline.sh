#!/usr/bin/env bash
# daily_pipeline.sh - 每日一条龙：同步 → 日报（自选）；全市场扫描可选（005.md 分离）
#
# 路径全部从脚本自身位置推导 + 环境变量覆盖；禁止任何本机绝对路径硬编码。
#
# 用法:
#   scripts/daily_pipeline.sh
# 可覆盖:
#   VENV / CZSC_MI_ROOT / MYSTERY_DB_PATH / MYSTERY_OUTPUT_DIR
#   THS_FUYAO_SCRIPT / THS_MARKETDB_DIR / TDX_API_URL / TDX_VIPDOC_DIR
#   RUN_MARKET_SCAN（默认 0：不跑全市场扫描；=1 时用 SCAN_LIMIT 只数）
#   SCAN_LIMIT
#   RUN_DATA_AUDIT（W81 默认 0：不跑数据质量审计；=1 时 czsc-mi audit 一次，
#     生产库约 4 分钟，超时 DATA_AUDIT_TIMEOUT 秒可配，失败不阻塞主流程）
#   RUN_CLEANUP（W101 默认 1：随管线定时清理历史数据；=0 关闭，
#     超时 CLEANUP_TIMEOUT 秒可配，策略由 MYSTERY_CLEAN_* env 控制，失败不阻塞）
#   SYNC_DAYS（W115 默认 30：日线同步回看天数。本地 DuckDB 已有 10 年历史、
#     SQLite 已灌满，日常只需小天数增量；365 全量在 951M 小内存机上会 OOM。
#     新系统首次建库后如需补历史，可临时设 SYNC_DAYS=365 单独手动跑一次）
set -euo pipefail
# W41：锁死北京时区——脚本用 date '+%F' 给 sync-turnover 传当日日期，
# 机器 TZ 若是 UTC 会错一天。
export TZ=Asia/Shanghai
# 路径从脚本自身位置推导，可用环境变量覆盖（O1/O2 通用，禁止硬编码绝对路径）
ROOT="${CZSC_MI_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
STOCK_DIR="$(dirname "$ROOT")"
BASE_DIR="$(dirname "$STOCK_DIR")"
# venv 位置探测：O1 在 $BASE_DIR/venv，O2 在 $STOCK_DIR/venv；兼容两机布局差异
VENV_BASE="$BASE_DIR/venv"
[ ! -x "$VENV_BASE/bin/czsc-mi" ] && [ -x "$STOCK_DIR/venv/bin/czsc-mi" ] && VENV_BASE="$STOCK_DIR/venv"

source "${CZSC_MI_STOCKRC:-$BASE_DIR/.stockrc}"
source "${VENV:-$VENV_BASE}/bin/activate"
cd "$ROOT"

export MYSTERY_DB_PATH="${MYSTERY_DB_PATH:-$STOCK_DIR/data/db/mystery_cache.db}"
# 两个缠论开关显式声明（W20）：结构展示默认开，混合分默认关（综合分=Mystery 1.22.30）
export MYSTERY_CHAN_ENABLED="${MYSTERY_CHAN_ENABLED:-1}"
export MYSTERY_CHAN_SCORE="${MYSTERY_CHAN_SCORE:-0}"
export THS_FUYAO_SCRIPT="${THS_FUYAO_SCRIPT:-$STOCK_DIR/Financial-API/python/toolkit/fuyao/scripts/fuyao.py}"
export THS_MARKETDB_DIR="${THS_MARKETDB_DIR:-$STOCK_DIR/Financial-API/data}"

SYNC_DAYS="${SYNC_DAYS:-30}"
echo "[daily_pipeline] 1/5 同步行情（日线最近 ${SYNC_DAYS} 天，分批增量）..."
# W115：小内存机（951M）单次全量 sync 逐股累积到 666MB，叠加常驻 Web 服务
# 会触发 OOM killer（rc=137），日报中断。改用分批脚本：每批独立进程跑完释放
# 内存，借助 sync_checkpoint 断点续跑。6 批 × 1000 只 ≈ 9 分钟，零 OOM。
# 老系统若无 sync_batched.sh，回退到单次 nice 运行。
if [ -x "${ROOT}/scripts/sync_batched.sh" ]; then
  SYNC_DAYS="${SYNC_DAYS}" SYNC_BATCH_SIZE="${SYNC_BATCH_SIZE:-1000}" \
    bash "${ROOT}/scripts/sync_batched.sh" || { rc=$?; exit "$rc"; }
else
  nice -n 5 czsc-mi sync --period daily --days "${SYNC_DAYS}" || { rc=$?
    if [ "$rc" -eq 3 ]; then
      echo "[daily_pipeline] 已有 sync 任务进行中（W49 写库互斥），本管线让路退出"
    fi
    exit "$rc"; }
fi

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

# W81（可选）数据质量审计：换手全 0 / OHLC 异常 / 重复主键告警。
# 默认关（生产库 900 万行全量约 4 分钟）；RUN_DATA_AUDIT=1 开启，
# 结果写 <报告目录>/audit_日期.log，摘要进 pipeline_status.json，失败不阻塞。
AUDIT_OK=skip
if [ "${RUN_DATA_AUDIT:-0}" = "1" ]; then
  echo "[daily_pipeline] 4.5/5 数据质量审计（audit，RUN_DATA_AUDIT=1）..."
  AUDIT_LOG="$(dirname "${STATUS_JSON}")/audit_$(date '+%F').log"
  if timeout "${DATA_AUDIT_TIMEOUT:-900}" czsc-mi audit --period daily \
      > "${AUDIT_LOG}" 2>&1; then
    AUDIT_OK=ok
    grep -E "重复主键|收盘价|OHLC|额缺失|跳空|换手缺失" "${AUDIT_LOG}" \
      | sed 's/^/[audit] /' || true
    grep -q "疑似数据源" "${AUDIT_LOG}" && AUDIT_OK=warn
  else
    AUDIT_OK=fail
    echo "[daily_pipeline] ⚠️ audit 失败/超时（不阻塞主流程），日志: ${AUDIT_LOG}"
  fi
fi

# W27b：状态落地（scan --report 页脚与 feishu_notify 读取同一文件）
cat > "${STATUS_JSON}" <<EOF
{"date": "$(date '+%F')", "sector_kline_sync": "${SECTOR_OK}", "shares_refresh": "${SHARES_OK}", "turnover_derive": "${TURNOVER_OK}", "data_audit": "${AUDIT_OK}"}
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

# W101 定时清理历史数据（默认随管线每日跑，失败不阻塞主流程）。
# 放在扫描之后：确保只清理"已完成"的旧 job，刚写的 job 在保留期内不会被删。
# 策略 env：MYSTERY_CLEAN_SCAN_DAYS(30)/REPLAY_DAYS(180)/CACHE_DAYS(30)/
#           KLINE_CAP(2000)/VACUUM_RATIO(0.15)；均可用 CLI 参数覆盖。
# VACUUM 仅当空闲页占比超阈值才触发（低峰期、锁库数分钟），日常多为跳过。
CLEANUP_OK=skip
if [ "${RUN_CLEANUP:-1}" = "1" ]; then
  echo "[daily_pipeline] 5.6/5 清理历史数据（过期扫描/陈旧缓存/冗余日K + 阈值VACUUM）..."
  CLEANUP_LOG="$(dirname "${STATUS_JSON}")/cleanup_$(date '+%F').log"
  if timeout "${CLEANUP_TIMEOUT:-900}" czsc-mi cleanup --apply > "${CLEANUP_LOG}" 2>&1; then
    CLEANUP_OK=ok
    grep -E "扫描历史|陈旧缓存|冗余日K|VACUUM" "${CLEANUP_LOG}" | sed 's/^/[cleanup] /' || true
  else
    CLEANUP_OK=fail
    echo "[daily_pipeline] ⚠️ cleanup 失败/超时（不阻塞主流程），日志: ${CLEANUP_LOG}"
  fi
  # 清理在 4.5 步写状态之后才跑完，这里回写 history_cleanup 字段供 Web/飞书观测
  if [ -f "${STATUS_JSON}" ]; then
    "${VENV:-$VENV_BASE}/bin/python" - "${STATUS_JSON}" "${CLEANUP_OK}" <<'PYEOF' 2>/dev/null || true
import json, sys
p, val = sys.argv[1], sys.argv[2]
try:
    d = json.load(open(p, encoding="utf-8"))
    d["history_cleanup"] = val
    json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False)
except Exception:
    pass
PYEOF
  fi
fi

echo "[daily_pipeline] ✅ 完成"

# 报告入 git（xlsx 经 GitHub raw 链接发给用户；失败不阻塞主流程）
REPORT_GIT_DIR="${MYSTERY_REPORT_DIR:-$STOCK_DIR/output}"
git -C "${REPORT_GIT_DIR}" add -A 2>/dev/null || true
git -C "${REPORT_GIT_DIR}" commit -m "daily report $(date +%Y%m%d)" 2>/dev/null || true
git -C "${REPORT_GIT_DIR}" push origin main 2>/dev/null \
  || echo "[daily_pipeline] ⚠️ git push 失败（报告仍在本地 ${REPORT_GIT_DIR}）"
