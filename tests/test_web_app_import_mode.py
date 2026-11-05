"""W64 回归：Web app 的脚本加载方式与拆分后跨 view 符号绑定。

事故背景：`mystery/apps/web/app.py` 由 `streamlit run ...app.py` 以**脚本方式**
加载（__package__ 为空），W53 把视图拆进 views/ 后使用了相对导入
`from .views.stock import *`，重启时抛
"attempted relative import with no known parent package"；且拆分后跨 view
函数调用（_add_watchlist_widget / _render_scan_table / _render_excel_download）
断了，扫描页/系统状态页渲染 NameError。

现有 test_web_pages_smoke.py 是 integration 且用 `import mystery.apps.web.app`
（包方式），恰好测不出这两种缺陷——故补本文件的离线契约测试。
"""
from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "mystery" / "apps" / "web" / "app.py"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_app_toplevel_imports_are_absolute():
    """app.py 以脚本方式运行，顶层不得出现相对导入（__package__ 为空会直接抛错）。"""
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    relative = [n for n in tree.body
                if isinstance(n, ast.ImportFrom) and n.level > 0]
    assert not relative, (
        "app.py 顶层出现相对导入，streamlit run 脚本方式会抛 "
        "ImportError: attempted relative import with no known parent package；"
        "请改用 mystery.apps.web.views.* 绝对导入")


def test_script_mode_imports_resolve():
    """复现 streamlit 脚本加载：__package__ 为空时逐条执行顶层 import 均可解析。"""
    pytest.importorskip("streamlit")  # CI 无 streamlit 时跳过（收集期不 import）
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    ns = {"__name__": "__main__", "__file__": str(APP), "__package__": ""}
    n = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            exec(compile(ast.Module(body=[node], type_ignores=[]),
                         str(APP), "exec"), ns)
            n += 1
    assert n >= 20, n
    # main() 依赖的视图入口符号必须由 import * 链汇聚进脚本命名空间
    for name in ("view_stock", "view_system", "view_sector",
                 "view_watchlist_subpage", "view_sector_strength",
                 "_clear_subview"):
        assert name in ns, f"脚本命名空间缺少 {name}"


def test_cross_view_symbols_bound():
    """拆分后各 view 跨模块复用的函数必须显式绑定（否则渲染时 NameError）。"""
    pytest.importorskip("streamlit")
    expected = {
        "mystery.apps.web.views.scan": ["_add_watchlist_widget"],
        "mystery.apps.web.views.sector": ["_render_scan_table"],
        "mystery.apps.web.views.system": [
            "_add_watchlist_widget", "_render_scan_table",
            "_render_excel_download"],
    }
    for mod_name, names in expected.items():
        mod = importlib.import_module(mod_name)
        for name in names:
            assert hasattr(mod, name), (
                f"{mod_name}.{name} 未绑定：拆分后跨 view 调用会 NameError，"
                "请补显式 import 或下沉到 components.py")
