"""verify_unified_analysis — 同股三路径评分一致性验证（P3 实现）。

三路径：个股页 / daily / 扫描（底层都是 services.analyze.analyze_one_stock），
要求 score 差 ≤ 1。P1 先验证单票与 stock_analyzer 金标。
"""
from __future__ import annotations

import sys


def main() -> int:
    print("P3 实现：调 mystery.services.analyze.analyze_one_stock 对比三入口")
    return 0


if __name__ == "__main__":
    sys.exit(main())
