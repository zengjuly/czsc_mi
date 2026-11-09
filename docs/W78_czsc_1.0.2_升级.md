# W78：czsc 升级 1.0.1 → 1.0.2（v0.10.37）

## 结论

低风险升级，已全链路验证后落地。生产 venv 从 `czsc==1.0.1` 升到 `1.0.2`，
`pyproject.toml` 的 `[chan]` 下限从 `czsc>=1.0.1,<2.0` 抬到 `czsc>=1.0.2,<2.0`。
**不改评分 / 信号 / `RULE_VER` / `MYSTERY_CHAN_SCORE`（保持 0）/ core 零 IO 约束，
不引入 `CzscTrader` 或批量信号注册。**

## 事实核查（纠正建议中的偏差）

| 项 | 建议说法 | 实测 |
| --- | --- | --- |
| 版本 | czsc_mi v0.10.35 | 实为 v0.10.36（W77 `33c5903`，已提交未推送） |
| 依赖现状 | 「长期固定 1.0.1」 | `[chan]=czsc>=1.0.1,<2.0`，生产实装 1.0.1，本就未 pin 死 |
| 系统状态页显示 `czsc.__version__` | 待做 | **已完成**（`czsc_version()` + 「czsc 版本」字段，见 `system.py`） |
| `docs/MIGRATION_NOTES.md` | 对照官方迁移文档 | czsc master 的 `docs/` 下**无此文件**（只有 `signal_contracts.md` / `release_validation.md`） |
| Adapter 直接依赖信号键「概率低」 | — | **实际有**：`czsc_adapter.py::signal_flags` 用 `czsc._native.call_signal` 直调 5 个信号函数；仅 `MYSTERY_CHAN_SCORE=1` 混合分路径执行，线上为 0，不影响生产分数，但升级必须实测 |

## 1.0.2 依赖变化

`pip install --dry-run czsc==1.0.2` 结果：除 `czsc` 本体外，**唯一连带变更**是
`urllib3 2.7.0 → 2.8.0`（1.0.2 新增 `urllib3>=2.8.0`）。其余
`wbt>=0.2.1` / `requests-toolbelt` / `polars>=0.20` 等生产 venv 均已满足。
wheel 为 `czsc-1.0.2-cp310-abi3-manylinux_2_17_x86_64`（abi3，兼容 py3.12，无需编译）。

## 验证链（升级前，隔离目录 `/tmp/czsc102`，不触碰生产 venv）

1. **隔离试装**：`pip install --no-deps --target=/tmp/czsc102 czsc==1.0.2`，再补
   `urllib3==2.8.0`，用 `PYTHONPATH` 前置构造「完整升级后状态」。
2. **API 契约**：`CZSC / Freq / format_standard_kline / RawBar`、
   `czsc.utils.plotting.lightweight.plot_czsc`、`_native.call_signal` 全部存在；
   `_extract` 依赖的 `bi_list/fx_list/zs_list/bars_raw/finished_bis` 及 bi/zs/bar
   属性齐全。
3. **5 个信号函数实测**（adapter.signal_flags 依赖）：
   `cxt_first_buy_V221126` / `cxt_second_bs_V240524` / `cxt_third_bs_V230319` /
   `tas_macd_bc_V240307` / `tas_macd_bc_V230803` —— `call_signal` 全部正常返回，
   且均在 1.0.2 的 `list_signal_names()` 注册表内。**信号键迁移未命中本项目。**
4. **金标三只 ChanStructure 对拍**（`sh600519` / `sz000001` / `sh600150`，同源
   fixture）：1.0.1 与 1.0.2 的 `n_fx / n_bi / n_zs / last_bi_dir /
   last_bi_confirmed / in_zs / zs_position / leave_zs / bi_stretch /
   last_zs_finished / 中枢 zg·zd·gg·dd / 末笔起止与高低` **逐字段零漂移**。
   注：金标分数测试（`test_score_offline.py`）是纯 core 路径、不 import czsc，
   chan 关闭时「分差 ≤1」天然成立，无需也不应作为升级验收重点。
5. **数据源冒烟**：`requests 2.34.2 / urllib3 2.8.0 / baostock` 导入正常。
6. **全量 pytest**：完整升级态下 `pytest -o addopts= -q -m "not integration"`
   → **563 passed / 15 deselected**。

## 落地改动

- `pyproject.toml`：`chan` 依赖下限 `>=1.0.2`；版本 `0.10.36 → 0.10.37`。
- `mystery/__init__.py`：`__version__ 0.10.37`。
- `tests/test_w78_czsc_upgrade.py`（新增 3 例）：依赖下限文本断言 +
  czsc 可用时的 CZSC 结构契约 / 5 信号函数可调用护栏（不硬编码版本，部署窗口内
  1.0.1 与 1.0.2 均通过；无 czsc 时 skip）。
- 本文档。
- **部署**：生产 venv `pip install "czsc>=1.0.2,<2.0"`（会带 urllib3 2.8.0），
  重启 Web 后系统状态页「czsc 版本」显示 1.0.2。

## 约束遵守

- `MYSTERY_CHAN_SCORE` 保持 0（混合分不进综合分），`MYSTERY_CHAN_ENABLED=1` 不变。
- chip_low / 三浪未乘进默认综合分；`RULE_VER` 不变；core 零 IO 不变。
- 唯一分析入口不变；`czsc` 仍只在 `czsc_adapter.py` 内 import。

## 回退（若 1.0.2 出现意外）

```bash
/home/ai/ai_runner/venv/bin/python -m pip install "czsc==1.0.1" "urllib3==2.7.0"
systemctl --user restart czsc-mi-web.service
# 并把 pyproject.toml 的 chan 下限与版本号回退到 W77 状态后提交
```
代码层面对 1.0.1 / 1.0.2 双兼容（`test_w78` 两个版本都跑过），回退不会破坏评分链路。
