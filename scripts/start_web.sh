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

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${CZSC_MI_WEB_PY:-/home/ai/ai_runner/venv/bin/python}"
PORT="${CZSC_MI_WEB_PORT:-18501}"
PIDFILE="${CZSC_MI_WEB_PIDFILE:-/tmp/czsc_mi_web.pid}"
LOG="${CZSC_MI_WEB_LOG:-/tmp/czsc_mi_web.log}"
# HTTPS 证书（streamlit 原生 ssl；证书需匹配访问域名，默认用 zz.zzhappyxiaowu.dpdns.org 证书）
SSL_CERT="${CZSC_MI_WEB_SSL_CERT:-/home/ai/ai_runner/stock/ssl_fix/nextcloud.crt}"
SSL_KEY="${CZSC_MI_WEB_SSL_KEY:-/home/ai/ai_runner/stock/ssl_fix/nextcloud.key}"

export MYSTERY_DB_PATH="${MYSTERY_DB_PATH:-/home/ai/ai_runner/stock/data/db/mystery_cache.db}"
export MYSTERY_CHAN_ENABLED="${MYSTERY_CHAN_ENABLED:-1}"

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

stop() {
  if is_running; then
    kill "$(cat "$PIDFILE")" 2>/dev/null || true
    sleep 2
    rm -f "$PIDFILE"
    echo "已停止"
  else
    rm -f "$PIDFILE"
    echo "未在运行"
  fi
}

status() {
  if is_running; then
    echo "运行中（PID $(cat "$PIDFILE")）"
    print_urls
  else
    echo "未运行"
  fi
}

case "${1:-start}" in
  start)   start ;;
  stop)    stop ;;
  restart) stop; sleep 1; start ;;
  status)  status ;;
  *) echo "用法: $0 {start|stop|restart|status}"; exit 1 ;;
esac
