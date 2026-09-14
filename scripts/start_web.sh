#!/usr/bin/env bash
# start_web.sh - czsc_mi Streamlit Web 启停脚本（双栈 IPv4+IPv6，支持重启）
#
# 用法:
#   scripts/start_web.sh start     # 启动（默认端口 18501）
#   scripts/start_web.sh stop
#   scripts/start_web.sh restart   # 重启
#   scripts/start_web.sh status    # 查看状态与访问地址
#
# 环境变量可覆盖:
#   CZSC_MI_WEB_PORT / MYSTERY_DB_PATH / MYSTERY_CHAN_ENABLED
set -euo pipefail

source /home/ai/ai_runner/.bashrc
source /home/ai/ai_runner/.stockrc
source /home/ai/ai_runner/venv/bin/activate

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# 解释器：默认当前 shell 的 python（venv 激活后即 venv），可用 CZSC_MI_WEB_PY 覆盖
PY="${CZSC_MI_WEB_PY:-python}"
PORT="${CZSC_MI_WEB_PORT:-18501}"
PIDFILE="${CZSC_MI_WEB_PIDFILE:-/tmp/czsc_mi_web.pid}"
LOG="${CZSC_MI_WEB_LOG:-/tmp/czsc_mi_web.log}"
# HTTPS 证书（streamlit 原生 ssl；证书需匹配访问域名，默认用 zz.zzhappyxiaowu.dpdns.org 证书）
SSL_CERT="${CZSC_MI_WEB_SSL_CERT:-/home/ai/ai_runner/stock/ssl_fix/nextcloud.crt}"
SSL_KEY="${CZSC_MI_WEB_SSL_KEY:-/home/ai/ai_runner/stock/ssl_fix/nextcloud.key}"

export MYSTERY_DB_PATH="${MYSTERY_DB_PATH:-/home/ai/ai_runner/stock/data/db/mystery_cache.db}"
export MYSTERY_CHAN_ENABLED="${MYSTERY_CHAN_ENABLED:-1}"
export MYSTERY_CHAN_SCORE="${MYSTERY_CHAN_SCORE:-0}"
# THS 数据源（同花顺扶摇）——不导出则 web 进程找不到 SDK，板块钻取/扫描全走本地库
export THS_FUYAO_SCRIPT="${THS_FUYAO_SCRIPT:-/home/ai/ai_runner/stock/Financial-API/python/toolkit/fuyao/scripts/fuyao.py}"
export THS_MARKETDB_DIR="${THS_MARKETDB_DIR:-/home/ai/ai_runner/stock/Financial-API/data}"

print_urls() {
  local scheme="http"
  [ -f "$SSL_CERT" ] && scheme="https"
  echo "端口: ${PORT}（监听 *:${PORT}，${scheme}，IPv4+IPv6 双栈）"
  while IFS= read -r ip; do
    [ -n "$ip" ] && echo "  IPv4: ${scheme}://${ip}:${PORT}"
  done < <(ip -4 addr show scope global 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1)
  while IFS= read -r ip; do
    [ -n "$ip" ] && echo "  IPv6: ${scheme}://[${ip}]:${PORT}"
  done < <(ip -6 addr show scope global 2>/dev/null | awk '/inet6 /{print $2}' | cut -d/ -f1)
  echo "  本机: ${scheme}://localhost:${PORT}"
  [ "$scheme" = "https" ] && echo "  域名: https://zz.zzhappyxiaowu.dpdns.org:${PORT}（证书 CN 匹配）"
}

is_running() {
  [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null
}

start() {
  if is_running; then
    echo "已在运行（PID $(cat "$PIDFILE")）"
    print_urls
    return 0
  fi
  cd "$ROOT"
  local ssl_args=()
  if [ -f "$SSL_CERT" ] && [ -f "$SSL_KEY" ]; then
    ssl_args=(--server.sslCertFile "$SSL_CERT" --server.sslKeyFile "$SSL_KEY")
  fi
  nohup "$PY" -m streamlit run mystery/apps/web/app.py \
    --server.port "$PORT" --server.address :: --server.headless true \
    "${ssl_args[@]}" \
    >"$LOG" 2>&1 &
  echo $! > "$PIDFILE"
  sleep 4
  if ! is_running; then
    echo "❌ 启动失败，日志见 $LOG"
    tail -5 "$LOG"
    rm -f "$PIDFILE"
    return 1
  fi
  echo "✅ 已启动（PID $(cat "$PIDFILE")）"
  print_urls
}

# 收集要停止的进程：PIDFILE 记录 + 命令行匹配本端口的全部 streamlit 实例
# （pgrep 兜底防 PIDFILE 失效/多实例残留——如 2026-09-13 PIDFILE 指向死 PID
#  而真实进程还占着端口导致 restart 假成功）
_collect_web_pids() {
  local pids=() fpid
  if [ -f "$PIDFILE" ]; then
    fpid="$(cat "$PIDFILE" 2>/dev/null || true)"
    [ -n "$fpid" ] && pids+=("$fpid")
  fi
  local p
  while IFS= read -r p; do
    [ -n "$p" ] && pids+=("$p")
  done < <(pgrep -f "streamlit run mystery/apps/web/app.py.*--server.port ${PORT}" 2>/dev/null || true)
  # 去重（保序）
  local uniq=() seen=" "
  for p in "${pids[@]}"; do
    case "$seen" in
      *" $p "*) ;;
      *) uniq+=("$p"); seen="$seen$p " ;;
    esac
  done
  printf '%s\n' "${uniq[@]}"
}

_any_alive() {
  # 入参：进程 PID 列表（每行一个）；有任一存活返回 0
  local p
  while IFS= read -r p; do
    [ -n "$p" ] && kill -0 "$p" 2>/dev/null && return 0
  done
  return 1
}

stop() {
  local pids=()
  while IFS= read -r p; do pids+=("$p"); done < <(_collect_web_pids)
  if [ "${#pids[@]}" -eq 0 ]; then
    rm -f "$PIDFILE"
    echo "未在运行"
    return 0
  fi
  echo "停止中（PID: ${pids[*]}）..."
  # 1) 优雅 TERM，最多等 STOP_TIMEOUT 秒（默认 8）
  local p waited=0
  for p in "${pids[@]}"; do kill "$p" 2>/dev/null || true; done
  while [ "$waited" -lt "${STOP_TIMEOUT:-8}" ]; do
    printf '%s\n' "${pids[@]}" | _any_alive || break
    sleep 1
    waited=$((waited + 1))
  done
  # 2) 仍存活 → 强制 KILL -9（D 状态等 IO 恢复后即可杀）
  if printf '%s\n' "${pids[@]}" | _any_alive; then
    echo "进程未响应 TERM（可能 D 状态/IO 挂起），发送 KILL -9"
    for p in "${pids[@]}"; do kill -9 "$p" 2>/dev/null || true; done
    waited=0
    while [ "$waited" -lt 5 ]; do
      printf '%s\n' "${pids[@]}" | _any_alive || break
      sleep 1
      waited=$((waited + 1))
    done
  fi
  # 3) 最终确认：还活着就报错并保留 PIDFILE（便于重试），不假成功
  local still=()
  for p in "${pids[@]}"; do
    kill -0 "$p" 2>/dev/null && still+=("$p")
  done
  if [ "${#still[@]}" -gt 0 ]; then
    echo "⚠️ 进程仍存活（${still[*]}，D 状态未恢复），端口 ${PORT} 可能仍被占用。" >&2
    echo "   建议稍后重试 stop，或排查系统 IO（dmesg / 挂载 / 磁盘）。" >&2
    return 1
  fi
  rm -f "$PIDFILE"
  echo "已停止（PID: ${pids[*]}）"
}

status() {
  if is_running; then
    echo "运行中（PID $(cat "$PIDFILE")）"
    print_urls
    return 0
  fi
  # PIDFILE 失效但端口实际有进程 → 修复 PIDFILE（如 2026-09-13 事故场景）
  local extra
  extra="$(pgrep -f "streamlit run mystery/apps/web/app.py.*--server.port ${PORT}" | head -1 || true)"
  if [ -n "$extra" ]; then
    echo "⚠️ PIDFILE 失效：实际有 streamlit 进程（PID $extra）在运行，已修复 PIDFILE"
    echo "$extra" > "$PIDFILE"
    print_urls
    return 0
  fi
  echo "未运行"
}

# systemd 用户服务接管检测：czsc-mi-web.service 启用后，启停操作提示走
# systemctl（systemd 提供崩溃自动拉起 + 开机自启；本脚本旧逻辑会与其冲突——
# 直接杀进程会被 systemd 的 Restart=always 立即拉起）。
# 需要强制用脚本旧逻辑时设 SYSTEMD_FORCE_LEGACY=1。
_systemd_managed() {
  systemctl --user list-unit-files czsc-mi-web.service 2>/dev/null | grep -q enabled
}

case "${1:-start}" in
  start|stop|restart)
    if _systemd_managed && [ "${SYSTEMD_FORCE_LEGACY:-0}" != "1" ]; then
      echo "⚠️ Web 已由 systemd 用户服务管理（czsc-mi-web.service：崩溃自动拉起 + 开机自启）。"
      echo "   请用: systemctl --user ${1} czsc-mi-web"
      echo "   强制使用本脚本旧逻辑: SYSTEMD_FORCE_LEGACY=1 $0 ${1}"
      exit 0
    fi
    ;;
esac

case "${1:-start}" in
  start)   start ;;
  stop)    stop ;;
  restart) stop; sleep 1; start ;;
  status)  status ;;
  *) echo "用法: $0 {start|stop|restart|status}"; exit 1 ;;
esac
