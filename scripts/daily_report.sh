#!/usr/bin/env bash
# daily_report.sh - 纯命令行：同步 → 生成日报到 stock/output → 输出 xlsx 路径供 agent 发送
set -uo pipefail
source /home/ai/ai_runner/.stockrc
export VENV=/home/ai/ai_runner/venv
export MYSTERY_DB_PATH=/home/ai/ai_runner/stock/data/db/mystery_cache.db
export THS_FUYAO_SCRIPT=/home/ai/ai_runner/stock/Financial-API/python/toolkit/fuyao/scripts/fuyao.py
export THS_MARKETDB_DIR=/home/ai/ai_runner/stock/Financial-API/data
export MYSTERY_REPORT_DIR=/home/ai/ai_runner/stock/output
export HITHINK_FINANCE_API_KEY=$(grep hithink_api_key /home/ai/ai_runner/stock/czsc_mi/config/config.yaml | awk '{print $2}')

cd /home/ai/ai_runner/stock/czsc_mi
source ${VENV}/bin/activate

# 1) 同步行情
czsc-mi sync --period daily --days 365

# 2) 生成日报
czsc-mi daily --watchlist

# 3) 找到今天的 xlsx 并 git 推送
TODAY=$(date +%Y%m%d)
XLSX="${MYSTERY_REPORT_DIR}/每日股票分析报告_${TODAY}.xlsx"
HTML="${MYSTERY_REPORT_DIR}/每日股票分析报告_${TODAY}.html"

cd /home/ai/ai_runner/stock/output
for f in "$XLSX" "$HTML"; do
  if [ -f "$f" ]; then
    git add "$f" || true
  fi
done
git commit -m "daily report ${TODAY}" --allow-empty || true
git push origin main

if [ ! -f "$XLSX" ]; then
  echo "ERROR: xlsx not found" >&2
  exit 1
fi

XLSX_URL="https://github.com/zengjuly/misteryresult/blob/main/每日股票分析报告_${TODAY}.xlsx"

# 发送飞书消息（interactive card 格式，链接可点击）
WEBHOOK_FILE="${HOME}/.config/czsc_mi/feishu_webhook"
if [ -f "$WEBHOOK_FILE" ]; then
  WEBHOOK="$(cat "$WEBHOOK_FILE" | tr -d '[:space:]')"
  if [ -n "$WEBHOOK" ]; then
    curl -s -X POST "$WEBHOOK" \
      -H 'Content-Type: application/json' \
      -d "{
        \"msg_type\": \"interactive\",
        \"card\": {
          \"header\": {
            \"title\": {\"tag\": \"plain_text\", \"content\": \"📊 每日股票分析报告 ${TODAY}\"},
            \"template\": \"green\"
          },
          \"elements\": [{
            \"tag\": \"markdown\",
            \"content\": \"✅ 分析报告已生成\\n\\n📥 [点击下载 xlsx 报告](${XLSX_URL})\"
          }]
        }
      }"
    echo ""
    echo "飞书消息已发送"
  fi
fi

echo "📊 每日股票分析报告已完成（${TODAY}）"
echo "下载链接: ${XLSX_URL}"
