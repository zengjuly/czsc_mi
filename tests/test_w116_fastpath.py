"""W116 日报提速：快速通道纯判断函数单测（完全离线，不碰数据库/网络）。

覆盖三个核心修复点：
1. _pick_incremental_baseline：忽略 None（无数据票），杜绝 min_last=1900
   导致 DuckDB 全表扫描（9 分钟 + OOM 真凶）。
2. fresh_codes_by_latest：数据充足（含轻微 stale）判 fresh；严重落后/无数据
   判 stale；北交所票不参与。
3. fresh_sectors_by_latest：已最新板块识别，ths_ 前缀格式归一。
"""
import pytest

from mystery.services.sync import (
    _pick_incremental_baseline,
    fresh_codes_by_latest,
    fresh_sectors_by_latest,
)


class TestPickIncrementalBaseline:
    def test_ignores_none(self):
        # 关键回归：None（无数据票）不能把基准拉到 1900
        assert _pick_incremental_baseline(
            ["2026-11-19", None, "2026-11-20", ""]) == "2026-11-19"

    def test_all_none_returns_none(self):
        assert _pick_incremental_baseline([None, None, ""]) is None

    def test_single_value(self):
        assert _pick_incremental_baseline(["2026-11-20"]) == "2026-11-20"

    def test_empty(self):
        assert _pick_incremental_baseline([]) is None

    def test_strips_time_suffix(self):
        # SQLite MAX(date) 可能带时间，应截成日期
        assert _pick_incremental_baseline(["2026-11-19 00:00:00"]) == "2026-11-19"


def _db_code_of(c):
    return "sh." + c[2:] if c.startswith("sh") else c


def _is_bj(c):
    return c.endswith(".BJ")


class TestFreshCodesByLatest:
    db_code_of = staticmethod(_db_code_of)
    is_bj = staticmethod(_is_bj)

    def test_all_fresh(self):
        codes = ["sh600519", "sz000001"]
        last_map = {"sh.600519": "2026-11-20", "sz000001": "2026-11-20"}
        got = fresh_codes_by_latest(
            codes, last_map, _db_code_of, _is_bj, "2026-11-20")
        assert got == set(codes)

    def test_mild_stale_within_days_is_fresh(self):
        # 落后 1 天（<=stale_days=3）仍判 fresh，避免在线空等超时
        codes = ["sh600519"]
        last_map = {"sh.600519": "2026-11-19"}
        got = fresh_codes_by_latest(
            codes, last_map, _db_code_of, _is_bj,
            "2026-11-20", stale_days=3)
        assert got == {"sh600519"}

    def test_severe_stale_is_not_fresh(self):
        codes = ["sh600519"]
        last_map = {"sh.600519": "2026-11-01"}  # 落后 19 天
        got = fresh_codes_by_latest(
            codes, last_map, _db_code_of, _is_bj,
            "2026-11-20", stale_days=3)
        assert got == set()

    def test_no_data_is_not_fresh(self):
        codes = ["sh688845"]
        got = fresh_codes_by_latest(
            codes, {}, _db_code_of, _is_bj, "2026-11-20")
        assert got == set()

    def test_bj_excluded(self):
        codes = ["sh600519", "bj920593.BJ"]
        last_map = {"sh.600519": "2026-11-20", "bj920593.BJ": "2026-11-20"}
        got = fresh_codes_by_latest(
            codes, last_map, _db_code_of, _is_bj, "2026-11-20")
        assert got == {"sh600519"}

    def test_empty_latest(self):
        codes = ["sh600519"]
        got = fresh_codes_by_latest(
            codes, {"sh.600519": "2026-11-20"},
            _db_code_of, _is_bj, "")
        assert got == set()


class TestFreshSectorsByLatest:
    def test_ths_prefix(self):
        codes = ["ths_881101", "ths_886109"]
        last_dates = {"ths_881101": "2026-11-20", "ths_886109": "2026-11-19"}
        got = fresh_sectors_by_latest(codes, last_dates, "2026-11-20")
        assert got == {"ths_881101"}

    def test_plain_code_normalized(self):
        # 无 ths_ 前缀 + .TI 后缀应归一到 ths_XXXXXX
        codes = ["881101.TI"]
        last_dates = {"ths_881101": "2026-11-20"}
        got = fresh_sectors_by_latest(codes, last_dates, "2026-11-20")
        assert got == {"881101.TI"}

    def test_all_fresh(self):
        codes = ["ths_881101", "ths_881103"]
        last_dates = {c: "2026-11-20" for c in codes}
        got = fresh_sectors_by_latest(codes, last_dates, "2026-11-20")
        assert got == set(codes)
