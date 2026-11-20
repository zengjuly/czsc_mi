#!/usr/bin/env bash
# ============================================================================
# czsc_mi 新系统一键安装脚本（W104 / W107）
# ----------------------------------------------------------------------------
# 在干净的 Ubuntu/Debian 系统上安装 czsc_mi 及全部依赖：
#   系统包 → 运行用户+linger → Python venv → 主仓库 → pip 库 → 外部组件
#   → 环境配置 → 数据库初始化 → 定时任务 → Web 服务(systemd user)
#   → 公网访问+鉴权(nginx Basic Auth) → 端到端校验
#
# 特点：幂等（可重复运行）、分步可跳过、dry-run 预演、失败不中断其它步骤。
#
# 用法：
#   sudo ./install_czsc_mi.sh --dry-run           # 预演，不改动系统
#   sudo ./install_czsc_mi.sh                     # 完整安装
#   sudo ./install_czsc_mi.sh --skip docker,cron  # 跳过部分步骤
#   sudo ./install_czsc_mi.sh --config install.env  # 从文件读取配置
#
# 公网访问（WEB_PUBLIC=1 时自动完成）：
#   - systemd user 服务常驻（enable-linger，不随 SSH 断开而停止）
#   - Streamlit 仅监听 127.0.0.1，nginx 反代 + Basic Auth 暴露公网端口
#   - iptables 放行公网端口并持久化（云平台安全组仍需自行放行）
#
# 配置：所有参数可用环境变量或 --config 文件覆盖，见同目录 install.env.example。
# 敏感凭据（行情 API key / Web 密码）不硬编码进本脚本：
#   API key 写入 ${BASE_DIR}/.stockrc（600）；Web 密码存 ${STOCK_DIR}/.web_password（600）。
# ============================================================================
set -uo pipefail

# ---------------- 默认参数（生产对齐，可全部覆盖） ----------------
BASE_DIR="${BASE_DIR:-$HOME}"
RUN_USER="${RUN_USER:-ai_runner}"
STOCK_DIR="${STOCK_DIR:-${BASE_DIR}/stock}"
CZSC_MI_DIR="${CZSC_MI_DIR:-${STOCK_DIR}/czsc_mi}"
VENV="${VENV:-${BASE_DIR}/venv}"
PY_VER="${PY_VER:-3.12}"

# 主仓库与外部组件
CZSC_MI_REPO="${CZSC_MI_REPO:-ssh://git@ssh.github.com:443/zengjuly/czsc_mi.git}"
CZSC_MI_BRANCH="${CZSC_MI_BRANCH:-main}"
THS_REPO="${THS_REPO:-https://gitee.com/HiThink-Tech/Financial-API.git}"
TDX_REPO="${TDX_REPO:-https://gh-proxy.org/https://github.com/oficcejo/tdx-api.git}"
HERMES_REPO="${HERMES_REPO:-https://gh-proxy.org/https://github.com/NousResearch/hermes-agent.git/}"
SKIP_CLONE="${SKIP_CLONE:-0}"   # 1=跳过 clone（源码已就位时）

# 数据目录
DATA_DIR="${DATA_DIR:-${STOCK_DIR}/data}"
DB_PATH="${DB_PATH:-${DATA_DIR}/db/mystery_cache.db}"
LOG_DIR_CZ="${LOG_DIR_CZ:-${CZSC_MI_DIR}/logs}"

# 行情数据（可选，缺省留空由用户填）
TDX_VIPDOC_DIR="${TDX_VIPDOC_DIR:-${STOCK_DIR}/data/tdx_vipdoc}"
HITHINK_API_KEY="${HITHINK_API_KEY:-}"

# Web
WEB_PORT="${WEB_PORT:-18501}"   # 保留参数（start_web.sh 直跑场景用）

# Web 公网访问与鉴权（W107）
WEB_PUBLIC="${WEB_PUBLIC:-0}"                   # 1=公网开放：nginx Basic Auth 反代 + 防火墙放行
WEB_PUBLIC_PORT="${WEB_PUBLIC_PORT:-18601}"     # 公网入口端口（nginx 监听，0.0.0.0）
WEB_INTERNAL_PORT="${WEB_INTERNAL_PORT:-18602}" # Streamlit 内部端口（仅 127.0.0.1）
WEB_AUTH_USER="${WEB_AUTH_USER:-czscmi}"        # Basic Auth 用户名
WEB_AUTH_PASSWORD="${WEB_AUTH_PASSWORD:-}"      # Basic Auth 密码；留空则安装时自动生成
WEB_MEMORY_MAX="${WEB_MEMORY_MAX:-}"            # systemd MemoryMax，如 700M（小内存机器建议设置）

# 运行控制
DRY_RUN=0
SKIP_STEPS=""
CONFIG_FILE=""

# ---------------- 参数解析 ----------------
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --config) CONFIG_FILE="${2:-}"; shift 2 ;;
    --skip) SKIP_STEPS="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "未知参数: $1"; exit 2 ;;
  esac
done

# 配置文件（KEY=VALUE，忽略注释）
if [ -n "$CONFIG_FILE" ] && [ -f "$CONFIG_FILE" ]; then
  echo "[$(date '+%F %T')] 加载配置: $CONFIG_FILE"
  while IFS='=' read -r k v; do
    k="$(echo "$k" | xargs)"
    case "$k" in ''|\#*) continue ;; esac
    v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
    export "$k=$v"
  done < <(grep -vE '^\s*(#|$)' "$CONFIG_FILE")
  # 重新同步由配置覆盖的变量
  BASE_DIR="${BASE_DIR:-$HOME}"; STOCK_DIR="${STOCK_DIR:-${BASE_DIR}/stock}"
  CZSC_MI_DIR="${CZSC_MI_DIR:-${STOCK_DIR}/czsc_mi}"; VENV="${VENV:-${BASE_DIR}/venv}"
  DATA_DIR="${DATA_DIR:-${STOCK_DIR}/data}"; DB_PATH="${DB_PATH:-${DATA_DIR}/db/mystery_cache.db}"
  LOG_DIR_CZ="${LOG_DIR_CZ:-${CZSC_MI_DIR}/logs}"
fi

# ---------------- 公共函数 ----------------
log()  { echo "[$(date '+%F %T')] $*"; }
step() { echo ""; log "============================================================"; log "▶ $*"; log "============================================================"; }
ok()   { log "  ✅ $*"; }
warn() { log "  ⚠️  $*"; }
die()  { log "  ❌ $*"; exit 1; }

run() {
  if [ "$DRY_RUN" = "1" ]; then echo "    [DRY-RUN] $*"; return 0; fi
  "$@"
}
should_skip() {
  local s
  for s in ${SKIP_STEPS//,/ }; do [ "$s" = "$1" ] && return 0; done
  return 1
}

# 校验是否 root（装系统包/建用户需要）；若非 root 但只跑用户级步骤则允许
IS_ROOT=0
[ "$(id -u)" = "0" ] && IS_ROOT=1
SUDO=""
[ "$IS_ROOT" = "0" ] && SUDO="sudo"

step "0/12 安装前信息确认"
log "  基础目录:   ${BASE_DIR}"
log "  运行用户:   ${RUN_USER}"
log "  主项目:     ${CZSC_MI_DIR}"
log "  venv:       ${VENV}"
log "  数据库:     ${DB_PATH}"
log "  Web 端口:   内部 ${WEB_INTERNAL_PORT}（127.0.0.1）/ 公网 $([ "$WEB_PUBLIC" = "1" ] && echo "${WEB_PUBLIC_PORT}（nginx+鉴权）" || echo "未开放")"
log "  Web 鉴权:   $([ "$WEB_PUBLIC" = "1" ] && echo "用户 ${WEB_AUTH_USER}（密码$([ -n "$WEB_AUTH_PASSWORD" ] && echo 已提供 || echo 将自动生成)）" || echo "不适用")"
log "  模式:       $([ "$DRY_RUN" = "1" ] && echo 'DRY-RUN 预演' || echo '实际安装')"
log "  跳过步骤:   ${SKIP_STEPS:-无}"

# ---------------- 1. 系统包 ----------------
step "1/12 安装系统依赖包"
if should_skip syspkg; then warn "跳过"; else
  if [ "$IS_ROOT" = "1" ] || [ -n "$SUDO" ]; then
    run $SUDO apt-get update -qq || warn "apt update 失败（继续）"
    PKGS="python3 python3-venv python3-pip git curl wget ca-certificates build-essential libffi-dev"
    # 公网开放时需要 nginx（鉴权反代）与 apache2-utils（htpasswd 生成）
    [ "$WEB_PUBLIC" = "1" ] && PKGS="$PKGS nginx apache2-utils"
    run $SUDO apt-get install -y -qq $PKGS 2>&1 | tail -5 || warn "部分系统包安装失败"
  else
    warn "非 root 且无 sudo，跳过系统包安装（请确保 python3-venv/git/curl 已装）"
  fi
  # 可选：docker（tdx-api 需要）
  if should_skip docker; then warn "跳过 docker"; else
    if command -v docker >/dev/null 2>&1; then ok "docker 已安装"
    else warn "docker 未安装：tdx-api 需要 docker。请按 https://docs.docker.com/engine/install/ubuntu/ 安装后再装 tdx 组件"; fi
  fi
fi

# ---------------- 2. 运行用户与目录 ----------------
step "2/12 创建运行用户与目录结构（含 linger 常驻）"
if should_skip userdir; then warn "跳过"; else
  if [ "$IS_ROOT" = "1" ]; then
    if ! id "$RUN_USER" >/dev/null 2>&1; then
      run useradd -m -s /bin/bash "$RUN_USER" && ok "创建用户 $RUN_USER"
    else ok "用户 $RUN_USER 已存在"; fi
  fi
  run mkdir -p "$STOCK_DIR" "$DATA_DIR/db" "$DATA_DIR/logs" "${CZSC_MI_DIR}/logs" "$BASE_DIR"
  [ "$IS_ROOT" = "1" ] && run chown -R "${RUN_USER}:${RUN_USER}" "$BASE_DIR"
  # linger（关键！）：systemd user 服务不随 SSH 会话断开而停止。
  # 未开启时用户管理器随最后一个会话退出被整体回收，Web 等服务会"莫名其妙"消失。
  if [ "$IS_ROOT" = "1" ] || [ -n "$SUDO" ]; then
    run $SUDO loginctl enable-linger "$RUN_USER" && ok "已启用 linger（服务常驻，不随 SSH 断开而停止）"
  else
    warn "无 sudo，未能开启 linger；请手动执行: sudo loginctl enable-linger $RUN_USER"
  fi
  ok "目录就绪"
fi

# ---------------- 3. Python venv ----------------
step "3/12 创建 Python 虚拟环境"
if should_skip venv; then warn "跳过"; else
  if [ ! -x "${VENV}/bin/python" ]; then
    run python3 -m venv "$VENV" || die "venv 创建失败（确认 python3-venv 已装）"
    ok "venv 已创建: $VENV"
  else ok "venv 已存在"; fi
  run "${VENV}/bin/pip" install --upgrade pip setuptools wheel -q || warn "pip 升级失败（继续）"
  if [ "$DRY_RUN" != "1" ] && [ -x "${VENV}/bin/python" ]; then
    log "  Python: $(${VENV}/bin/python --version 2>&1)"
  fi
fi

# ---------------- 4. 主仓库 ----------------
step "4/12 获取 czsc_mi 主仓库"
if should_skip repo; then warn "跳过"; else
  if [ "$SKIP_CLONE" = "1" ]; then
    warn "SKIP_CLONE=1，假定源码已就位: ${CZSC_MI_DIR}"
  elif [ -d "${CZSC_MI_DIR}/.git" ]; then
    ok "仓库已存在: ${CZSC_MI_DIR}（如需更新用 scripts/upgrade）"
  else
    run git clone --depth 1 -b "$CZSC_MI_BRANCH" "$CZSC_MI_REPO" "$CZSC_MI_DIR" \
      || die "clone czsc_mi 失败（检查仓库地址与 SSH key）"
  fi
  [ -f "${CZSC_MI_DIR}/pyproject.toml" ] || die "未找到 pyproject.toml: ${CZSC_MI_DIR}"
fi

# ---------------- 5. pip 依赖 ----------------
step "5/12 安装 Python 依赖（基础 + chan + web + pinyin + 数据源）"
if should_skip pip; then warn "跳过"; else
  PIP="${VENV}/bin/pip"
  run "$PIP" install -e "$CZSC_MI_DIR" -q || die "安装 czsc_mi 本体失败"
  # 核心 + 可选 extra + 运行期数据源库（生产实测清单）
  run "$PIP" install -q \
    "czsc>=1.0.2,<2.0" streamlit plotly pypinyin \
    pandas PyYAML openpyxl numpy \
    mootdx baostock duckdb requests \
    || warn "部分依赖安装失败，请检查网络/镜像源"
  ok "pip 依赖安装完成"
  # 版本快照
  "$VENV/bin/python" - <<'PY' 2>/dev/null || true
import importlib.metadata as m
for p in ["pandas","czsc","streamlit","pypinyin","mootdx","baostock","duckdb"]:
    try: print(f"  {p}={m.version(p)}")
    except Exception: print(f"  {p}=NO")
PY
fi

# ---------------- 6. 外部组件 clone ----------------
step "6/12 获取外部组件（Financial-API / tdx-api / hermes）"
if should_skip components; then warn "跳过"; else
  # ths: Financial-API
  if [ -d "${STOCK_DIR}/Financial-API/.git" ]; then ok "Financial-API 已存在"
  else run git clone --depth 1 "$THS_REPO" "${STOCK_DIR}/Financial-API" || warn "Financial-API clone 失败"; fi
  # tdx: tdx-api
  if [ -d "${STOCK_DIR}/tdx-api/.git" ]; then ok "tdx-api 已存在"
  else run git clone --depth 1 "$TDX_REPO" "${STOCK_DIR}/tdx-api" || warn "tdx-api clone 失败"; fi
  # hermes: 独立 git + 独立 venv
  if [ -d "${BASE_DIR}/.hermes/hermes-agent/.git" ]; then ok "hermes 已存在"
  else
    run git clone --depth 1 "$HERMES_REPO" "${BASE_DIR}/.hermes/hermes-agent" || warn "hermes clone 失败"
  fi
  # 启动 tdx 容器
  if should_skip docker; then warn "跳过 tdx 容器启动"; 
  elif [ -f "${STOCK_DIR}/tdx-api/docker-compose.yml" ] && command -v docker >/dev/null 2>&1; then
    run bash -c "cd '${STOCK_DIR}/tdx-api' && docker compose up -d --build" || warn "tdx 容器启动失败"
    ok "tdx-api 容器已启动（8080）"
  fi
fi

# ---------------- 7. 环境配置 ----------------
step "7/12 写入环境配置 .stockrc 与 systemd env"
if should_skip env; then warn "跳过"; else
  RC_FILE="${BASE_DIR}/.stockrc"
  if [ "$DRY_RUN" != "1" ]; then
    cat > "$RC_FILE" <<EOF
# czsc_mi 运行环境变量（一键安装生成，请按实际环境调整）
export TDX_VIPDOC_DIR="${TDX_VIPDOC_DIR}"
export MYSTERY_DB_PATH="${DB_PATH}"
export SOURCE_REPORT_DIR="${DATA_DIR}/logs/"
export MYSTERY_REPORT_DIR="${STOCK_DIR}/output/"
export THS_FUYAO_SCRIPT="${STOCK_DIR}/Financial-API/python/toolkit/fuyao/scripts/fuyao.py"
export THS_MARKETDB_DIR="${STOCK_DIR}/Financial-API/data"
export MARKETDB_DB_PATH="${STOCK_DIR}/Financial-API/data/market.duckdb"
export MARKETDB_DATA_ROOT="${STOCK_DIR}/Financial-API/data"
export TDX_API_URL="http://localhost:8080/api"
export HITHINK_FINANCE_API_KEY="${HITHINK_API_KEY}"
export MYSTERY_CHAN_ENABLED=1
export MYSTERY_CHAN_SCORE=0
EOF
    chmod 600 "$RC_FILE"
    [ "$IS_ROOT" = "1" ] && chown "${RUN_USER}:${RUN_USER}" "$RC_FILE"
    ok "写入 ${RC_FILE}（600 权限）"
    [ -z "$HITHINK_API_KEY" ] && warn "HITHINK_FINANCE_API_KEY 为空，请编辑 .stockrc 填入，否则同花顺数据源不可用"
  else
    echo "    [DRY-RUN] 写入 ${RC_FILE}"
  fi
fi

# ---------------- 8. 数据库初始化 ----------------
step "8/12 初始化数据库（首次连接自动建 schema + migrations）"
if should_skip db; then warn "跳过"; else
  run mkdir -p "$(dirname "$DB_PATH")"
  if [ "$DRY_RUN" != "1" ]; then
    ( cd "$CZSC_MI_DIR" && set -a && source "$BASE_DIR/.stockrc" && set +a && \
      "$VENV/bin/python" -c "
from mystery.config import load_config
from mystery.services.analyze import AnalysisService
s = AnalysisService(load_config())
print('  数据库初始化完成，股票数:', len(s.market.db.get_stock_list()))
" ) || warn "数据库初始化校验失败（空库首次会自动建表，可忽略后手动复测）"
  fi
fi

# ---------------- 9. 定时任务 ----------------
step "9/12 安装定时任务（分钟 MACD 盘中监控）"
if should_skip cron; then warn "跳过"; else
  CRON_LINE="1,31 9-15 * * 1-5 /bin/bash ${CZSC_MI_DIR}/scripts/minute_macd_watch.sh >> ${CZSC_MI_DIR}/logs/minute_macd_cron.log 2>&1"
  if [ "$DRY_RUN" = "1" ]; then
    echo "    [DRY-RUN] 追加 crontab: $CRON_LINE"
  else
    ( crontab -l 2>/dev/null | grep -v "minute_macd_watch.sh" ; echo "$CRON_LINE" ) | crontab -
    ok "分钟 MACD cron 已安装"
  fi
fi

# ---------------- 10. Web 服务 ----------------
step "10/12 安装 Web 服务（systemd user service，仅监听 127.0.0.1）"
if should_skip web; then warn "跳过"; else
  if [ "$(id -un)" != "$RUN_USER" ] && [ "$IS_ROOT" = "1" ]; then
    warn "当前为 root 而 RUN_USER=$RUN_USER：systemd user 服务需以运行用户安装"
    warn "请以运行用户重跑其余步骤，或手动安装 ~/.config/systemd/user/czsc-mi-web.service"
  else
    SERVICE_DIR="${HOME}/.config/systemd/user"
    run mkdir -p "$SERVICE_DIR" "${CZSC_MI_DIR}/logs"
    if [ "$DRY_RUN" != "1" ]; then
      {
        cat <<EOF
[Unit]
Description=czsc_mi Streamlit Web (czsc_mi)
After=network.target

[Service]
Type=simple
WorkingDirectory=${CZSC_MI_DIR}
ExecStart=/bin/bash -c "set -a; source ${BASE_DIR}/.stockrc; set +a; exec ${VENV}/bin/python -m streamlit run mystery/apps/web/app.py --server.port ${WEB_INTERNAL_PORT} --server.address 127.0.0.1 --server.headless true"
Restart=always
RestartSec=5
EOF
        # 小内存机器限内存，防止一次重查询拖垮整机（超限自动重启）
        [ -n "$WEB_MEMORY_MAX" ] && echo "MemoryMax=${WEB_MEMORY_MAX}"
        cat <<EOF

[Install]
WantedBy=default.target
EOF
      } > "${SERVICE_DIR}/czsc-mi-web.service"
      chmod 600 "${SERVICE_DIR}/czsc-mi-web.service"
      systemctl --user daemon-reload
      systemctl --user enable --now czsc-mi-web.service 2>/dev/null || systemctl --user restart czsc-mi-web.service
      ok "czsc-mi-web.service 已安装并启动（127.0.0.1:${WEB_INTERNAL_PORT}）"
    else
      echo "    [DRY-RUN] 写入 ${SERVICE_DIR}/czsc-mi-web.service 并 enable --now"
    fi
  fi
fi

# ---------------- 11. 公网访问与鉴权 ----------------
step "11/12 公网访问与鉴权（nginx Basic Auth 反代 + 防火墙放行）"
if [ "$WEB_PUBLIC" != "1" ]; then
  log "  WEB_PUBLIC=0，Web 仅本机可访问（http://127.0.0.1:${WEB_INTERNAL_PORT}），跳过"
elif should_skip publicweb; then warn "跳过"; else
  PW_AUTOGEN=0
  if [ -z "$WEB_AUTH_PASSWORD" ]; then
    WEB_AUTH_PASSWORD="$(openssl rand -base64 15 | tr -d '/+=')"
    PW_AUTOGEN=1
  fi
  run $SUDO apt-get install -y -qq nginx apache2-utils 2>&1 | tail -2 || warn "nginx/apache2-utils 安装失败"
  if [ "$DRY_RUN" != "1" ]; then
    # 1) 凭据落盘（600）
    echo "$WEB_AUTH_PASSWORD" > "${STOCK_DIR}/.web_password" && chmod 600 "${STOCK_DIR}/.web_password"
    $SUDO htpasswd -bc /etc/nginx/czsc_mi.htpasswd "$WEB_AUTH_USER" "$WEB_AUTH_PASSWORD" \
      && $SUDO sh -c 'chmod 640 /etc/nginx/czsc_mi.htpasswd && chgrp www-data /etc/nginx/czsc_mi.htpasswd' \
      || warn "htpasswd 生成失败"
    # 2) nginx 站点：公网端口 + Basic Auth → 127.0.0.1 内部端口（含 WebSocket 支持）
    cat > /tmp/czsc-mi.nginx.conf <<EOF
server {
    listen ${WEB_PUBLIC_PORT};
    server_name _;

    auth_basic "czsc_mi restricted";
    auth_basic_user_file /etc/nginx/czsc_mi.htpasswd;

    location / {
        proxy_pass http://127.0.0.1:${WEB_INTERNAL_PORT};
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 86400;
        proxy_buffering off;
        client_max_body_size 64m;
    }
}
EOF
    $SUDO cp /tmp/czsc-mi.nginx.conf /etc/nginx/sites-available/czsc-mi \
      && $SUDO ln -sf /etc/nginx/sites-available/czsc-mi /etc/nginx/sites-enabled/czsc-mi \
      && if $SUDO nginx -t; then
        $SUDO systemctl enable --now nginx && $SUDO systemctl reload nginx \
          && ok "nginx 鉴权反代已就绪（0.0.0.0:${WEB_PUBLIC_PORT} → 127.0.0.1:${WEB_INTERNAL_PORT}）"
      else warn "nginx 配置校验失败，请检查 /etc/nginx/sites-available/czsc-mi"; fi
    # 3) 防火墙放行并持久化（仅系统级 iptables；ufw/云安全组请自行放行）
    if command -v iptables >/dev/null 2>&1; then
      $SUDO iptables -C INPUT -p tcp --dport "$WEB_PUBLIC_PORT" -j ACCEPT 2>/dev/null \
        || $SUDO iptables -I INPUT -p tcp --dport "$WEB_PUBLIC_PORT" -j ACCEPT
      [ -d /etc/iptables ] && $SUDO sh -c "iptables-save > /etc/iptables/rules.v4"
      ok "iptables 已放行 ${WEB_PUBLIC_PORT}（已持久化）"
    fi
    warn "云平台安全组（如 OCI/AWS）需另行放行 ${WEB_PUBLIC_PORT}/tcp"
    # 4) 汇报入口与凭据
    log "  公网入口: http://<服务器IP或域名>:${WEB_PUBLIC_PORT}"
    log "  鉴权账号: ${WEB_AUTH_USER}"
    if [ "$PW_AUTOGEN" = "1" ]; then
      log "  自动生成密码: ${WEB_AUTH_PASSWORD}"
      log "  （已保存 ${STOCK_DIR}/.web_password，600 权限；改密码: sudo htpasswd -b /etc/nginx/czsc_mi.htpasswd ${WEB_AUTH_USER} 新密码）"
    else
      log "  密码由 WEB_AUTH_PASSWORD 提供，备份存于 ${STOCK_DIR}/.web_password"
    fi
  else
    echo "    [DRY-RUN] 配置 nginx(${WEB_PUBLIC_PORT}→127.0.0.1:${WEB_INTERNAL_PORT}) + htpasswd(${WEB_AUTH_USER}) + iptables 放行"
  fi
fi

# ---------------- 12. 校验 ----------------
step "12/12 端到端校验"
if should_skip verify; then warn "跳过"; else
  if [ "$DRY_RUN" != "1" ]; then
    ( cd "$CZSC_MI_DIR" && set -a && source "$BASE_DIR/.stockrc" && set +a && \
      "$VENV/bin/czsc-mi" --help >/dev/null 2>&1 ) && ok "czsc-mi CLI 可用" || warn "czsc-mi CLI 校验失败"
    "$VENV/bin/python" -c "import czsc, streamlit, pypinyin; print('  核心库 import OK')" 2>/dev/null && ok "核心库可用" || warn "核心库校验失败"
    log "  数据库: ${DB_PATH} ($(du -h "$DB_PATH" 2>/dev/null | cut -f1 || echo '未生成'))"
    # Web 健康检查（内部 + 公网鉴权）
    if systemctl --user is-active --quiet czsc-mi-web.service 2>/dev/null; then
      HC=$(curl -s --max-time 5 "http://127.0.0.1:${WEB_INTERNAL_PORT}/_stcore/health" 2>/dev/null || true)
      [ "$HC" = "ok" ] && ok "Web 内部健康检查 ok（127.0.0.1:${WEB_INTERNAL_PORT}）" || warn "Web 内部健康检查未就绪（可能仍在启动）"
    fi
    if [ "$WEB_PUBLIC" = "1" ] && $SUDO systemctl is-active --quiet nginx 2>/dev/null; then
      CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:${WEB_PUBLIC_PORT}/" 2>/dev/null || true)
      [ "$CODE" = "401" ] && ok "公网入口鉴权生效（无凭据返回 401）" || warn "公网入口未返回 401（当前 $CODE），请检查 nginx 配置"
    fi
  fi
fi

# ---------------- 完成 ----------------
step "安装完成"
log "  后续步骤（按需手动执行）："
log "   1) 编辑 ${BASE_DIR}/.stockrc 填入 HITHINK_FINANCE_API_KEY 等凭据"
log "   2) 准备通达信本地数据到 ${TDX_VIPDOC_DIR}（如使用 tdx_local 数据源）"
log "   3) Web 已由 systemd user 服务托管: systemctl --user status czsc-mi-web.service"
log "      （WEB_PUBLIC=0 或未装服务时手动启动: cd ${CZSC_MI_DIR} && bash scripts/start_web.sh start）"
log "   4) 全量同步/扫描: czsc-mi sync / czsc-mi scan"
log "   5) 组件升级见 scripts/upgrade/README.md"
log "   6) 拼音首字母搜索需 pypinyin（已装），重启 Web 后生效"
# ============================================================================
# 附：实战安装过程总结（O2 轻量节点，2026-11 实测）
# ----------------------------------------------------------------------------
# 在 1G 内存 OCI 节点上完整走过本脚本全流程，关键经验：
#
#  1. 内存先行：小内存机器先加 swap（如 2G，swappiness=10），否则 pip 装大库会 OOM。
#  2. 数据库中转：大库跨机传输用"瘦身库 + gzip 分卷(500M/卷) + 合并 + quick_check"，
#     直接 scp 3.3G 全量库极易中断且损坏。
#  3. linger 必开：systemd --user 实例默认随最后一个 SSH 会话退出被整体回收，
#     Web 会"跑一会儿就消失"。步骤 2 已自动 loginctl enable-linger，勿省略。
#  4. 公网三道关：Streamlit 仅绑 127.0.0.1 → nginx Basic Auth 反代（含 WebSocket
#     升级头）→ iptables 放行+持久化 → 云平台安全组另行放行。缺一道都访问不通。
#  5. 内存护栏：WEB_MEMORY_MAX=700M 防一次重查询拖垮整机，超限自动重启。
#  6. 路径零硬编码：本脚本所有路径从 $HOME/环境变量推导（v0.10.62 起全仓约定），
#     任意机器直接可用；守卫测试 tests/test_no_hardcoded_paths.py 防回归。
#  7. 网络代理：GitHub clone 不通时用 https://gh-proxy.org/ 前缀代理。
#  8. 新 Python：源码/PPA 安装后需 apt install python3.x-venv，否则 venv 创建失败。
#
# 详细说明见同目录 INSTALL.md「实战安装记录」章节。
# ============================================================================
