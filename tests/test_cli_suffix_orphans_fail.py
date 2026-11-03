"""P2-2：suffix_orphans 可配置硬失败开关。

默认仅 QA WARN（返回 0，不阻断 cron）；设 MYSTERY_SUFFIX_ORPHANS_FAIL=1 时
若库内仍有孤儿行则返回退出码 4，供 daily_pipeline.sh / cron 据此告警；
指纹读取异常降级返回 0（不应因 QA 检查阻断主流程）。
"""
import io
import contextlib
import os

from mystery.apps import cli
import mystery.store.db as dbmod


def _patch_db(orphan_count, boom=False):
    class _DB:
        def data_fingerprint(self):
            if boom:
                raise RuntimeError("db locked")
            return {"suffix_orphans": orphan_count}
    dbmod.MysteryDB = lambda: _DB()


def test_default_warn_returns_zero():
    os.environ.pop("MYSTERY_SUFFIX_ORPHANS_FAIL", None)
    _patch_db(5)
    assert cli._suffix_orphans_hard_fail() == 0


def test_flag_off_with_orphans_is_zero():
    os.environ["MYSTERY_SUFFIX_ORPHANS_FAIL"] = ""
    _patch_db(5)
    assert cli._suffix_orphans_hard_fail() == 0


def test_flag_on_no_orphans_returns_zero():
    os.environ["MYSTERY_SUFFIX_ORPHANS_FAIL"] = "1"
    _patch_db(0)
    assert cli._suffix_orphans_hard_fail() == 0


def test_flag_on_with_orphans_returns_four():
    os.environ["MYSTERY_SUFFIX_ORPHANS_FAIL"] = "1"
    _patch_db(3)
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        rc = cli._suffix_orphans_hard_fail()
    assert rc == 4
    assert "exit 4" in buf.getvalue()


def test_fingerprint_error_degrades_to_zero():
    os.environ["MYSTERY_SUFFIX_ORPHANS_FAIL"] = "1"
    _patch_db(0, boom=True)
    assert cli._suffix_orphans_hard_fail() == 0
