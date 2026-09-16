"""W38 环境变量集中控制回归：mystery.config 导入时自动 setdefault 注入 ~/.stockrc。"""
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_stockrc_injected_on_import():
    """裸环境（env -i）import mystery.config 后关键变量必须到位。"""
    code = (
        "import sys, os; sys.path.insert(0, %r);"
        "import mystery.config;"
        "print(os.environ.get('MYSTERY_DB_PATH',''));"
        "print(os.environ.get('MYSTERY_CHAN_ENABLED',''))" % REPO
    )
    r = subprocess.run(
        [sys.executable, "-c", code],
        env={"HOME": os.path.expanduser("~"), "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stderr
    db, chan = r.stdout.split()
    assert db.endswith("mystery_cache.db"), db
    assert chan == "1", chan


def test_existing_env_wins(tmp_path):
    """已有 env 优先：load_stockrc 不覆盖显式设置（setdefault 语义）。"""
    from mystery import config as cfgmod
    rc = tmp_path / "stockrc"
    rc.write_text("export MYSTERY_TEST_VAR=from_file\n", encoding="utf-8")
    os.environ["MYSTERY_TEST_VAR"] = "from_env"
    try:
        n = cfgmod.load_stockrc(str(rc))
        assert os.environ["MYSTERY_TEST_VAR"] == "from_env"
        assert n == 0  # 已存在不计注入数
        del os.environ["MYSTERY_TEST_VAR"]
        n2 = cfgmod.load_stockrc(str(rc))
        assert os.environ["MYSTERY_TEST_VAR"] == "from_file"
        assert n2 == 1
    finally:
        os.environ.pop("MYSTERY_TEST_VAR", None)


def test_missing_file_silent(tmp_path):
    from mystery import config as cfgmod
    assert cfgmod.load_stockrc(str(tmp_path / "nope")) == 0
