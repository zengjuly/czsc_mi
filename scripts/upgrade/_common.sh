#!/usr/bin/env bash
# 外部依赖升级脚本公共函数库（W104）
# 被各 upgrade_*.sh source，不单独执行。
# 约定：调用方先设置 COMPONENT 名称，再 source 本文件。

# ---------- 路径与环境（均可用环境变量覆盖，便于一键安装脚本复用） ----------
DEP_BASE_DIR="${DEP_BASE_DIR:-$HOME}"
STOCK_DIR="${STOCK_DIR:-${DEP_BASE_DIR}/stock}"
VENV="${VENV:-${DEP_BASE_DIR}/venv}"
CZSC_MI_DIR="${CZSC_MI_DIR:-${STOCK_DIR}/czsc_mi}"
LOG_DIR="${LOG_DIR:-${CZSC_MI_DIR}/logs}"
STATE_FILE="${STATE_FILE:-${CZSC_MI_DIR}/data/dep_versions.json}"

COMPONENT="${COMPONENT:-unknown}"
CURRENT_LOG="${CURRENT_LOG:-${LOG_DIR}/upgrade_${COMPONENT}.log}"
DRY_RUN="${DRY_RUN:-0}"          # 1=只打印不执行
NO_ROLLBACK="${NO_ROLLBACK:-0}"  # 1=失败不自动回滚

# ---------- 日志 ----------
_log_line() { printf '[%s] %s\n' "$(date '+%F %T')" "$*"; }
log()  { _log_line "$*" | tee -a "$CURRENT_LOG" 2>/dev/null || _log_line "$*"; }
ok()   { log "  ✅ $*"; }
warn() { log "  ⚠️  $*"; }
die()  { log "  ❌ $*"; exit 1; }
step() { log "▶ $*"; }

mkdir -p "$LOG_DIR" "$(dirname "$STATE_FILE")" 2>/dev/null || true

# ---------- 执行封装（dry-run 感知） ----------
run() {
  if [ "$DRY_RUN" = "1" ]; then
    log "  [DRY-RUN] $*"
    return 0
  fi
  "$@"
}

# ---------- 版本状态记录（简易 JSON append，python 读写避免 jq 依赖） ----------
state_set() {
  # state_set <key> <before> <after> <method>
  [ "$DRY_RUN" = "1" ] && return 0
  "$VENV/bin/python" - "$STATE_FILE" "$COMPONENT" "$1" "$2" "$3" "$4" <<'PY' 2>/dev/null || true
import json, sys, datetime
path, comp, key, before, after, method = sys.argv[1:7]
try:
    data = json.load(open(path, encoding="utf-8"))
except Exception:
    data = {}
data.setdefault(comp, {})[key] = {
    "before": before, "after": after, "method": method,
    "at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
}
json.dump(data, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PY
}

# ---------- git 升级通用流程 ----------
# git_upgrade <repo_dir> <branch|空=自动探测> <remote> <verify_cmd...>
git_upgrade() {
  local repo="$1" branch="$2" remote="${3:-origin}"
  shift 3 || true
  local verify_cmd=("$@")
  [ -d "$repo/.git" ] || die "不是 git 仓库: $repo"

  step "记录升级前版本"
  local before before_short
  before="$(git -C "$repo" rev-parse HEAD)"
  before_short="${before:0:8}"
  log "    当前 commit: ${before_short}"

  step "拉取远端更新 (${remote})"
  run git -C "$repo" fetch --prune "$remote" || die "git fetch 失败"
  # 未指定分支则自动探测远端 HEAD 对应的默认分支
  if [ -z "$branch" ]; then
    branch="$(git -C "$repo" remote show "$remote" 2>/dev/null \
      | awk '/HEAD branch/{print $NF}' | head -1)"
    branch="${branch:-master}"
  fi
  log "    目标分支: ${branch}"
  run git -C "$repo" reset --hard "${remote}/${branch}" || die "git reset ${remote}/${branch} 失败"
  local after after_short
  after="$(git -C "$repo" rev-parse HEAD 2>/dev/null || echo "$before")"
  after_short="${after:0:8}"

  if [ "$before" = "$after" ]; then
    ok "已是最新版本 (${after_short})，无需变更"
    state_set "git" "$before_short" "$after_short" "git pull (no change)"
    return 0
  fi
  log "    ${before_short} -> ${after_short}"
  log "    变更摘要:"
  git -C "$repo" --no-pager log --oneline "${before}..${after}" 2>/dev/null | head -15 | sed 's/^/      /' | tee -a "$CURRENT_LOG" 2>/dev/null || true

  if [ ${#verify_cmd[@]} -gt 0 ]; then
    step "升级后校验"
    if ! "${verify_cmd[@]}"; then
      warn "校验失败"
      _git_rollback "$repo" "$before" "$before_short"
      return 1
    fi
    ok "校验通过"
  fi
  state_set "git" "$before_short" "$after_short" "git pull"
  ok "升级完成: ${COMPONENT} @ ${after_short}"
}

_git_rollback() {
  local repo="$1" before="$2" short="$3"
  if [ "$NO_ROLLBACK" = "1" ]; then
    warn "已跳过自动回滚（NO_ROLLBACK=1），请手动回滚到 ${short}"
    return 0
  fi
  step "自动回滚到 ${short}"
  run git -C "$repo" reset --hard "$before" && ok "已回滚到 ${short}" || warn "回滚失败，请手动处理: git -C $repo reset --hard $short"
}

# ---------- pip 升级通用流程 ----------
# pip_upgrade <pkg> <verify_import_name...>
pip_upgrade() {
  local pkg="$1"
  shift
  local verify_names=("$@")
  local PIP="$VENV/bin/pip" PY="$VENV/bin/python"

  step "记录 ${pkg} 升级前版本"
  local before
  before="$($PY -c "import importlib.metadata as m; print(m.version('$pkg'))" 2>/dev/null || echo '未安装')"
  log "    当前版本: ${before}"

  step "升级 ${pkg}"
  run "$PIP" install --upgrade "$pkg" || die "pip install --upgrade ${pkg} 失败"

  local after
  after="$($PY -c "import importlib.metadata as m; print(m.version('$pkg'))" 2>/dev/null || echo '未知')"
  log "    新版本: ${after}"

  step "升级后校验 (import)"
  local fail=0
  for mod in "${verify_names[@]}"; do
    if [ "$DRY_RUN" != "1" ] && ! "$PY" -c "import $mod" >/dev/null 2>&1; then
      warn "import ${mod} 失败"
      fail=1
    fi
  done
  if [ "$fail" = "1" ]; then
    if [ "$before" != "未安装" ] && [ "$NO_ROLLBACK" != "1" ]; then
      step "自动回滚 ${pkg}==${before}"
      run "$PIP" install "${pkg}==${before}" && ok "已回滚" || warn "回滚失败"
    else
      warn "已跳过回滚，请手动处理"
    fi
    return 1
  fi
  ok "校验通过"
  state_set "pip:$pkg" "$before" "$after" "pip install -U"
  ok "升级完成: ${pkg} ${before} -> ${after}"
}

# ---------- 横幅 ----------
banner() {
  log "============================================================"
  log " 组件升级: ${COMPONENT}"
  log " 时间: $(date '+%F %T %Z')  用户: $(whoami)  主机: $(hostname)"
  log " 模式: $([ "$DRY_RUN" = "1" ] && echo 'DRY-RUN (只打印)' || echo '实际执行')"
  log "============================================================"
}
