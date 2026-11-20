"""守卫：仓库内代码不得硬编码本机绝对路径（AGENTS.md §0）。

覆盖 tests/ 与 scripts/ 下所有 .py / .sh / .service 源文件：
路径一律从 ``__file__`` / ``BASH_SOURCE`` 或环境变量推导，保证同一份代码
可在不同 home 目录的任意机器上运行。

文档（docs/、AGENTS.md）与配置样例（install.env.example）描述的是某台机器的
真实布局，属说明性质，不在本守卫扫描范围。
"""
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]

# 拆开拼接，避免守卫自身被扫描命中
_O1_MARK = "/home" + "/ai/"
_O2_MARK = "/home" + "/ubuntu/"
_SUFFIXES = {".py", ".sh", ".service"}


def test_no_hardcoded_home_paths_in_code():
    """tests/ 与 scripts/ 的源码中不得出现本机 home 绝对路径。"""
    hits = []
    for sub in ("tests", "scripts"):
        for src in sorted((_REPO / sub).rglob("*")):
            if not src.is_file() or src.suffix not in _SUFFIXES:
                continue
            if src.name == Path(__file__).name:  # 跳过守卫自身
                continue
            text = src.read_text(encoding="utf-8", errors="ignore")
            for lineno, line in enumerate(text.splitlines(), 1):
                if _O1_MARK in line or _O2_MARK in line:
                    hits.append(f"{src.relative_to(_REPO)}:{lineno}")
    assert not hits, "检测到硬编码绝对路径，请改为从文件位置或环境变量推导:\n" + "\n".join(hits)
