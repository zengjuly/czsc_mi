#!/usr/bin/env bash
# 一键升级 czsc_mi 的纯 pip 数据/展示依赖（pandas/mootdx/baostock/duckdb/streamlit/plotly 等）— W104
# czsc 单独升级（见 upgrade_czsc.sh）；本脚本处理其余 pip 库。
# 用法: ./upgrade_pip_libs.sh [--dry-run] [--no-rollback]
set -uo pipefail

COMPONENT="pip-libs"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/_common.sh"

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --no-rollback) NO_ROLLBACK=1 ;;
    *) echo "未知参数: $arg"; exit 2 ;;
  esac
done

banner

# pkg:import_name 配对；import_name 含点时取顶层模块
LIBS=(
  "pandas:pandas"
  "openpyxl:openpyxl"
  "PyYAML:yaml"
  "streamlit:streamlit"
  "plotly:plotly"
  "mootdx:mootdx"
  "baostock:baostock"
  "duckdb:duckdb"
  "pypinyin:pypinyin"
  "requests:requests"
)

FAILED=0
for pair in "${LIBS[@]}"; do
  pkg="${pair%%:*}"
  mod="${pair#*:}"
  mod="${mod%%.*}"
  echo ""
  pip_upgrade "$pkg" "$mod" || { warn "${pkg} 升级失败，继续下一个"; FAILED=1; }
done

if [ "$FAILED" = "1" ]; then
  die "部分 pip 库升级失败，请查看日志 ${CURRENT_LOG}"
fi
log "🎉 ${COMPONENT} 升级脚本结束"
