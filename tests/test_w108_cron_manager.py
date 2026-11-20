"""W108：cron_manager 解析/启停/增删 + Web 定时任务页与自选股列表改造测试。"""
import pytest

from mystery.services import cron_manager as cm

SAMPLE = """\
# 市场数据同步
30 17 * * 1-5 /bin/bash -c 'source /home/x/.stockrc; exec /home/x/venv/bin/marketdb auto-sync' >> /tmp/a.log 2>&1
# 收盘日报（飞书）
0 18 * * 1-5 /home/x/stock/czsc_mi/scripts/daily_feishu.sh
# minute macd 已停用示例
# 1,31 9-15 * * 1-5 /bin/bash /home/x/stock/czsc_mi/scripts/minute_macd_watch.sh
MYSTERY_DB_PATH=/home/x/stock/data/db/mystery_cache.db
# 纯注释行，不是任务
*/5 * * * * /usr/bin/other_tool --sync
"""


@pytest.fixture()
def fake_crontab(monkeypatch):
    recorded = {"content": SAMPLE}

    def run(cmd, **kw):
        class P:
            returncode = 0
            stderr = ""
            stdout = recorded["content"] if cmd == ["crontab", "-l"] else ""
        if cmd == ["crontab", "-"]:
            recorded["content"] = kw.get("input")
        return P()

    monkeypatch.setattr(cm.subprocess, "run", run)
    return recorded


def test_parse_basic(fake_crontab):
    jobs = cm.list_jobs()
    labels = [j.label for j in jobs]
    # marketdb / daily_feishu / minute_macd(停用) / other_tool 各一条，env 一条
    assert "行情库自动同步（marketdb）" in labels
    assert "收盘日报（飞书）" in labels
    assert "分钟 MACD 盘中监控" in labels
    assert "自定义任务" in labels
    macd = next(j for j in jobs if j.label == "分钟 MACD 盘中监控")
    assert macd.enabled is False
    assert macd.kind == "czsc_mi"
    assert macd.schedule == "1,31 9-15 * * 1-5"
    env = next(j for j in jobs if j.kind == "env")
    assert env.schedule == "" and env.enabled is True
    other = next(j for j in jobs if j.label == "自定义任务")
    assert other.kind == "other"


def test_parse_no_crontab_and_pure_comments():
    assert cm.parse_crontab("") == []
    assert cm.parse_crontab("# 只是注释\n\n# 备注说明行不是任务") == []


def test_set_enabled_toggle_roundtrip(fake_crontab):
    jobs = cm.list_jobs()
    macd = next(j for j in jobs if j.label == "分钟 MACD 监控" or "minute_macd_watch" in j.command)
    assert macd.enabled is False
    assert cm.set_enabled(macd.job_id, True) is True
    after = cm.parse_crontab(fake_crontab["content"])
    macd2 = next(j for j in after if "minute_macd_watch" in j.command)
    assert macd2.enabled is True
    assert macd2.schedule == "1,31 9-15 * * 1-5"
    # 再次停用恢复注释
    assert cm.set_enabled(macd2.job_id, False) is True
    after2 = cm.parse_crontab(fake_crontab["content"])
    assert next(j for j in after2 if "minute_macd_watch" in j.command).enabled is False
    # 幂等：重复停用返回 False（无改动）
    assert cm.set_enabled(macd2.job_id, False) is False


def test_set_enabled_preserves_others(fake_crontab):
    jobs = cm.list_jobs()
    feishu = next(j for j in jobs if "daily_feishu" in j.command)
    cm.set_enabled(feishu.job_id, False)
    content = fake_crontab["content"]
    # marketdb 行未受影响
    assert any("auto-sync" in l and not l.startswith("#") for l in content.splitlines())
    assert any("daily_feishu" in l and l.startswith("#") for l in content.splitlines())
    # env 行原样保留
    assert any(l.startswith("MYSTERY_DB_PATH=") for l in content.splitlines())


def test_add_and_remove(fake_crontab):
    j = cm.add_job("10 8 * * *", "/home/x/stock/czsc_mi/scripts/daily_report.sh", "每日报告")
    assert j.enabled and j.kind == "czsc_mi"
    content = fake_crontab["content"]
    assert "10 8 * * *" in content and "# 每日报告" in content
    assert cm.remove_job(j.job_id) is True
    assert "10 8 * * *" not in fake_crontab["content"]
    # 说明注释行一并删除
    assert "# 每日报告" not in fake_crontab["content"]


def test_add_job_validation(fake_crontab):
    with pytest.raises(ValueError):
        cm.add_job("bad schedule", "cmd")          # 4 段
    with pytest.raises(ValueError):
        cm.add_job("* * * * *", "multi\nline")     # 命令含换行
    with pytest.raises(ValueError):
        cm.add_job("", "")                          # 空
    # @daily 简写合法
    j = cm.add_job("@daily", "echo hi")
    assert j.schedule == "@daily"


def test_remove_unknown_id(fake_crontab):
    assert cm.remove_job("deadbeef0000") is False


def test_crontab_error_propagates(monkeypatch):
    def run(cmd, **kw):
        class P:
            returncode = 1
            stderr = "permission denied"
            stdout = ""
        return P()
    monkeypatch.setattr(cm.subprocess, "run", run)
    with pytest.raises(RuntimeError):
        cm.list_jobs()


def test_no_crash_on_empty_line_input(fake_crontab):
    # remove 全部条目（含 env 行）后 crontab 为空
    for j in cm.list_jobs():
        cm.remove_job(j.job_id)
    assert fake_crontab["content"].strip() == ""


# ---------------- Web 视图改造断言 ----------------

def test_watchlist_view_uses_dataframe_table():
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1].joinpath(
        "mystery/apps/web/views/stock.py").read_text(encoding="utf-8")
    assert "st.dataframe" in src
    assert '"股票名称"' in src          # 首列 = 股票名称
    assert "on_select" in src          # 行选中驱动 分析/删除
    assert '"an_' not in src           # 旧逐行按钮已移除


def test_cron_view_registered_in_app():
    import pathlib
    app = pathlib.Path(__file__).resolve().parents[1].joinpath(
        "mystery/apps/web/app.py").read_text(encoding="utf-8")
    assert "定时任务" in app
    assert "view_cron_page" in app
    cron = pathlib.Path(__file__).resolve().parents[1].joinpath(
        "mystery/apps/web/views/cron.py").read_text(encoding="utf-8")
    assert "__all__ = [\"view_cron_page\"]" in cron


def test_ctl_script_exists_and_safe():
    import pathlib
    p = pathlib.Path(__file__).resolve().parents[1].joinpath("scripts/czsc_mi_ctl.sh")
    src = p.read_text(encoding="utf-8")
    assert "/home/" not in src         # 守卫口径：无硬编码绝对路径
    for action in ("start", "stop", "restart", "status"):
        assert action in src
