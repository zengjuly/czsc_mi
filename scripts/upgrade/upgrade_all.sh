#!/usr/bin/env bash
# 一键升级所有外部依赖（W104）— 按顺序调用各组件升级脚本
# 顺序：pip 基础库 → czsc → ths → tdx(会重启容器) → hermes
# 用法:
#   ./upgrade_all.sh                # 全部升级
#   ./upgrade_all.sh --dry-run      # 预演，只打印不执行
#   ./upgrade_all.sh --only tdx     # 只升级某个组件
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DRY_RUN=0
NO_ROLLBACK=0
ONLY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --no-rollback) NO_ROLLBACK=1; shift ;;
    --only) ONLY="${2:-}"; shift 2 ;;
    *) echo "未知参数: $1 (支持 --dry-run / --no-rollback / --only <组件>)"; exit 2 ;;
  esac
done

# 组件 -> 脚本映射
declare -A SCRIPTS=(
  [pip-libs]="upgrade_pip_libs.sh"
  [czsc]="upgrade_czsc.sh"
  [ths]="upgrade_ths.sh"
  [tdx]="upgrade_tdx.sh"
  [hermes]="upgrade_hermes.sh"
)
ORDER=(pip-libs czsc ths tdx hermes)

if [ -n "$ONLY" ]; then
  ORDER=("$ONLY")
  [ -n "${SCRIPTS[$ONLY]:-}" ] || { echo "未知组件: $ONLY (可选: ${!SCRIPTS[@]})"; exit 2; }
fi

ARGS=()
[ "$DRY_RUN" = "1" ] && ARGS+=(--dry-run)
[ "$NO_ROLLBACK" = "1" ] && ARGS+=(--no-rollback)

echo "============================================================"
echo " 外部依赖批量升级  $(date '+%F %T')"
echo " 组件: ${ORDER[*]}"
echo " 模式: $([ "$DRY_RUN" = "1" ] && echo DRY-RUN || echo 实际执行)"
echo "============================================================"

FAILED=()
for comp in "${ORDER[@]}"; do
  script="${SCRIPTS[$comp]}"
  echo ""
  echo "──────────▶ 升级 ${comp} (${script}) ──────────"
  if bash "${HERE}/${script}" "${ARGS[@]}"; then
    echo "──────────✔ ${comp} 完成 ──────────"
  else
    echo "──────────✘ ${comp} 失败 ──────────"
    FAILED+=("$comp")
  fi
done

echo ""
echo "============================================================"
if [ ${#FAILED[@]} -eq 0 ]; then
  echo "🎉 全部组件升级成功"
else
  echo "⚠️  以下组件升级失败: ${FAILED[*]}"
  echo "   请查看 logs/upgrade_<组件>.log 排查"
  exit 1
fi
