#!/usr/bin/env python3
"""G4 轻加权 scan 阈值再标定（W41 复跑：缺口最小幅度闸门 GAP_MIN_PCT 修复后）。

用法：python recalib_patterns.py <n=250> <seed=7>

方法（同 G4 首次再标定）：对全市场做分层抽样 n 只（步长抽样，起始偏移=seed），
每只仅跑一次 analyze_one_stock（默认关加权 → base 分），再用
combine(pattern_weight=*) 在同一 bd 上推导「关/开」两种综合分，避免重复流水线。
结果落 recalib_patterns.json，并打印：综合分分布、阈值命中数、三形态命中率、
综合分变动、衰竭缺口 −6 语义核查。

目的：验证 patterns_adv.detect_gap 加 GAP_MIN_PCT 闸门后，缺口命中率是否从
100% 降到 ~30%（+4 权重由「非选择型」变为有区分度），并确认 scan 阈值影响。
"""
import sys
import json
import statistics

from mystery.services.analyze import AnalysisService, analyze_one_stock
from mystery.core.scorer import combine


def _stats(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return {"n": 0}
    s = sorted(vals)
    p90 = s[min(len(s) - 1, int(len(s) * 0.9) - 1)] if len(s) > 1 else s[0]
    return {
        "n": len(s),
        "mean": round(statistics.mean(s), 2),
        "median": statistics.median(s),
        "p90": round(p90, 2),
        "max": max(s),
        "min": min(s),
    }


def main():
    N = int(sys.argv[1]) if len(sys.argv) > 1 else 250
    SEED = int(sys.argv[2]) if len(sys.argv) > 2 else 7

    svc = AnalysisService()
    codes = [s["code"] for s in svc.market.fetch_stock_list()]
    print(f"全市场 {len(codes)} 只，抽样 n={N} seed={SEED}")

    step = max(1, len(codes) // N)
    start = SEED % step
    sample = codes[start::step][:N]
    if len(sample) < N:  # 不足则从头补
        sample = (sample + codes)[:N]
    print(f"实际抽样 {len(sample)} 只 (step={step}, start={start})")

    rows = []
    for i, code in enumerate(sample, 1):
        try:
            res = analyze_one_stock(
                code, include_detail=True, use_cache=False, fill_financial=False
            )
            bd = res.mystery
            sig = bd.signal or {}
            base = combine(bd, pattern_weight=False)[0]
            weighted = combine(bd, pattern_weight=True)[0]
            base = base or 0.0
            weighted = weighted or 0.0
            rows.append({
                "symbol": code,
                "name": getattr(res, "name", None),
                "base": base,
                "weighted": weighted,
                "veto": sig.get("年线滤网") is False,
                "gap_up": bool(sig.get("向上跳空")),
                "gap_type": sig.get("缺口类型"),
                "bull_engulf": bool(sig.get("强势反包阳")),
                "low_ground": bool(sig.get("低位地量")),
            })
        except Exception as e:  # noqa: BLE001
            rows.append({"symbol": code, "error": str(e)[:160]})
        if i % 50 == 0:
            print(f"  ...{i}/{len(sample)}")

    ok = [r for r in rows if "error" not in r]
    errs = [r for r in rows if "error" in r]
    print(f"成功 {len(ok)}，失败 {len(errs)}")
    if errs:
        print("失败样例:", [(e["symbol"], e["error"]) for e in errs[:5]])

    base_all = [r["base"] for r in ok]
    w_all = [r["weighted"] for r in ok]
    base_pos = [r["base"] for r in ok if r["base"] > 0]
    w_pos = [r["weighted"] for r in ok if r["weighted"] > 0]

    print("\n== 综合分分布（关 vs 开 轻加权）==")
    print("全样本 关:", _stats(base_all))
    print("全样本 开:", _stats(w_all))
    print("base>0 子集 关:", _stats(base_pos))
    print("base>0 子集 开:", _stats(w_pos))

    print("\n== 阈值命中数（base>0 子集，>=阈值 的票数）==")
    thr = {}
    for t in (50, 60, 70, 76):
        c = sum(1 for v in base_pos if v >= t)
        o = sum(1 for v in w_pos if v >= t)
        thr[str(t)] = {"off": c, "on": o, "delta": o - c}
        print(f"  >={t}: 关 {c} / 开 {o} / Δ {o - c}")

    print("\n== 三形态命中（含闸门后）==")
    gap_up = sum(1 for r in ok if r["gap_up"])
    g_b = sum(1 for r in ok if r["gap_type"] == "突破")
    g_c = sum(1 for r in ok if r["gap_type"] == "持续")
    g_e = sum(1 for r in ok if r["gap_type"] == "衰竭")
    eng = sum(1 for r in ok if r["bull_engulf"])
    low = sum(1 for r in ok if r["low_ground"])
    rate = round(100 * gap_up / len(ok), 1) if ok else 0
    print(f"向上跳空缺口: {gap_up}/{len(ok)} ({rate}%)")
    print(f"  突破 {g_b} / 持续 {g_c} / 衰竭 {g_e}")
    print(f"强势反包阳: {eng}")
    print(f"低位地量: {low}")

    deltas = [r["weighted"] - r["base"] for r in ok if r["base"] > 0]
    up = sum(1 for d in deltas if d > 0)
    dn = sum(1 for d in deltas if d < 0)
    same = sum(1 for d in deltas if d == 0)
    print(f"\n== base>0 子集综合分变动（开−关）== 上升 {up} / 不变 {same} / 下降 {dn}")
    if deltas:
        print(f"最大升 {max(deltas)} / 最大降 {min(deltas)}")

    print("\n== 衰竭缺口 −6 语义核查 ==")
    found = False
    for r in ok:
        if r["gap_type"] == "衰竭":
            found = True
            print(f"  {r['symbol']} base={r['base']} weighted={r['weighted']} "
                  f"Δ={r['weighted'] - r['base']} veto={r['veto']}")
    if not found:
        print("  本次样本中无衰竭缺口")

    out = {
        "n": N, "seed": SEED, "total_market": len(codes),
        "gap_up_rate_pct": rate,
        "stats_base": _stats(base_all),
        "stats_weighted": _stats(w_all),
        "stats_base_pos": _stats(base_pos),
        "stats_weighted_pos": _stats(w_pos),
        "threshold_hits": thr,
        "patterns": {"gap_up": gap_up, "gap_break": g_b, "gap_cont": g_c,
                     "gap_exhaust": g_e, "bull_engulf": eng, "low_ground": low},
        "rows": rows,
    }
    with open("recalib_patterns.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("\n已落 recalib_patterns.json")


if __name__ == "__main__":
    main()
