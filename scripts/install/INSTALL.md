# czsc_mi 新系统一键安装（W104 / W107）

`install_czsc_mi.sh` 在干净的 Ubuntu/Debian 系统上安装 czsc_mi 及全部依赖，幂等可重复运行。

## 快速开始

```bash
# 1) 复制配置模板并按需修改（尤其是 API key、仓库地址、鉴权密码）
cp install.env.example install.env
vim install.env

# 2) 先预演，确认将执行的动作
sudo bash install_czsc_mi.sh --config install.env --dry-run

# 3) 实际安装
sudo bash install_czsc_mi.sh --config install.env
```

## 安装内容（12 步）

| 步骤 | 内容 | 可跳过参数 |
|---|---|---|
| 1 | 系统包（python3-venv/pip/git/curl/build-essential；WEB_PUBLIC=1 时含 nginx+htpasswd） | `--skip syspkg,docker` |
| 2 | 运行用户 + 目录结构 + **enable-linger（服务常驻，关键）** | `--skip userdir` |
| 3 | Python venv + pip 升级 | `--skip venv` |
| 4 | clone czsc_mi 主仓库 | `--skip repo` |
| 5 | pip 依赖（czsc/streamlit/pypinyin/pandas/mootdx/baostock/duckdb…） | `--skip pip` |
| 6 | clone 外部组件（Financial-API / tdx-api / hermes）+ 启动 tdx 容器 | `--skip components` |
| 7 | 写 `.stockrc` 环境配置（600 权限） | `--skip env` |
| 8 | 初始化数据库（首次连接自动建 schema + migrations） | `--skip db` |
| 9 | 安装分钟 MACD 盘中 cron | `--skip cron` |
| 10 | Web systemd user 服务（仅监听 127.0.0.1，Restart=always，可选 MemoryMax） | `--skip web` |
| 11 | 公网访问与鉴权：nginx Basic Auth 反代 + iptables 放行并持久化（WEB_PUBLIC=1 时） | `--skip publicweb` |
| 12 | 端到端校验（CLI / import / 数据库 / Web 内部健康 / 公网入口 401） | `--skip verify` |

`--skip` 接受逗号分隔的多步骤名。

## 公网访问与鉴权（W107）

设 `WEB_PUBLIC=1` 后安装脚本自动完成（对应 O2 生产实战配置）：

| 项 | 默认值 | 说明 |
|---|---|---|
| `WEB_PUBLIC_PORT` | 18601 | nginx 公网监听（0.0.0.0），Basic Auth 保护 |
| `WEB_INTERNAL_PORT` | 18602 | Streamlit 真实端口，仅绑 127.0.0.1，公网无法直连 |
| `WEB_AUTH_USER` | czscmi | Basic Auth 用户名 |
| `WEB_AUTH_PASSWORD` | 空 | 留空则自动生成强密码，打印并保存到 `<STOCK_DIR>/.web_password`（600） |
| `WEB_MEMORY_MAX` | 空 | systemd MemoryMax，小内存机器建议 700M，超限自动重启 |

要点：

- Streamlit 无内置鉴权，**公网暴露必须走 nginx Basic Auth 反代**（含 WebSocket 升级头，否则页面连不上后端）。
- 密码修改：`sudo htpasswd -b /etc/nginx/czsc_mi.htpasswd czscmi 新密码`。
- iptables 规则会写入 `/etc/iptables/rules.v4` 持久化（需 iptables-persistent/netfilter-persistent）；
  **云平台安全组（OCI/AWS 等）需另行放行公网端口**。
-linger 已在步骤 2 开启：这是"服务跑一会儿就消失"问题的根治手段，见下文实战记录。

## 实战安装记录（O2 轻量节点，1G 内存，供参考）

2026-11 在 OCI 1G 内存节点（ubuntu 用户、Python 3.14）完整走过一遍，踩坑与结论：

1. **内存是第一约束**：先加 2G swap（`fallocate` + `mkswap` + `swappiness=10`），否则 pip 装大库会 OOM。
2. **数据库从 O1 瘦身中转**：全量库 3.3G 直接 scp 极易中断且损坏；改为按日期裁剪出 1.5G 瘦身库，
   gzip 分卷（500M/卷）+ 分卷合并 + `PRAGMA quick_check` 校验，稳定可靠。
3. **tencent/gh 代理源**：clone 不通时用 `https://gh-proxy.org/` 前缀代理 GitHub。
4. **Python 版本**：源码编译/PPA 装新 Python 后务必 `apt install python3.x-venv`，否则 venv 模块缺失。
5. **服务必须 linger**：曾出现"Web 访问一下就消失"，根因是 systemd --user 实例随最后一个 SSH 会话
   退出被整体回收（日志可见用户管理器 PID 反复变化）。`sudo loginctl enable-linger <user>` 根治。
6. **公网暴露三道关**：①Streamlit 改 127.0.0.1 监听；②系统 iptables 放行入口端口（持久化）；
   ③云安全组放行。生产用 nginx（约几 MB 内存）加 Basic Auth，Streamlit 本体不对公网。
7. **MemoryMax 防拖垮**：1G 机器给 Web 设 700M 上限，超限自动重启，别让一次重查询打死整机。
8. **禁止硬编码绝对路径**（v0.10.62 起）：全部路径从 `__file__`/`$HOME`/环境变量推导，
   任意机器开箱即用；守卫测试 `tests/test_no_hardcoded_paths.py` 防回归。

## 关键设计

- **幂等**：已存在的用户/venv/仓库/容器会跳过重建；crontab 去重。
- **配置外置**：所有路径、仓库地址、凭据走环境变量或 `--config` 文件，不硬编码；敏感 API key 写入 `.stockrc`（600），Web 密码写入 `<STOCK_DIR>/.web_password`（600）。
- **服务常驻**：enable-linger + systemd user service（Restart=always），SSH 断开、进程崩溃都会自动恢复。
- **安全默认**：Streamlit 仅绑 127.0.0.1；公网入口强制 Basic Auth；凭据文件 600 权限。
- **数据源解耦**：数据库首次连接自动执行 `schema.sql` + `migrations/*.sql`，安装脚本只需建空库路径。
- **docker 可选**：tdx-api 依赖 docker；未装 docker 时脚本会提示但不中断其它步骤。
- **失败宽容**：单个步骤失败记录 warn 后继续，最后统一给出后续待办清单。

## tdx 数据环境（W109）

tdx-api 提供通达信在线行情（个股/指数日K、分钟K），czsc_mi 经 `TDX_API_URL`（默认 `http://127.0.0.1:8080/api`）调用。安装脚本步骤 6 按 `TDX_MODE` 自动部署：

| 模式 | 依赖 | 说明 |
|---|---|---|
| `auto`（默认） | — | 有 docker 走 `docker`，否则走 `binary` |
| `binary` | 无 | 预编译 `stock-web` 二进制裸跑 + systemd user 服务 `czsc-mi-tdxapi.service`，小内存机器推荐 |
| `docker` | docker | `docker compose up -d --build`（含 Go 编译） |
| `tunnel` | ssh | 不本地部署，`TDX_API_URL` 指向 SSH 隧道转发地址（需自行配 `tdx-tunnel`） |

binary 模式二进制来源 `TDX_BINARY_SRC`（URL 或本机路径）留空时依次尝试：源码仓库缓存 → 本地 docker 镜像导出。

> 注意：tdx-api **首次启动需 2-3 分钟初始化**（连接通达信公共服务器并下载全市场 5.3 万条代码缓存），期间 8080 未监听属正常；之后常驻内存约 120MB。

## 安装后待办

1. 编辑 `${BASE_DIR}/.stockrc` 填入 `HITHINK_FINANCE_API_KEY` 等凭据。
2. 如使用 tdx_local 数据源，准备通达信数据到 `TDX_VIPDOC_DIR`。
3. Web 已由 systemd user 服务托管：`systemctl --user status czsc-mi-web.service`；
   公网入口 `http://<IP>:<WEB_PUBLIC_PORT>`（账号 `WEB_AUTH_USER`，密码见 `.web_password`）。
4. 全量同步/扫描：`czsc-mi sync` / `czsc-mi scan`。
5. 拼音首字母搜索需 pypinyin（脚本已装），重启 Web 后生效。
6. 组件升级见 `scripts/upgrade/README.md`。

## 回滚

安装脚本不删除任何既有数据。如需整体移除：

```bash
systemctl --user disable --now czsc-mi-web.service        # 停 Web 服务
sudo loginctl disable-linger <user>                        # 关闭常驻（可选）
sudo rm /etc/nginx/sites-enabled/czsc-mi                   # 撤公网入口
crontab -l | grep -v minute_macd_watch | crontab -         # 移除 cron
docker compose down                                        # 在 tdx-api 目录执行
# 按需删除目录（谨慎）：${CZSC_MI_DIR} / ${VENV} / ${STOCK_DIR}
```
