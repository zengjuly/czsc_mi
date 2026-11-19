# czsc_mi 新系统一键安装（W104）

`install_czsc_mi.sh` 在干净的 Ubuntu/Debian 系统上安装 czsc_mi 及全部依赖，幂等可重复运行。

## 快速开始

```bash
# 1) 复制配置模板并按需修改（尤其是 API key、仓库地址）
cp install.env.example install.env
vim install.env

# 2) 先预演，确认将执行的动作
sudo bash install_czsc_mi.sh --config install.env --dry-run

# 3) 实际安装
sudo bash install_czsc_mi.sh --config install.env
```

## 安装内容（10 步）

| 步骤 | 内容 | 可跳过参数 |
|---|---|---|
| 1 | 系统包（python3-venv/pip/git/curl/build-essential；docker 需另装） | `--skip syspkg,docker` |
| 2 | 运行用户 + 目录结构 | `--skip userdir` |
| 3 | Python venv + pip 升级 | `--skip venv` |
| 4 | clone czsc_mi 主仓库 | `--skip repo` |
| 5 | pip 依赖（czsc/streamlit/pypinyin/pandas/mootdx/baostock/duckdb…） | `--skip pip` |
| 6 | clone 外部组件（Financial-API / tdx-api / hermes）+ 启动 tdx 容器 | `--skip components` |
| 7 | 写 `.stockrc` 环境配置（600 权限）+ systemd env | `--skip env` |
| 8 | 初始化数据库（首次连接自动建 schema + migrations） | `--skip db` |
| 9 | 安装分钟 MACD 盘中 cron | `--skip cron` |
| 10 | 端到端校验（CLI / import / 数据库） | `--skip verify` |

`--skip` 接受逗号分隔的多步骤名。

## 关键设计

- **幂等**：已存在的用户/venv/仓库/容器会跳过重建；crontab 去重。
- **配置外置**：所有路径、仓库地址、凭据走环境变量或 `--config` 文件，不硬编码；敏感 API key 写入 `.stockrc`（600）。
- **数据源解耦**：数据库首次连接自动执行 `schema.sql` + `migrations/*.sql`，安装脚本只需建空库路径。
- **docker 可选**：tdx-api 依赖 docker；未装 docker 时脚本会提示但不中断其它步骤。
- **失败宽容**：单个步骤失败记录 warn 后继续，最后统一给出后续待办清单。

## 安装后待办

1. 编辑 `${BASE_DIR}/.stockrc` 填入 `HITHINK_FINANCE_API_KEY` 等凭据。
2. 如使用 tdx_local 数据源，准备通达信数据到 `TDX_VIPDOC_DIR`。
3. 启动 Web：`cd ${CZSC_MI_DIR} && bash scripts/start_web.sh start`。
4. 全量同步/扫描：`czsc-mi sync` / `czsc-mi scan`。
5. 拼音首字母搜索需 pypinyin（脚本已装），重启 Web 后生效。
6. 组件升级见 `scripts/upgrade/README.md`。

## 回滚

安装脚本不删除任何既有数据。如需整体移除：

```bash
crontab -l | grep -v minute_macd_watch | crontab -   # 移除 cron
docker compose -d  # 在 tdx-api 目录执行 down
# 按需删除目录（谨慎）：${CZSC_MI_DIR} / ${VENV} / ${STOCK_DIR}
```
