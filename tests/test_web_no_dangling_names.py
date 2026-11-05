"""W66 回归测试：Web 视图模块不得存在悬空全局名。

W53 把单文件 app.py 拆成 5 个 view 模块后，同一命名空间内的隐式可见性消失，
出现过多起"顶层 import 正常、某条渲染分支才 NameError"的缺陷（W64 修了 5 处、
W66 又修了 components/scan 的 3 处）。这些分支依赖运行数据（如后台任务表只在
存在扫描任务时渲染），普通冒烟测试测不出来。

本测试用字节码级检查：反汇编每个 code object 的 LOAD_GLOBAL/LOAD_NAME，逐一比对
模块实际 __dict__ 与 builtins。比手写 AST 作用域分析可靠——LOAD_GLOBAL 只含真正
的全局名引用（无属性名噪声），且 `from x import *` 展开的名字已在模块 __dict__ 中。
"""
from __future__ import annotations

import builtins
import dis
import importlib
from pathlib import Path

import pytest

WEB_MODULES = [
    "mystery.apps.web.app",
    "mystery.apps.web.views.components",
    "mystery.apps.web.views.stock",
    "mystery.apps.web.views.scan",
    "mystery.apps.web.views.sector",
    "mystery.apps.web.views.system",
]

_BUILTINS = set(dir(builtins))


def _iter_code_objects(code, seen=None):
    if seen is None:
        seen = set()
    if id(code) in seen:
        return
    seen.add(id(code))
    yield code
    for const in code.co_consts:
        if hasattr(const, "co_code"):
            yield from _iter_code_objects(const, seen)


def _dangling_names(mod) -> list:
    """返回 [(qualname, name)]：函数/模块代码里引用但模块全局与内置都没有的名字。"""
    src = Path(mod.__file__).read_text(encoding="utf-8")
    top = compile(src, mod.__file__, "exec")
    globals_ = set(vars(mod))
    bad, seen = [], set()
    for code in _iter_code_objects(top):
        for ins in dis.get_instructions(code):
            if ins.opname not in ("LOAD_GLOBAL", "LOAD_NAME"):
                continue
            name = ins.argval
            if not isinstance(name, str):
                continue
            if name in globals_ or name in _BUILTINS:
                continue
            key = (code.co_qualname, name)
            if key in seen:
                continue
            seen.add(key)
            bad.append(key)
    return bad


@pytest.mark.parametrize("modname", WEB_MODULES)
def test_no_dangling_global_names(modname):
    """每个 Web 模块的所有 code object 都不得引用未定义的全局名。"""
    mod = importlib.import_module(modname)
    bad = _dangling_names(mod)
    assert not bad, (
        f"{modname} 存在悬空全局名（该分支一旦执行就会 NameError）：\n"
        + "\n".join(f"  {n}  <- {q}" for q, n in bad)
    )


def test_components_does_not_toplevel_import_scan():
    """components 是底层公共模块，顶层导入 scan 会形成循环导入。

    只检查模块级（tree.body）的 import 语句；函数内的延迟导入是允许的。
    """
    import ast

    from mystery.apps.web.views import components

    tree = ast.parse(Path(components.__file__).read_text(encoding="utf-8"))
    offenders = []
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        mod = node.module or ""
        # 只针对 views 层的 scan（from .scan import ... / from ..views.scan import ...）；
        # mystery.services.scan 是业务服务层，不构成循环。
        if (node.level > 0 and mod == "scan") or mod == "mystery.apps.web.views.scan":
            offenders.append(ast.unparse(node))
    assert not offenders, f"components.py 顶层不应导入 views.scan（循环导入）：{offenders}"


def test_scan_module_has_logger_and_datetime():
    """W53 拆分时丢失的模块级依赖（logger / datetime）必须存在。"""
    from mystery.apps.web.views import scan

    assert hasattr(scan, "logger"), "scan.py 缺少 logger = logging.getLogger(__name__)"
    assert hasattr(scan, "datetime"), "scan.py 缺少 from datetime import datetime"
