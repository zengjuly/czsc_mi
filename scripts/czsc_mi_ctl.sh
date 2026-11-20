#!/usr/bin/env bash
# ============================================================================
# czsc_mi 全栈服务一键启停（W108）
# ----------------------------------------------------------------------------
# 管理本机 czsc_mi 相关的全部服务：
#   1) systemd user 服务：czsc-mi-web / czsc-mi-search / czsc-mi-tdxapi / tdx-tunnel（按存在自动识别）
#   2) tdx-api docker 容器（容器名含 tdx 即管理；无 docker 则跳过）
#   3) czsc_mi 相关 crontab 条目（stop=注释停用，start=取消注释恢复；
#      只动 czsc_mi 脚本行，其它任务如 nginx 备份、第三方 cron 一律不碰）
#
# 用法：
#   ./czsc_mi_ctl.sh status    # 查看全部服务/任务状态（默认）
#   ./czsc_mi_ctl.sh start     # 全部启动（服务 start + cron 取消注释）
#   ./czsc_mi_ctl.sh stop      # 全部停止（服务 stop + cron 注释停用）
#   ./czsc_mi_ctl.sh restart   # 全部重启
#   ./czsc_mi_ctl.sh start --no-cron   # 只管服务，不动 crontab
#
# 说明：systemd user 服务依赖 linger 常驻（install_czsc_mi.sh 步骤 2 已开启）。
# ============================================================================
set -uo pipefail

ACTION="${1:-status}"
[ $# -gt 0 ] && shift 1
NO_CRON=0
for a in "$@"; do [ "$a" = "--no-cron" ] && NO_CRON=1; done
case "$ACTION" in start|stop|restart|status) ;; *) echo "用法: $0 start|stop|restart|status [--no-cron]" >&2; exit 2 ;; esac

SUDO=""
[ "$(id -u)" != "0" ] && [ -n "$(command -v sudo)" ] && SUDO="sudo"

log()  { echo "[$(date '+%F %T')] $*"; }
ok()   { log "  ✅ $*"; }
warn() { log "  ⚠️  $*"; }

# ---------------- systemd user 服务 ----------------
# 注意：不用 grep -q（pipefail 下提前退出会吞掉上游 SIGPIPE，整条管道误判失败），
# 改用不带 -q 的 grep 全量读取；unit 不存在时 systemctl --user status 退出码为 4。
# 注意：tdx-tunnel.service（ssh -L 转发远端 tdx-api）已被本地 czsc-mi-tdxapi 取代，
# 不在默认列表——隧道与本地 8080 冲突；如需临时用隧道请手动 systemctl --user。
USER_SERVICES="czsc-mi-web.service czsc-mi-search.service czsc-mi-tdxapi.service"
svc_exists() {
  systemctl --user status "$1" >/dev/null 2>&1
  [ $? -ne 4 ]
}

do_services() {
  for s in $USER_SERVICES; do
    if ! svc_exists "$s"; then continue; fi
    case "$ACTION" in
      start)   systemctl --user start "$s"  >/dev/null 2>&1 && ok "服务启动 $s" || warn "服务启动失败 $s" ;;
      stop)    systemctl --user stop "$s"   >/dev/null 2>&1 && ok "服务停止 $s" || warn "服务停止失败 $s" ;;
      restart) systemctl --user restart "$s" >/dev/null 2>&1 && ok "服务重启 $s" || warn "服务重启失败 $s" ;;
      status)  st=$(systemctl --user is-active "$s" 2>/dev/null || true); log "  服务 $s: $st" ;;
    esac
  done
}

# ---------------- docker 容器（tdx-api 等） ----------------
do_docker() {
  # 优先免 sudo（用户在 docker 组），不可用再退回 sudo
  local DC=""
  if command -v docker >/dev/null 2>&1 && docker ps >/dev/null 2>&1; then DC="docker"
  elif command -v docker >/dev/null 2>&1; then DC="$SUDO docker"
  else [ "$ACTION" = status ] && log "  docker: 未安装（跳过）"; return 0; fi
  local containers
  containers=$($DC ps -a --format '{{.Names}}' 2>/dev/null | grep -i "tdx" || true)
  [ -z "$containers" ] && { [ "$ACTION" = status ] && log "  docker: 无 tdx 相关容器"; return 0; }
  for c in $containers; do
    case "$ACTION" in
      start)   $DC start "$c" >/dev/null 2>&1 && ok "容器启动 $c" || warn "容器启动失败 $c" ;;
      stop)    $DC stop "$c"  >/dev/null 2>&1 && ok "容器停止 $c" || warn "容器停止失败 $c" ;;
      restart) $DC restart "$c" >/dev/null 2>&1 && ok "容器重启 $c" || warn "容器重启失败 $c" ;;
      status)  log "  容器 $c: $($DC inspect -f '{{.State.Status}}' "$c" 2>/dev/null || echo unknown)" ;;
    esac
  done
}

# ---------------- crontab（czsc_mi 相关条目） ----------------
# 与 mystery/services/cron_manager.py 的 CZSC_MI_MARKERS 口径一致
CRON_MARKERS="czsc_mi minute_macd_watch.sh daily_feishu.sh daily_report.sh daily_pipeline.sh weekly_shares.sh auto-sync"

_line_is_marked() {
  local l="$1"
  for m in $CRON_MARKERS; do
    case "$l" in *"$m"*) return 0 ;; esac
  done
  return 1
}

do_cron() {
  [ "$NO_CRON" = "1" ] && { log "  crontab: --no-cron 跳过"; return 0; }
  local cur
  cur=$(crontab -l 2>/dev/null || true)
  [ -z "$cur" ] && { log "  crontab: 为空（无任务）"; return 0; }
  local out="" touched=0
  while IFS= read -r l; do
    if _line_is_marked "$l"; then
      local probe="${l#\#}"; probe="${probe# }"
      # 计划表达式：@简写 或 5 段 cron 字段（数字/星号/逗号/连字符/斜杠）
      if echo "$probe" | grep -qE "^(@[a-z]+|[[:digit:]*,-/]+( +[[:digit:]*,-/]+){4})( |$)"; then
        case "$ACTION" in
          stop)
            if [ "$l" = "$probe" ]; then out+="# $l"$'\n'; touched=1; else out+="$l"$'\n'; fi ;;
          start|restart)
            if [ "$l" != "$probe" ]; then out+="$probe"$'\n'; touched=1; else out+="$l"$'\n'; fi ;;
          status)
            if [ "$l" = "$probe" ]; then log "  cron 启用中: $probe"; else log "  cron 已停用: $probe"; fi
            touched=1 ;;
        esac
        continue
      fi
    fi
    out+="$l"$'\n'
  done <<< "$cur"
  if { [ "$ACTION" = stop ] || [ "$ACTION" = start ] || [ "$ACTION" = restart ]; } && [ "$touched" = "1" ]; then
    printf '%s' "$out" | crontab - && ok "crontab 已更新（$ACTION）" || warn "crontab 写入失败"
  fi
  [ "$ACTION" = status ] && [ "$touched" = "0" ] && log "  crontab: 无 czsc_mi 任务"
}

# ---------------- 主流程 ----------------
step() { log "=== $* ==="; }
case "$ACTION" in
  start)   step "czsc_mi 全栈启动"; do_services; do_docker; do_cron;   log "完成。" ;;
  stop)    step "czsc_mi 全栈停止"; do_services; do_docker; do_cron;   log "完成。" ;;
  restart) step "czsc_mi 全栈重启"; do_services; do_docker; do_cron;   log "完成。" ;;
  status)  step "czsc_mi 全栈状态"; do_services; do_docker; do_cron ;;
esac
