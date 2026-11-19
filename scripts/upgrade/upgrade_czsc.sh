#!/usr/bin/env bash
# 一键升级 czsc（缠论内核，pip 包）— W104
# 用法: ./upgrade_czsc.sh [--dry-run] [--no-rollback]
set -uo pipefail

COMPONENT="czsc"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/_common.sh"

# 参数解析
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --no-rollback) NO_ROLLBACK=1 ;;
    *) echo "未知参数: $arg (支持 --dry-run / --no-rollback)"; exit 2 ;;
  esac
done

banner
pip_upgrade "czsc" "czsc" || die "czsc 升级流程未完成"
log "🎉 ${COMPONENT} 升级脚本结束"
