# 外部依赖一键升级脚本（W104）

为 czsc_mi 依赖的每个外部组件提供**独立**的一键升级脚本，含版本记录、备份、升级、校验、失败自动回滚。

## 组件与升级方式

| 脚本 | 组件 | 升级方式 | 校验 |
|---|---|---|---|
| `upgrade_czsc.sh` | czsc 缠论内核 | `pip install -U czsc` | import + 版本 |
| `upgrade_pip_libs.sh` | pandas/openpyxl/streamlit/mootdx/baostock/duckdb/pypinyin 等 | `pip install -U` | import |
| `upgrade_ths.sh` | 同花顺 Financial-API | gitee `git pull` | fuyao 脚本存在 |
| `upgrade_tdx.sh` | 通达信 tdx-api | git pull + docker 重建容器 | healthz + REST 200 |
| `upgrade_hermes.sh` | Hermes 飞速通道 | git pull + 独立 venv 重装 | `hermes --version` |
| `upgrade_all.sh` | 以上全部 | 按序调用 | 汇总退出码 |

## 用法

```bash
cd <你的仓库目录>/scripts/upgrade     # 如 $HOME/stock/czsc_mi/scripts/upgrade

# 单个组件
bash upgrade_czsc.sh
bash upgrade_tdx.sh

# 全部（推荐先 dry-run 预演）
bash upgrade_all.sh --dry-run
bash upgrade_all.sh

# 只升级某个
bash upgrade_all.sh --only tdx
```

## 通用参数

| 参数 | 说明 |
|---|---|
| `--dry-run` | 只打印将执行的命令，不实际改动 |
| `--no-rollback` | 失败时不自动回滚（默认自动回滚到升级前 commit/版本） |
| `--only <组件>` | 仅 `upgrade_all.sh` 支持 |

## 安全设计

- **版本记录**：每次升级前后版本写入 `data/dep_versions.json`（含时间、方式）。
- **自动回滚**：
  - git 组件 → 回滚到升级前 commit；
  - pip 组件 → `pip install pkg==旧版本`；
  - tdx → 回滚 commit + 用旧镜像 ID 重启容器。
- **日志**：`logs/upgrade_<组件>.log`，`upgrade_all.sh` 汇总结果。
- **tdx 重启**：升级 tdx 会短暂重启 8080 容器（分钟 MACD 定时检测依赖它），建议在非交易时段执行。
- **Hermes**：只升级 send 通道，不启动 gateway（W96 已禁用）。

## 路径覆盖

所有路径可用环境变量覆盖（便于一键安装脚本复用）。默认值从用户 home 推导
（`DEP_BASE_DIR` 默认 `$HOME`），无需硬编码：

```bash
DEP_BASE_DIR=$HOME          # 基础目录（默认，可省）
STOCK_DIR=$HOME/stock       # stock 根目录（默认，可省）
VENV=$HOME/venv             # Python venv（默认，可省）
CZSC_MI_DIR=...             # 主项目目录
```

> 说明：`_common.sh` 内已用 `${DEP_BASE_DIR:-$HOME}` 等写法自动推导，
> 上面仅在需要指向非默认位置时显式覆盖即可。
