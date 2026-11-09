"""W81 回归：audit 换手缺失检查（轻量，仅最新交易日）+ 管线接入护栏。

验证点：
  1. 最新交易日 turn 缺失/<=0 且 volume>0 → turnover_zero_recent 检出；
  2. 占比 >50% → turnover_missing_likely=True（数据源缺失告警）；
  3. 正常换手数据 → 新检查为 0 / False（旧 5 项检查行为不变）;
  4. daily_pipeline.sh / weekly_shares.sh 含可选 audit 步骤（RUN_DATA_AUDIT
     默认关），daily 的 pipeline_status.json 含 data_audit 字段。
全部只读，不改动任何写入行为。
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mystery.store.audit import audit_kline

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


_COLS = ("code", "date", "period", "open", "high", "low", "close",
         "volume", "amount", "turn", "pctChg")


def _raw_insert(db_path, rows):
    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            f"INSERT INTO stock_kline_data {_COLS} VALUES ("
            f"{','.join('?' * len(_COLS))})", rows)
        conn.commit()
    finally:
        conn.close()


def test_audit_turnover_zero_detected(tmp_path):
    """最新交易日换手缺失（turn=0 / NULL 且 volume>0）应被检出。"""
    fp = str(tmp_path / "a.db")
    sqlite3.connect(fp).close()  # 占位，下面用带 schema 的建表
    from mystery.store.db import MysteryDB
    MysteryDB(db_path=fp)
    # 前一日正常 2 只 + 最新日 3 只中 2 只换手缺失（占比 2/3 > 50%）
    _raw_insert(fp, [
        ("sh.600519", "2024-01-01", "daily", 10, 11, 9, 10.5, 1000, 10500, 1.5, 0.5),
        ("sz.000001", "2024-01-01", "daily", 10, 11, 9, 10.5, 1000, 10500, 2.0, 0.5),
        ("sh.600519", "2024-01-02", "daily", 10, 11, 9, 10.6, 1000, 10500, 0.0, 0.5),
        ("sz.000001", "2024-01-02", "daily", 10, 11, 9, 10.6, 1000, 10500, None, 0.5),
        ("sz.000002", "2024-01-02", "daily", 10, 11, 9, 10.6, 1000, 10500, 3.0, 0.5),
    ])
    rep = audit_kline(fp, period="daily")
    assert rep["checks"]["latest_trade_date"] == "2024-01-02"
    assert rep["checks"]["turnover_zero_recent"] == 2
    assert rep["checks"]["turnover_zero_ratio"] > 0.5
    assert rep["checks"]["turnover_missing_likely"] is True
    samples = rep["samples"]["turnover_zero_recent"]
    assert sorted(s[0] for s in samples) == ["sh.600519", "sz.000001"]


def test_audit_turnover_clean_no_warning(tmp_path):
    """正常换手数据：新检查为 0 / 不告警，旧 5 项检查行为不变。"""
    fp = str(tmp_path / "b.db")
    from mystery.store.db import MysteryDB
    MysteryDB(db_path=fp)
    _raw_insert(fp, [
        ("sh.600519", "2024-01-02", "daily", 10, 11, 9, 10.5, 1000, 10500, 1.5, 0.5),
        ("sz.000001", "2024-01-02", "daily", 10, 11, 9, 10.5, 1000, 10500, 2.0, 0.5),
    ])
    rep = audit_kline(fp, period="daily")
    assert rep["checks"]["turnover_zero_recent"] == 0
    assert rep["checks"]["turnover_missing_likely"] is False
    # 旧检查项不受影响
    assert rep["checks"]["duplicate_pk"] == 0
    assert rep["checks"]["invalid_ohlc"] == 0
    assert rep["checks"]["volume_no_amount"] == 0


def test_pipeline_scripts_have_optional_audit_step():
    """日更/周更脚本含可选 audit 步骤，且默认关闭、失败不阻塞。"""
    daily = open(os.path.join(_ROOT, "scripts", "daily_pipeline.sh"),
                 encoding="utf-8").read()
    weekly = open(os.path.join(_ROOT, "scripts", "weekly_shares.sh"),
                  encoding="utf-8").read()
    # 日更：默认关、可开、写状态、不阻塞
    assert 'RUN_DATA_AUDIT:-0' in daily
    assert "czsc-mi audit --period daily" in daily
    assert '"data_audit"' in daily
    assert "不阻塞主流程" in daily
    assert "DATA_AUDIT_TIMEOUT:-900" in daily
    # 周更：同样默认关、失败不影响铺盘
    assert 'RUN_DATA_AUDIT:-0' in weekly
    assert "czsc-mi audit --period daily" in weekly
    assert "不影响铺盘结果" in weekly
