#!/usr/bin/env bash
# 一键升级 ths（同花顺 Financial-API，gitee git 仓库）— W104
# 该仓库提供 fuyao 脚本 + marketdb duckdb 数据源；升级=git pull，不改动 data/ 下数据。
# 用法: ./upgrade_ths.sh [--dry-run] [--no-rollback]
set -uo pipefail

COMPONENT="ths"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/_common.sh"

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --no-rollback) NO_ROLLBACK=1 ;;
    *) echo "未知参数: $arg"; exit 2 ;;
  esac
done

THS_DIR="${THS_DIR:-${STOCK_DIR}/Financial-API}"
FUYAO="$THS_DIR/python/toolkit/fuyao/scripts/fuyao.py"

banner

verify_ths() {
  [ -f "$FUYAO" ] && log "    fuyao 脚本存在" && return 0
  warn "fuyao 脚本缺失: $FUYAO"
  return 1
}

git_upgrade "$THS_DIR" "" "origin" verify_ths
log "🎉 ${COMPONENT} 升级脚本结束（数据目录 data/ 未改动）"
