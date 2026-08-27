"""test_core_rules — P0 占位：core 不 import czsc，纯函数容器可实例化。

P1 迁入后补：给定合成 OHLC，主升浪/平台函数有确定输出。
"""
from mystery.core.mystery_rules import MysteryLogic


def test_logic_instantiable():
    logic = MysteryLogic()
    assert logic.cfg == {}


def test_core_no_czsc_import():
    """core 模块禁止 import czsc。"""
    import subprocess
    import sys

    code = "import sys; sys.modules['czsc']=None; import mystery.core.models, mystery.core.mystery_rules"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
