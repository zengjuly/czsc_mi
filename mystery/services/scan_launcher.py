"""mystery.services.scan_launcher — W88：Web 后台扫描改「独立进程」启动器。

背景
----
此前 Web「🚀 后台扫描」在 Streamlit 进程内起 daemon 线程
（``components.py::_bg_launch``）。全市场 5579 只扫描 + 三浪收尾全部塞进
Web 进程，带来三个致命问题：

1. Web 重启 / OOM / 用户刷新卡住页面 → daemon 线程直接消失，成果全废；
2. 进程池 worker 由 Web fork，几 GB 内存峰值算在 Web 头上（实测 RSS 2.3GB），
   连 frpc / sshd 都可能被内存压力拖垮；
3. 按钮默认 ``resume=False``，算完才一次性 ``_write_scan_batch``，中途死亡
   ``job_id=null / n_results=0``（11-10、11-11、11-12 三次全量扫描均如此）。

W88 把启动改为 ``systemd-run --user`` transient unit（不可用时回退裸
``Popen(start_new_session=True)``）：扫描跑在**独立 cgroup / 会话**，Web 重启
不影响它；CLI 自带 ``--resume`` 按 chunk 增量落库，个股结果边扫边存；进度与
终态由 CLI 写 ``bg_task_log`` 台账，页面只读台账渲染，跨进程可见。

本模块只负责「构造 argv + 拉起进程」，不含任何业务计算，也不写数据库；
纯函数部分可单测。按 AGENTS.md §0，**不把本机绝对路径写进业务代码**：
CLI 路径由 ``sys.executable`` 同目录推导，db 路径由调用方注入环境变量。
"""
from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

SCAN_KINDS = ("market", "watchlist")


def cli_executable() -> str:
    """定位 ``czsc-mi`` console script（与当前解释器同 venv），不硬编码路径。"""
    cand = Path(sys.executable).with_name("czsc-mi")
    if cand.is_file():
        return str(cand)
    return shutil.which("czsc-mi") or "czsc-mi"


def build_scan_argv(scan_kind: str, task_id: str, *,
                    exe: Optional[str] = None) -> List[str]:
    """构造独立扫描 CLI 参数（纯函数）。

    ``--resume`` 让 CLI 复用同 scan_type+交易日 的 working job、跳过已完成
    个股并按 chunk 增量落库；``--bg-task-id`` 让 CLI 自己写台账进度/终态；
    ``--market`` 保留 ``--limit=None`` 的全市场语义（CLI 默认防呆 100 只）。
    """
    if scan_kind not in SCAN_KINDS:
        raise ValueError(f"未知扫描类型: {scan_kind!r}，可选 {SCAN_KINDS}")
    argv = [exe or cli_executable(), "scan", "--resume",
            "--bg-task-id", task_id]
    argv.append("--market" if scan_kind == "market" else "--watchlist")
    return argv


def unit_name(task_id: str) -> str:
    """transient user unit 名（按任务 id 唯一，避免并发重名）。"""
    return f"czsc-mi-scan-{task_id}"


def log_file(task_id: str, *, base: Optional[Path] = None) -> Path:
    """扫描日志路径（按任务 id 分文件，便于事后排查）。"""
    root = base or Path(os.environ.get("XDG_STATE_HOME")
                        or (Path.home() / ".local" / "state"))
    d = root / "czsc-mi"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"scan_{task_id}.log"


def systemd_available() -> bool:
    """是否可用 ``systemd-run --user``（需 XDG_RUNTIME_DIR + 可执行文件）。"""
    return bool(os.environ.get("XDG_RUNTIME_DIR") and shutil.which("systemd-run"))


def launch(db_path: str, task_id: str, scan_kind: str, *,
           exe: Optional[str] = None,
           prefer_systemd: bool = True,
           env: Optional[Dict[str, str]] = None,
           log_base: Optional[Path] = None,
           cwd: Optional[str] = None) -> Dict[str, object]:
    """拉起独立扫描进程，返回启动信息 dict。

    优先级：``systemd-run --user --collect``（独立 cgroup，Web 重启/被 kill
    都不会带走扫描）→ 回退 ``Popen(start_new_session=True)``（脱离会话）。
    两种模式都把日志落到 ``log_file()``，并把 ``MYSTERY_DB_PATH`` 注入环境，
    保证 CLI 打到与 Web 同一个生产库。
    """
    argv = build_scan_argv(scan_kind, task_id, exe=exe)
    child_env = dict(os.environ if env is None else env)
    child_env["MYSTERY_DB_PATH"] = db_path
    lp = log_file(task_id, base=log_base)
    workdir = cwd or os.getcwd()

    if prefer_systemd and systemd_available():
        # shell 只做日志重定向；exec 让 CLI 成为 unit 主进程，可被 systemctl 管理。
        inner = f"exec {shlex.join(argv)} >> {shlex.quote(str(lp))} 2>&1"
        cmd = [shutil.which("systemd-run"), "--user",
               f"--unit={unit_name(task_id)}", "--collect",
               f"--setenv=MYSTERY_DB_PATH={db_path}",
               f"--property=WorkingDirectory={workdir}",
               "/bin/bash", "-c", inner]
        mode = "systemd-run"
        stdout = subprocess.DEVNULL
        stderr = subprocess.DEVNULL
    else:
        cmd = list(argv)
        mode = "popen"
        stdout = stderr = None  # 由下方 open() 接管

    if mode == "systemd-run":
        proc = subprocess.Popen(cmd, stdout=stdout, stderr=stderr,
                                env=child_env, stdin=subprocess.DEVNULL,
                                start_new_session=True, cwd=workdir)
    else:
        with open(lp, "ab", buffering=0) as fh:
            proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT,
                                    env=child_env, stdin=subprocess.DEVNULL,
                                    start_new_session=True, cwd=workdir)
    info: Dict[str, object] = {
        "mode": mode, "pid": proc.pid, "argv": argv,
        "unit": unit_name(task_id), "log": str(lp),
    }
    logger.info(f"[scan_launcher] 已启动独立扫描 task={task_id} "
                f"kind={scan_kind} mode={mode} argv={argv} log={lp}")
    return info
