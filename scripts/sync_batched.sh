#!/usr/bin/env bash
# sync_batched.sh - 分批全市场日线同步，规避小内存机 OOM（W115）
#
# 背景：czsc-mi sync 逐股遍历 5559 只，单进程内存累积到 666MB，叠加常驻
# Web 服务（MemoryMax 700M）在 951M 物理内存机上触发 OOM killer。
# 方案：把证券列表切成若干批，每批一个独立进程跑完即退出释放内存；
# 借助 sync 的 checkpoint（sync_checkpoint.json）实现断点续跑——已同步的
# 票在后续批次会被 done 跳过，因此重复扫描代价极低。
#
# 用法：bash scripts/sync_batched.sh [--days N] [--batch-size N]
# 环境变量：
#   SYNC_DAYS      日线回看天数（默认 30）
#   SYNC_BATCH_SIZE 每批股票数（默认 1000；越小越省内存、批次越多）
#   VENV / CZSC_MI_ROOT / MYSTERY_DB_PATH 由调用方注入
set -uo pipefail
ROOT="${CZSC_MI_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$ROOT"
DAYS="${SYNC_DAYS:-30}"
BATCH="${SYNC_BATCH_SIZE:-1000}"
VENV_BIN="${VENV:-$(dirname "$ROOT")/venv}/bin"

echo "[sync-batched] 回看 ${DAYS} 天，每批 ${BATCH} 只"

# 从本地库取全市场代码（内部格式 sh.600000），写成临时列表
LIST_FILE="$(mktemp)"
trap 'rm -f "$LIST_FILE"' EXIT
"${VENV_BIN}/python" -c "
from mystery.services.analyze import AnalysisService
from mystery.config import load_config
s = AnalysisService(load_config())
for it in s.market.db.get_stock_list():
    print(it['code'])
" > "$LIST_FILE" || { echo "[sync-batched] 获取证券列表失败"; exit 1; }

TOTAL=$(wc -l < "$LIST_FILE")
echo "[sync-batched] 证券总数: ${TOTAL}"

RC=0
 OFFSET=0
BATCH_NO=0
while [ "$OFFSET" -lt "$TOTAL" ]; do
  BATCH_NO=$((BATCH_NO+1))
  # 取本批代码（每行一个，转成 --symbols 的参数数组）
  mapfile -t SYMS < <(sed -n "$((OFFSET+1)),$((OFFSET+BATCH))p" "$LIST_FILE")
  [ "${#SYMS[@]}" -eq 0 ] && break
  echo "[sync-batched] 批次 ${BATCH_NO}: ${#SYMS[@]} 只 (offset ${OFFSET})"
  nice -n 5 "${VENV_BIN}/czsc-mi" sync --period daily --days "$DAYS" \
    --symbols "${SYMS[@]}" >/dev/null 2>&1 || RC=$?
  OFFSET=$((OFFSET+BATCH))
done

echo "[sync-batched] 完成，共 ${BATCH_NO} 批，rc=${RC}"
exit "$RC"
