#!/usr/bin/env python3
"""calibrate_chan — 阶段6B 离线标定（010.md）。

对固定 K 线快照的 N 只票（默认 30：金标三只 + 主升 + 年线否决 + 无结构 +
随机样本），计算：
  - 现行 S_chan（scorer.chan_score 四标量口径）
  - 6A 新字段（zs_position / leave_zs / bi_stretch / n_zs / last_zs_finished）
  - czsc 信号（call_signal 直调：一买 / 二买 / 三买 / MACD双分型背驰）
输出 JSON 表供人工对照，规则表定稿写入 docs/010.md。

**本脚本只读，不写库、不进评分路径。** 生产路径永远不 import 本文件。

用法（需真实库环境）:
    source /home/ai/ai_runner/.stockrc
    export MYSTERY_DB_PATH=... 
    python scripts/calibrate_chan.py [--n 30] [--out /tmp/chan_calib.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from mystery.adapters.czsc_adapter import CzscAdapter  # noqa: E402
from mystery.adapters.market import MarketDataClient  # noqa: E402
from mystery.core.scorer import chan_score  # noqa: E402

# 信号名 → call_signal params（czsc 1.0.1 实测形态，010.md 修正①）
SIGNALS = {
    "buy1": ("cxt_first_buy_V221126", {"di": 1}),
    "buy2": ("cxt_second_bs_V240524",
             {"di": 1, "big_gap_factor": 0.95, "window": 8}),
    "buy3": ("cxt_third_bs_V230319",
             {"di": 1, "window": 8, "bm": 20, "fast": 5, "slow": 20,
              "vols_ma": 5}),
    "bc": ("tas_macd_bc_V230803", {}),
}


def _signal_values(series, freq_name: str) -> dict:
    """在日线 CZSC 对象上直调 4 信号，返回 {tag: value}。失败返回空。"""
    out = {}
    try:
        from czsc import _native as n
        adapter = CzscAdapter()
        c = adapter._build_czsc(series)
        if c is None:
            return {}
        for tag, (name, params) in SIGNALS.items():
            try:
                sigs = n.call_signal(name, c, params=params)
                out[tag] = "|".join(str(s.value) for s in sigs)
            except Exception as e:  # 版本漂移 → 该因子记空
                out[tag] = f"ERR:{type(e).__name__}"
    except Exception:
        pass
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--out", default="/tmp/chan_calib.json")
    args = ap.parse_args()

    market = MarketDataClient({})
    db = market.db
    # 样本池：金标三只优先，其余从股票池随机（证券口径）
    fixed = ["sh600519", "sz000001", "sh600150"]
    pool = [r["code"] for r in db.get_stock_list(stock_only=True)]
    import random
    random.seed(62)  # 固定随机种子，样本可复现
    picks = fixed + [p for p in random.sample(pool, min(args.n - len(fixed),
                                                       len(pool)))
                     if p not in fixed]

    results = []
    for sym in picks:
        try:
            daily = market.fetch_bars(sym, "1d")
        except Exception as e:
            results.append({"symbol": sym, "error": str(e)[:100]})
            continue
        if not daily.bars:
            results.append({"symbol": sym, "error": "no bars"})
            continue
        chan = CzscAdapter().analyze_multi(daily, ["1d", "1w"])
        c1 = chan.get("1d")
        sigs = _signal_values(daily, "日线")
        # 现行 S_chan（用旧四标量）
        s_old = chan_score(chan)
        results.append({
            "symbol": sym,
            "trade_date": str(daily.bars[-1].dt)[:10],
            "close": float(daily.bars[-1].close),
            "s_chan_old": s_old,
            "last_bi_dir": c1.last_bi_dir if c1 else "",
            "last_bi_confirmed": c1.last_bi_confirmed if c1 else False,
            "in_zs_time": c1.in_zs if c1 else False,
            "zs_position": c1.zs_position if c1 else "",
            "leave_zs": c1.leave_zs if c1 else "",
            "bi_stretch": c1.bi_stretch if c1 else None,
            "n_zs": c1.n_zs if c1 else 0,
            "last_zs_finished": c1.last_zs_finished if c1 else False,
            "w_last_bi_dir": chan["1w"].last_bi_dir if "1w" in chan else "",
            **{f"sig_{k}": v for k, v in sigs.items()},
        })
        print(f"[calib] {sym} S_old={s_old} pos={c1.zs_position if c1 else '-'} "
              f"sigs={ {k: sigs.get(k, '')[:20] for k in SIGNALS} }")

    Path(args.out).write_text(
        json.dumps(results, ensure_ascii=False, indent=1))
    # 汇总：各因子取值分布（标定依据）
    df = pd.DataFrame([r for r in results if "error" not in r])
    print("\n== 因子分布 ==")
    for col in ("zs_position", "leave_zs", "last_bi_dir", "in_zs_time",
                "last_zs_finished", "sig_buy1", "sig_buy2", "sig_buy3",
                "sig_bc"):
        if col in df:
            print(f"\n[{col}]")
            print(df[col].value_counts().head(8).to_string())
    print(f"\n共 {len(df)} 只有效样本 → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
