"""守卫：运维脚本的 venv/.stockrc 路径须智能探测，不得固定假设单一布局。

O1 venv 在 $HOME/venv、.stockrc 在 $HOME/.stockrc；
O2 venv 在 $HOME/stock/venv、.stockrc 在 $HOME/stock/.stockrc。
脚本从自身位置推导 BASE_DIR/STOCK_DIR 后，必须探测两处实际存在者。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

TARGETS = [
    "minute_macd_watch.sh", "daily_feishu.sh", "daily_pipeline.sh",
    "daily_report.sh", "weekly_shares.sh", "start_web.sh",
]


def test_scripts_probe_venv_location():
    for name in TARGETS:
        src = (SCRIPTS / name).read_text(encoding="utf-8")
        assert "VENV_BASE=" in src, f"{name}: 缺少 venv 探测变量 VENV_BASE"
        assert "STOCK_DIR/venv" in src, f"{name}: 未探测 O2 的 STOCK_DIR/venv"
        assert "$VENV_BASE" in src, f"{name}: venv 引用未走探测结果"


def test_no_fixed_base_dir_venv():
    """不得再出现固定的 BASE_DIR/venv（应替换为探测后的 VENV_BASE）。"""
    import re
    for name in TARGETS:
        src = (SCRIPTS / name).read_text(encoding="utf-8")
        # 探测行内部允许出现 BASE_DIR/venv（作为候选），但引用处应走 VENV_BASE
        bad = re.findall(r'VENV[^"\n]*\$\{?BASE_DIR/venv', src)
        assert not bad, f"{name}: 仍固定引用 BASE_DIR/venv: {bad}"


def test_no_hardcoded_abs_paths():
    """脚本不得含 O1/O2 绝对路径字面量。"""
    for name in TARGETS:
        src = (SCRIPTS / name).read_text(encoding="utf-8")
        assert "/home/ai" not in src and "/home/ubuntu" not in src, f"{name}: 含绝对路径"
