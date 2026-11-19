#!/usr/bin/env bash
# 一键升级 tdx-api（通达信 REST 数据源，git + docker 容器）— W104
# 升级流程：git pull → docker compose 重建镜像并重启 → healthz 校验 → 失败回滚到旧 commit + 旧镜像
# 用法: ./upgrade_tdx.sh [--dry-run] [--no-rollback]
set -uo pipefail

COMPONENT="tdx"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/_common.sh"

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --no-rollback) NO_ROLLBACK=1 ;;
    *) echo "未知参数: $arg"; exit 2 ;;
  esac
done

TDX_DIR="${TDX_DIR:-${STOCK_DIR}/tdx-api}"
CONTAINER="tdx-stock-web"
HEALTH_URL="http://127.0.0.1:8080/api/kline-history?code=sh600519&type=day&limit=1"

banner

[ -d "$TDX_DIR/.git" ] || die "tdx-api 仓库不存在: $TDX_DIR"
command -v docker >/dev/null 2>&1 || die "docker 命令不可用"

step "记录升级前 commit 与镜像 ID"
BEFORE="$(git -C "$TDX_DIR" rev-parse HEAD)"
BEFORE_SHORT="${BEFORE:0:8}"
OLD_IMAGE="$(docker inspect --format='{{.Image}}' "$CONTAINER" 2>/dev/null || echo '')"
log "    commit: ${BEFORE_SHORT}  镜像: ${OLD_IMAGE:0:12}"

step "拉取 tdx-api 最新代码"
run git -C "$TDX_DIR" fetch --prune origin || die "git fetch 失败"
run git -C "$TDX_DIR" reset --hard origin/HEAD || die "git reset origin/HEAD 失败"
# origin/HEAD 可能未设置，兜底用 origin/master 或 origin/main
if git -C "$TDX_DIR" rev-parse --verify origin/HEAD >/dev/null 2>&1; then
  run git -C "$TDX_DIR" reset --hard origin/HEAD || true
else
  BRANCH="$(git -C "$TDX_DIR" remote show origin 2>/dev/null | awk '/HEAD branch/{print $NF}')"
  BRANCH="${BRANCH:-master}"
  run git -C "$TDX_DIR" reset --hard "origin/${BRANCH}" || die "git reset origin/${BRANCH} 失败"
fi

AFTER="$(git -C "$TDX_DIR" rev-parse HEAD)"
AFTER_SHORT="${AFTER:0:8}"
if [ "$BEFORE" = "$AFTER" ]; then
  ok "代码已是最新 (${AFTER_SHORT})，仍将重建镜像以确保一致"
fi
log "    ${BEFORE_SHORT} -> ${AFTER_SHORT}"
git -C "$TDX_DIR" --no-pager log --oneline "${BEFORE}..${AFTER}" 2>/dev/null | head -15 | sed 's/^/      /' | tee -a "$CURRENT_LOG" 2>/dev/null || true

tdx_rollback() {
  if [ "$NO_ROLLBACK" = "1" ]; then warn "已跳过自动回滚"; return 0; fi
  step "回滚 tdx-api 到 ${BEFORE_SHORT}"
  run git -C "$TDX_DIR" reset --hard "$BEFORE" || warn "git 回滚失败"
  if [ -n "$OLD_IMAGE" ]; then
    run docker stop "$CONTAINER" >/dev/null 2>&1 || true
    run docker rm "$CONTAINER" >/dev/null 2>&1 || true
    # 用旧镜像 ID 重新起容器（保留原端口映射/网络）
    NEW_IMG="$(docker images --format '{{.ID}}' --filter "id=${OLD_IMAGE}" 2>/dev/null | head -1)"
    if [ -n "$NEW_IMG" ]; then
      run docker run -d --name "$CONTAINER" --restart unless-stopped \
        -p 8080:8080 -e TZ=Asia/Shanghai --network stock-network \
        "$OLD_IMAGE" >/dev/null && ok "已用旧镜像重启容器" || warn "镜像回滚失败，需手动 docker run"
    fi
  fi
}

step "重建 docker 镜像并重启容器"
if ! run bash -c "cd '$TDX_DIR' && docker compose down && docker compose up -d --build"; then
  warn "compose 重建失败"
  tdx_rollback
  die "tdx-api 升级失败"
fi

step "等待容器 healthy"
if [ "$DRY_RUN" != "1" ]; then
  OK=0
  for i in $(seq 1 30); do
    STATUS="$(docker inspect --format='{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null || echo 'none')"
    if [ "$STATUS" = "healthy" ] || [ "$STATUS" = "none" ]; then OK=1; break; fi
    sleep 2
  done
  if [ "$OK" != "1" ]; then
    warn "容器未能 healthy (状态: $STATUS)"
    tdx_rollback
    die "tdx-api 健康检查失败"
  fi
fi

step "校验 REST 接口"
if [ "$DRY_RUN" != "1" ]; then
  CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$HEALTH_URL" 2>/dev/null || echo 000)"
  if [ "$CODE" = "200" ]; then
    ok "REST 接口返回 200"
  else
    warn "REST 接口返回 ${CODE}"
    tdx_rollback
    die "tdx-api 接口校验失败"
  fi
fi

state_set "git" "$BEFORE_SHORT" "$AFTER_SHORT" "git pull + docker rebuild"
ok "升级完成: tdx-api @ ${AFTER_SHORT}"
log "🎉 ${COMPONENT} 升级脚本结束"
