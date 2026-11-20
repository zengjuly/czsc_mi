"""mystery.services.cron_manager — 用户 crontab 定时任务管理（W108，零新依赖）。

设计（纯标准库，操作当前用户的 crontab）：
- ``list_jobs()``：解析 ``crontab -l``，返回 :class:`CronJob` 列表（含友好名与
  czsc_mi/其它 分类）。
- ``set_enabled(job_id, enabled)``：以注释/取消注释方式启停单条任务，
  **只改动目标行**，crontab 其余内容（含 env 行与其它任务）原样保留。
- ``add_job(schedule, command, comment)``：追加新任务（schedule 基本校验）。
- ``remove_job(job_id)``：删除任务行（连同其紧邻的说明注释行）。
- ``job_id`` 为整行内容 SHA1 前 12 位（行号会随编辑漂移，不可用）。

禁用行格式与 crontab 习惯一致：``# <原行>``（保留原计划表达式，可一键恢复）。
env 行（``NAME=value``）与纯注释行原样识别并保留，不作为任务条目暴露。
"""
from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass

# 已知任务 → 友好名（按命令文本匹配，顺序即优先级）
CRON_KNOWN: list[tuple[str, str]] = [
    ("minute_macd_watch.sh", "分钟 MACD 盘中监控"),
    ("daily_feishu.sh", "收盘日报（飞书）"),
    ("daily_report.sh", "每日分析报告"),
    ("daily_pipeline.sh", "每日流水线"),
    ("weekly_shares.sh", "每周自选股同步"),
    ("auto-sync", "行情库自动同步（marketdb）"),
]
# 判定为 czsc_mi 相关的关键词（ctl 脚本启停口径一致）
CZSC_MI_MARKERS: tuple[str, ...] = (
    "czsc_mi", "minute_macd_watch.sh", "daily_feishu.sh", "daily_report.sh",
    "daily_pipeline.sh", "weekly_shares.sh", "auto-sync")

# cron 计划表达式：5 段 * * * * * 或 @reboot/@daily 等简写
_SCHED_RE = re.compile(r"^(?P<sch>@(?:reboot|yearly|annually|monthly|weekly|daily|midnight|hourly)"
                       r"|[\d*,-/]+\s+[\d*,-/]+\s+[\d*,-/]+\s+[\d*,-/]+\s+[\d*,-/]+)\s+(?P<cmd>.+)$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


@dataclass
class CronJob:
    """一条 crontab 任务（或 env/注释行的只读快照）。"""
    job_id: str        # 整行内容 sha1[:12]
    line: str          # 原始行（禁用时带 "# " 前缀）
    schedule: str      # 计划表达式（env 行为 ""）
    command: str       # 命令（禁用时为去掉 "# " 后的命令）
    label: str         # 友好名（未识别则 "自定义任务"）
    enabled: bool
    kind: str          # "czsc_mi" | "other" | "env"


def _label_of(command: str) -> str:
    for marker, label in CRON_KNOWN:
        if marker in command:
            return label
    return "自定义任务"


def _kind_of(command: str, line: str) -> str:
    if _ENV_RE.match(line):
        return "env"
    if any(m in command for m in CZSC_MI_MARKERS) or "czsc_mi" in line:
        return "czsc_mi"
    return "other"


def _entry_id(line: str) -> str:
    return hashlib.sha1(line.encode("utf-8")).hexdigest()[:12]


def _run_crontab_list() -> str:
    """读当前 crontab；无 crontab（exit 127/1）返回空串。"""
    try:
        p = subprocess.run(["crontab", "-l"], capture_output=True,
                           text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"crontab 读取失败: {e}") from e
    if p.returncode != 0:
        # "no crontab for <user>" 属正常空态
        if "no crontab" in (p.stderr or "").lower():
            return ""
        raise RuntimeError(f"crontab 读取失败: {(p.stderr or '').strip()}")
    return p.stdout or ""


def _write_crontab(content: str) -> None:
    p = subprocess.run(["crontab", "-"], input=content,
                       capture_output=True, text=True, timeout=10)
    if p.returncode != 0:
        raise RuntimeError(f"crontab 写入失败: {(p.stderr or '').strip()}")


def parse_crontab(content: str) -> list[CronJob]:
    """解析 crontab 文本为任务列表（无副作用，供测试直接调用）。"""
    jobs: list[CronJob] = []
    for raw in content.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if _ENV_RE.match(line):
            jobs.append(CronJob(job_id=_entry_id(line), line=line, schedule="",
                                command=line, label="(env)", enabled=True,
                                kind="env"))
            continue
        disabled = False
        probe = line
        if line.lstrip().startswith("#"):
            candidate = line.lstrip()[1:].lstrip()
            if _SCHED_RE.match(candidate):
                disabled, probe = True, candidate
            else:
                continue  # 纯注释行：不作为条目暴露
        m = _SCHED_RE.match(probe)
        if not m:
            continue  # 无法识别的行：跳过，保持不破坏
        sch, cmd = m.group("sch"), m.group("cmd")
        jobs.append(CronJob(
            job_id=_entry_id(line), line=line, schedule=sch, command=cmd,
            label=_label_of(cmd), enabled=not disabled, kind=_kind_of(cmd, line)))
    return jobs


def list_jobs() -> list[CronJob]:
    """当前用户 crontab 的任务列表。"""
    return parse_crontab(_run_crontab_list())


def _rewrite(jobs_by_line: list[str]) -> None:
    _write_crontab("\n".join(jobs_by_line) + ("\n" if jobs_by_line else ""))


def set_enabled(job_id: str, enabled: bool) -> bool:
    """启停任务：注释/取消注释目标行。返回是否发生改动。"""
    content = _run_crontab_list()
    lines = content.splitlines()
    changed = False
    for i, line in enumerate(lines):
        if not line.strip() or _entry_id(line) != job_id:
            continue
        stripped = line.lstrip()
        if enabled and stripped.startswith("#"):
            lines[i] = stripped[1:].lstrip()
            changed = True
        elif not enabled and not stripped.startswith("#"):
            lines[i] = f"# {line}"
            changed = True
        if changed:
            break
    if changed:
        _rewrite(lines)
    return changed


def add_job(schedule: str, command: str, comment: str = "") -> CronJob:
    """追加一条任务。schedule 为 5 段表达式或 @daily 简写；写前做基本校验。"""
    schedule = (schedule or "").strip()
    command = (command or "").strip()
    if not schedule or not command:
        raise ValueError("schedule 与 command 均不能为空")
    if not re.match(r"^@(?:reboot|yearly|annually|monthly|weekly|daily|midnight|hourly)$", schedule) \
            and len(schedule.split()) != 5:
        raise ValueError("schedule 需为 5 段 cron 表达式或 @daily 等简写")
    if "\n" in command or "\r" in command:
        raise ValueError("command 不能包含换行")
    lines = _run_crontab_list().splitlines()
    new_line = f"{schedule} {command}"
    if comment:
        lines.append(f"# {comment.strip()}")
    lines.append(new_line)
    _rewrite(lines)
    return CronJob(job_id=_entry_id(new_line), line=new_line,
                   schedule=schedule, command=command,
                   label=_label_of(command), enabled=True,
                   kind=_kind_of(command, new_line))


def remove_job(job_id: str) -> bool:
    """删除任务行；若其上一行是紧邻的说明注释行则一并删除。返回是否改动。"""
    content = _run_crontab_list()
    lines = content.splitlines()
    for i, line in enumerate(lines):
        if not line.strip() or _entry_id(line) != job_id:
            continue
        del lines[i]
        # 任务行紧邻的上一行若是注释说明行，一并删除（幂等、不影响更早注释）
        if i > 0 and lines[i - 1].lstrip().startswith("#"):
            del lines[i - 1]
        _rewrite(lines)
        return True
    return False
