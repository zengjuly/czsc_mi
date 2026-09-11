#!/usr/bin/env bash
# daily_feishu.sh - 系统定时任务入口（cron）：同步 → 扫描 → 日报 → 飞书通知。
#
# 全程无 LLM：只调 czsc-mi CLI + 本仓库脚本。失败也会发飞书告警。
#
# 使用:
#   crontab -e 添加（默认工作日 18:00）:
#     0 18 * * 1-5 /home/ai/ai_runner/stock/czsc_mi/scripts/daily_feishu.sh
#
# 可覆盖:
#   VENV / CZSC_MI_ROOT / MYSTERY_DB_PATH / MYSTERY_REPORT_DIR
#   THS_FUYAO_SCRIPT / THS_MARKETDB_DIR / TDX_API_URL / TDX_VIPDOC_DIR
#   FEISHU_WEBHOOK（或用 ~/.config/czsc_mi/feishu_webhook 文件）
#
# 日志: ~/.local/state/czsc_mi/daily.log
set -uo pipefail

ROOT="${CZSC_MI_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
LOG_DIR="${HOME}/.local/state/czsc_mi"
LOG="${LOG_DIR}/daily.log"
mkdir -p "${LOG_DIR}"

# 按日归档：日志文件不含今天的「开始」记录时整体归档（同日重跑直接 append）
if [ -s "${LOG}" ]; then
  last_date="$(grep -oE '^===== [0-9]{4}-[0-9]{2}-[0-9]{2}' "${LOG}" | tail -1 | awk '{print $2}')"
  if [ -n "${last_date}" ] && [ "${last_date}" != "$(date '+%F')" ]; then
    mv "${LOG}" "${LOG_DIR}/daily_${last_date}.log"
  fi
fi

export VENV="${VENV:-/home/ai/ai_runner/venv}"
export MYSTERY_DB_PATH="${MYSTERY_DB_PATH:-/home/ai/ai_runner/stock/data/db/mystery_cache.db}"
export THS_FUYAO_SCRIPT="${THS_FUYAO_SCRIPT:-/home/ai/ai_runner/stock/Financial-API/python/toolkit/fuyao/scripts/fuyao.py}"
export THS_MARKETDB_DIR="${THS_MARKETDB_DIR:-/home/ai/ai_runner/stock/Financial-API/data}"
export MYSTERY_REPORT_DIR="${MYSTERY_REPORT_DIR:-/home/ai/ai_runner/stock/output}"
# API Key 不写入仓库：由 .stockrc / config.yaml 顶层 hithink_api_key 注入（ths.py 启动时读）


{
  echo "===== $(date '+%F %T') 开始 ====="
  source /home/ai/ai_runner/.stockrc
  cd "${ROOT}"
  bash scripts/daily_pipeline.sh
  rc=$?
  echo "[daily_feishu] pipeline 返回码: ${rc}"

  source "${VENV}/bin/activate"
  if [ "${rc}" -eq 0 ]; then
    python scripts/feishu_notify.py
  else
    # 失败告警：附日志最后 15 行
    tail_err="$(tail -15 "${LOG}" | sed 's/^/  /')"
    python scripts/feishu_notify.py --error "pipeline 退出码 ${rc}

${tail_err}"
  fi
  echo "===== $(date '+%F %T') 结束 ====="
} >> "${LOG}" 2>&1

exit "${rc}"
