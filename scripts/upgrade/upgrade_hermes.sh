#!/usr/bin/env bash
# 一键升级 hermes-agent（Hermes 飞速通道，git + 独立 venv）— W104
# 升级=git pull → 用其独立 venv 重装 Python 依赖 → hermes --version 校验 → 失败回滚。
# 注意：网关服务已禁用（W96），本脚本只升级 send 通道，不启动 gateway。
# 用法: ./upgrade_hermes.sh [--dry-run] [--no-rollback]
set -uo pipefail

COMPONENT="hermes"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/_common.sh"

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --no-rollback) NO_ROLLBACK=1 ;;
    *) echo "未知参数: $arg"; exit 2 ;;
  esac
done

HERMES_DIR="${HERMES_DIR:-${DEP_BASE_DIR}/.hermes/hermes-agent}"
HERMES_VENV="${HERMES_DIR}/.hermes"
HERMES_BIN="${HERMES_DIR}/.hermes/bin/hermes"
[ -x "$HERMES_BIN" ] || HERMES_BIN="${DEP_BASE_DIR}/.local/bin/hermes"

banner

verify_hermes() {
  if "$HERMES_BIN" --version 2>&1 | head -1 | grep -qi 'hermes'; then
    log "    $($HERMES_BIN --version 2>&1 | head -1)"
    return 0
  fi
  warn "hermes --version 校验失败"
  return 1
}

step "记录升级前版本"
BEFORE="$(git -C "$HERMES_DIR" rev-parse HEAD 2>/dev/null)"
BEFORE_SHORT="${BEFORE:0:8}"
log "    当前 commit: ${BEFORE_SHORT}"

step "拉取 hermes-agent 最新代码"
run git -C "$HERMES_DIR" fetch --prune origin || die "git fetch 失败"
BRANCH="$(git -C "$HERMES_DIR" remote show origin 2>/dev/null | awk '/HEAD branch/{print $NF}' | head -1)"
BRANCH="${BRANCH:-main}"
run git -C "$HERMES_DIR" reset --hard "origin/${BRANCH}" || die "git reset 失败"
AFTER="$(git -C "$HERMES_DIR" rev-parse HEAD 2>/dev/null)"
AFTER_SHORT="${AFTER:0:8}"
log "    ${BEFORE_SHORT} -> ${AFTER_SHORT}"
git -C "$HERMES_DIR" --no-pager log --oneline "${BEFORE}..${AFTER}" 2>/dev/null | head -10 | sed 's/^/      /' | tee -a "$CURRENT_LOG" 2>/dev/null || true

step "用独立 venv 重装 Python 依赖"
if [ -x "${HERMES_VENV}/bin/pip" ]; then
  REQ=""
  for f in requirements.txt pyproject.toml; do
    [ -f "${HERMES_DIR}/${f}" ] && REQ="$f" && break
  done
  if [ -n "$REQ" ]; then
    if [ "$REQ" = "requirements.txt" ]; then
      run "${HERMES_VENV}/bin/pip" install -r "${HERMES_DIR}/requirements.txt" -U -q || warn "依赖重装失败（继续校验）"
    else
      run bash -c "cd '$HERMES_DIR' && '${HERMES_VENV}/bin/pip' install -U -e . -q" || warn "依赖重装失败（继续校验）"
    fi
  fi
else
  warn "未找到 hermes 独立 venv pip: ${HERMES_VENV}/bin/pip（跳过依赖重装）"
fi

step "升级后校验"
if [ "$DRY_RUN" != "1" ] && ! verify_hermes; then
  if [ "$NO_ROLLBACK" != "1" ]; then
    step "回滚到 ${BEFORE_SHORT}"
    run git -C "$HERMES_DIR" reset --hard "$BEFORE" && ok "已回滚" || warn "回滚失败"
  fi
  die "hermes 升级失败"
fi

state_set "git" "$BEFORE_SHORT" "$AFTER_SHORT" "git pull + pip reinstall"
ok "升级完成: hermes @ ${AFTER_SHORT}"
log "🎉 ${COMPONENT} 升级脚本结束（未启动 gateway 服务）"
