"""mystery.services.streaming_scan — W89 阶段化流式全市场扫描（内存有界）。

背景
----
11-10 / 11-11 / 11-12 三次全量扫描都卡死在「收尾·三浪主升评估」。
W88 把 K 线改成流式释放，消除了 GB 级爆点，但架构仍是「全量结果驻留内存
→ 算完才落库」：``scan.py`` 的 ``results`` 全量 list、``wave3_scan.py`` 的
``assessments`` / ``row_by_sym`` / ``assigns`` 全量、``_resume_results`` 把整个
job 读回内存再全量 UPDATE、线程池回退分支一次性落库、指数在个股之后并入、
没有独立板块阶段。

真正把「逐票落盘」和「全池合成」绑死的是 ``wave3_portfolio``（组合总风险）
与 ``scale_down``：它们是**跨全部候选**算出的全局耦合字段，却被嵌进了
**每一条**结果的 ``payload_json``。于是「写第 10 条」必须等「全池候选风险
算完」，才逼出了全量驻留。

本模块的解法
------------
按用户要求的顺序分四阶段，每阶段内 **每 ``batch``（默认 10）只一批落盘**：

1. **大盘**：各大宽基指数完整研判 → 落库（沿用 W84 ``is_index`` 口径）+ 分片。
2. **板块**：关注板块完整研判（价格/形态/强度/吸筹/相关个股）→ 分片文件
   （不进 ``scan_results``，避免污染个股结果表与 Web/日报渲染）。
3. **个股**：逐票完成**全部个股级分析**（评分、信号、K 线组合形态、主力吸筹、
   三浪 ``evaluate`` 与单票仓位），每 10 只一批 ``INSERT OR REPLACE`` + ``COMMIT``
   （幂等、崩溃最多丢当前批、可断点续跑），同时 flush + fsync 到 JSONL 分片。
   批对象落盘后立即释放，内存里不再累积完整结果。
4. **组合层**：内存只保留 KB 级 assessment（不含 K 线）与候选收益序列，调同一个
   ``wave3.evaluate_portfolio`` 算全局 ``scale_down`` / 组合摘要，再用 SQLite
   ``json_set`` **在 SQL 层**回填到已落盘的行，不把 payload 读回 Python。

内存上界：O(batch × 单票完整 dict + 单票 K 线) + O(池大小 × KB 级 assessment
约 1~2MB，组合层数学上必需的最小跨票状态)。与「5579 只完整 K 线（数 GB）」
彻底解耦。落盘完成后 CLI 才读回轻量结果（无 K 线）供打印/日报，属展示契约。

所有阶段异常硬隔离：单票取数/评估失败只标「回避」继续；板块/指数阶段失败
只记 warning；分片写入失败不影响 DB 落库。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..adapters.codes import normalize_symbol
from ..core import wave3
from . import scan as _scan
from . import wave3_scan as _w3s
from .runtime_settings import scan_wave3_options_effective

logger = logging.getLogger(__name__)

STREAM_ON_ENV = "MYSTERY_SCAN_STREAMING"
STREAM_BATCH_ENV = "MYSTERY_SCAN_STREAM_BATCH"
STREAM_MIN_ENV = "MYSTERY_SCAN_STREAM_MIN"
STREAM_SHARD_ENV = "MYSTERY_SCAN_STREAM_SHARD"
STREAM_BATCH_DEFAULT = 10
STREAM_MIN_DEFAULT = 200       # 小于该池大小仍走旧路径，保证小池/自选零回归
STAGE_KEEP = 3                 # 分片文件只保留最近 3 次 job


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _stream_on() -> bool:
    return os.environ.get(STREAM_ON_ENV, "1").strip().lower() not in (
        "0", "false", "no", "off")


def streaming_enabled(*, scan_type: str, watchlist, universe,
                      no_persist: bool, n_codes: int) -> bool:
    """是否走 W89 流式流水线。

    仅对「全市场 + 持久化 + 大池 + 三浪全量口径」启用；其余（自选、板块、
    前台 limit 小池、no_persist 调试、显式三浪上限/自动限流）全部保持旧路径，
    既有测试与行为零回归。``MYSTERY_SCAN_STREAMING=0`` 可一键回退。
    """
    if not _stream_on() or no_persist or n_codes <= 0:
        return False
    if watchlist or universe is not None:
        return False
    # W94/D：历史回放（--as-of）用独立 scan_type='replay'，同样必须走
    # 流式分批落盘，否则全市场 5579 只堆内存有 OOM 风险（用户明确要求
    # 「无论如何都不应该 OOM」）。replay 台账与 market 互相独立、互不清理。
    if scan_type not in ("market", "full", "replay"):
        return False
    if n_codes < _env_int(STREAM_MIN_ENV, STREAM_MIN_DEFAULT):
        return False
    # 三浪限流（按分降序取前 N）无法在流式逐票阶段判定，需回退旧路径；
    # 线上显式 MAX=0（全量）与三浪关闭都不限流，可安全流式。
    try:
        eff = scan_wave3_options_effective()
    except Exception:
        eff = {}
    raw = str(eff.get("MYSTERY_SCAN_WAVE3_MAX") or "").strip()
    if raw and raw != "0":
        return False
    if not raw:
        try:
            trigger = int(str(eff.get("MYSTERY_SCAN_WAVE3_AUTO_TRIGGER") or 500))
            auto_max = int(str(eff.get("MYSTERY_SCAN_WAVE3_AUTO_MAX") or 200))
        except (TypeError, ValueError):
            trigger, auto_max = 500, 200
        if n_codes > trigger and 0 < auto_max < n_codes:
            return False
    return True


def _open_fresh_job(db, scan_type: str, trade_date: str) -> Tuple[int, set]:
    """非 resume 模式：新建空 working job（行为与 _write_scan_batch 的 INSERT 一致）。"""
    conn = db._connect()
    try:
        now = datetime.now().isoformat(timespec="seconds")
        cur = conn.execute(
            "INSERT INTO scan_jobs (trade_date, started_at, finished_at, "
            "n_ok, n_fail, scan_type) VALUES (?,?,?,?,?,?)",
            (trade_date, now, now, 0, 0, scan_type))
        conn.commit()
        return int(cur.lastrowid), set()
    finally:
        conn.close()


def _prune_old_jobs(db, scan_type: str, job_id: int) -> None:
    """与 _write_scan_batch 同口径清理老 job（W77 保留最近 N 份）。"""
    conn = db._connect()
    try:
        keep_older = _scan._scan_job_keep() - 1
        conn.execute(
            "DELETE FROM scan_results WHERE job_id IN ("
            " SELECT id FROM scan_jobs WHERE scan_type=? AND id<>? "
            " ORDER BY id DESC LIMIT -1 OFFSET ?)",
            (scan_type, job_id, keep_older))
        conn.execute(
            "DELETE FROM scan_jobs WHERE id IN ("
            " SELECT id FROM scan_jobs WHERE scan_type=? AND id<>? "
            " ORDER BY id DESC LIMIT -1 OFFSET ?)",
            (scan_type, job_id, keep_older))
        conn.commit()
    finally:
        conn.close()


class _Shards:
    """JSONL 分片写入器：每批一个文件，flush + fsync 后才算落盘。"""

    def __init__(self, db, job_id: int):
        self.dir: Optional[Path] = None
        self.enabled = os.environ.get(STREAM_SHARD_ENV, "1").strip().lower() \
            not in ("0", "false", "no", "off")
        if not self.enabled:
            return
        try:
            override = os.environ.get("MYSTERY_SCAN_SHARD_DIR", "").strip()
            base = Path(override) if override else \
                Path(str(db.db_path)).parent / "scan_shards"
            self.dir = base / str(job_id)
            self.dir.mkdir(parents=True, exist_ok=True)
            self._prune(base, self.dir.name)
        except Exception as e:  # noqa: BLE001 分片是辅助，失败不影响 DB 落库
            logger.warning("[W89] 分片目录创建失败，仅落库: %s", str(e)[:120])
            self.dir = None

    @staticmethod
    def _prune(base: Path, keep_name: str) -> None:
        try:
            dirs = sorted((p for p in base.iterdir() if p.is_dir()),
                          key=lambda p: p.stat().st_mtime, reverse=True)
            for p in [d for d in dirs if d.name != keep_name][STAGE_KEEP - 1:]:
                for f in p.glob("*.jsonl"):
                    try:
                        f.unlink()
                    except OSError:
                        pass
                try:
                    p.rmdir()
                except OSError:
                    pass
        except Exception:  # noqa: BLE001
            pass

    def write(self, prefix: str, seq: int, rows: List[Dict[str, Any]]) -> None:
        if self.dir is None or not rows:
            return
        path = self.dir / f"{prefix}_{seq:05d}.jsonl"
        try:
            with path.open("w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except Exception as e:  # noqa: BLE001
            logger.warning("[W89] 分片写入失败（不影响落库）: %s", str(e)[:120])


class StreamingScan:
    """阶段化流式扫描编排器；由 ``scan_market`` 在大池场景下调用。"""

    def __init__(self, *, svc, codes: List[str], scan_type: str, cfg,
                 meta, progress_cb, include_detail: bool, min_score,
                 resume: bool, job_holder, no_persist: bool = False):
        self.svc = svc
        self.db = svc.market.db
        self.client = svc.market
        self.codes = list(codes)
        self.scan_type = scan_type
        self.cfg = cfg
        self.meta = meta
        self.cb = progress_cb
        self.include_detail = include_detail
        self.min_score = min_score
        self.resume = resume
        self.job_holder = job_holder
        self.batch = _env_int(STREAM_BATCH_ENV, STREAM_BATCH_DEFAULT)

        self.job_id = 0
        self.n_ok = 0
        self.n_fail = 0
        self.n_batches = 0
        self.shards: Optional[_Shards] = None

        # 三浪开关与口径（与 attach_wave3_to_results 完全同源）
        self.eq = _w3s._env_float("MYSTERY_WAVE3_EQUITY", _w3s.DEFAULT_EQUITY)
        self.rp = _w3s._env_float("MYSTERY_WAVE3_RISK_PCT", _w3s.DEFAULT_RISK_PCT)
        self.min_bars = int(wave3.DEFAULTS["MIN_BARS"])
        self.strict = False
        self.w3_on = True
        try:
            eff = scan_wave3_options_effective()
            self.w3_on = str(eff.get("MYSTERY_SCAN_WAVE3", "1")).strip() not in (
                "0", "false", "False", "no", "off", "")
            self.strict = str(eff.get("MYSTERY_SCAN_WAVE3_STRICT", "0")).strip() in (
                "1", "true", "True", "yes")
        except Exception:  # noqa: BLE001 配置读取失败按默认全量处理
            pass

        # 组合层唯一保留的跨票状态：KB 级 assessment + 候选收益序列
        self.assessments: List[Dict[str, Any]] = []
        self.cand_returns: Dict[str, Any] = {}
        self.status: Dict[str, str] = {}
        self.n_loaded = 0
        # 归一 symbol → 落库原始 symbol：assessment 用归一码，scan_results
        # 主键用原始码，阶段④ SQL 回填需按原始码定位（W88 是内存引用赋值，
        # 不涉及 SQL；流式落盘后必须显式映射，否则回填行匹配不上）。
        self.raw_by_norm: Dict[str, str] = {}

    # ---------- 基础设施 ----------

    def _report(self, done: int, total: int, stage: str = "") -> None:
        if stage:
            # CLI 直跑时 logging 未必配到文件，阶段标记同时写 stderr，保证
            # 「大盘→板块→个股→组合」的顺序在日志里可追溯（不污染 stdout）。
            try:
                print(f"[W89] {stage}", file=sys.stderr, flush=True)
            except Exception:  # noqa: BLE001
                pass
        if self.cb is None:
            return
        try:
            if stage:
                self.cb(done, total, stage)
            else:
                self.cb(done, total)
        except TypeError:
            try:
                self.cb(done, total)
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001 心跳失败不得影响扫描
            pass

    def _flush(self, rows: List[Dict[str, Any]], failed_inc: int,
               prefix: str, seq: int) -> None:
        """一批落盘：DB 幂等写入（COMMIT）+ JSONL 分片 fsync。"""
        if not rows and not failed_inc:
            return
        if rows:
            _scan._append_scan_results(self.db, self.job_id, rows, failed_inc)
            self.n_ok += len(rows)
        elif failed_inc:
            conn = self.db._connect()
            try:
                conn.execute(
                    "UPDATE scan_jobs SET n_fail = COALESCE(n_fail,0) + ? "
                    "WHERE id=?", (failed_inc, self.job_id))
                conn.commit()
            finally:
                conn.close()
        self.n_fail += failed_inc
        if self.shards is not None:
            self.shards.write(prefix, seq, rows)
        self.n_batches += 1

    # ---------- 阶段 ① 大盘 ----------

    def stage_indexes(self) -> None:
        """各大宽基指数完整研判，先于个股落盘（W84 口径，is_index=True）。"""
        from .index_analysis import MAJOR_INDEXES, analyze_index
        self._report(0, len(MAJOR_INDEXES), "① 大盘指数")
        buf: List[Dict[str, Any]] = []
        for k, (name, code) in enumerate(MAJOR_INDEXES, 1):
            try:
                d = analyze_index(self.client, name, code)
                buf.append(_scan._index_result(d))
            except Exception as e:  # noqa: BLE001 单指数失败不阻塞
                logger.warning("[W89] 指数 %s 分析失败: %s", name, str(e)[:100])
            if len(buf) >= self.batch:
                self._flush(buf, 0, "01_index", self.n_batches + 1)
                buf = []
        if buf:
            self._flush(buf, 0, "01_index", self.n_batches + 1)
        logger.info("[W89] 阶段① 大盘指数完成：%d 只", len(MAJOR_INDEXES))

    # ---------- 阶段 ② 板块 ----------

    def stage_sectors(self) -> None:
        """关注板块完整研判（板块自身价格/形态/强度/吸筹/相关个股）。

        板块结果写分片文件而非 ``scan_results``：既有 Web 结果表与日报按
        ``is_index`` 区分指数、其余均为个股，混入板块行会污染渲染；日报的
        板块区块本就由 ``analyze_focus_sectors`` 独立生成，不依赖本表。
        """
        from . import runtime_settings as rts
        from .sector_analysis import analyze_focus_sectors
        try:
            opts = rts.sector_options_normalized()
            sectors = analyze_focus_sectors(self.db, opts=opts, results=None)
        except Exception as e:  # noqa: BLE001 板块分析失败不阻塞个股
            logger.warning("[W89] 阶段② 板块分析跳过: %s", str(e)[:120])
            sectors = []
        self._report(0, max(1, len(sectors)), "② 板块分析")
        for i in range(0, len(sectors), self.batch):
            part = sectors[i:i + self.batch]
            if self.shards is not None:
                self.shards.write("02_sector", i // self.batch + 1, part)
        logger.info("[W89] 阶段② 板块完成：%d 个（分片文件，不进结果表）",
                    len(sectors))

    # ---------- 阶段 ③ 个股 ----------

    def _wave3_one(self, d: Dict[str, Any]):
        """单票完整三浪评估（复用 wave3_scan 的取数/评估/压缩函数，口径同源）。

        返回 ``(compact, assessment, returns, sym)``；assessment 为 KB 级
        （不含 K 线），供阶段④组合层使用。K 线在 finally 里逐票释放。
        """
        raw = str(d.get("symbol") or "")
        try:
            sym = normalize_symbol(raw)
        except Exception:  # noqa: BLE001
            sym = raw
        daily = daily_rec = weekly = monthly = None
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                daily = _w3s._daily_from_db(self.db, sym)
            if daily is None or not daily.bars:
                return ({"verdict": "回避", "reason": "无日K数据",
                         "data_ok": False}, None, None, sym)
            daily_rec = _w3s._records(daily)
            if len(daily_rec) < self.min_bars:
                return ({"verdict": "回避",
                         "reason": f"日K仅{len(daily_rec)}根，不足{self.min_bars}根",
                         "data_ok": False}, None, None, sym)
            weekly = self.client.resample_bars(daily, "1w")
            monthly = self.client.resample_bars(daily, "1M")
            a = wave3.evaluate(
                daily_rec, _w3s._records(weekly), _w3s._records(monthly),
                equity=self.eq, risk_pct=self.rp, symbol=sym,
                name=str(d.get("name") or ""), strict=self.strict)
            ret = _w3s._returns(daily_rec) if a["verdict"] == "候选" else None
            return _w3s._wave3_compact(a), a, ret, sym
        except Exception as e:  # noqa: BLE001 单票失败标回避，不中断整轮
            return ({"verdict": "回避", "reason": str(e)[:80],
                     "data_ok": False}, None, None, sym)
        finally:
            daily = daily_rec = weekly = monthly = None

    def _finish_batch(self, part: List[Dict[str, Any]], part_failed: int) -> None:
        """逐票补齐个股级全部分析 → 按 batch 分批落盘 → 释放批对象。

        进程池 chunk 可以大于 batch（并行效率），但**落盘严格按 batch 一只
        一批**；每批 ``_flush`` 后立即清空，内存里任意时刻最多 batch 个完整
        dict + 单票 K 线，与股票池大小解耦。
        """
        for d in part:
            if self.w3_on:
                compact, a, ret, sym = self._wave3_one(d)
                if a is not None:
                    # 批内落盘即给出**个股级完整**三浪结论（含仓位建议）：
                    # 单票 evaluate 的 sizing 用 shares/lots/risk_amount，而
                    # _wave3_compact 读的是 suggested_*（组合层才写），此处补齐
                    # 别名，保证即使进程在阶段④之前中断，已落盘的每只票也不是
                    # 半截记录。阶段④会用组合层重算值整体覆盖，最终口径不变。
                    sz = a.get("sizing") or {}
                    for ck, sk in (("lots", "lots"), ("shares", "shares"),
                                   ("risk_amount", "risk_amount"),
                                   ("scale_down", "scale_down")):
                        if compact.get(ck) is None and sz.get(sk) is not None:
                            compact[ck] = sz[sk]
                d["wave3"] = compact
                raw = str(d.get("symbol") or "")
                self.raw_by_norm[sym] = raw
                if a is not None:
                    self.assessments.append(a)
                    self.n_loaded += 1
                    if ret is not None:
                        self.cand_returns[sym] = ret
                else:
                    self.status[sym] = str(compact.get("reason") or "")[:80]
        n = len(part)
        for i in range(0, n, self.batch):
            sub = part[i:i + self.batch]
            self._flush(sub, part_failed if i == 0 else 0,
                        "03_stock", self.n_batches + 1)
            # 切片是新 list，part.clear() 清不到它 → 逐批显式断开引用
            sub.clear()
        if not n and part_failed:
            self._flush([], part_failed, "03_stock", self.n_batches + 1)
        # 显式断开批引用：CPython 引用计数立即释放本批完整结果 dict
        part.clear()

    def stage_stocks(self, codes: List[str]) -> None:
        total = len(codes)
        self._report(0, total, "③ 个股全量扫描")
        workers = max(1, int(os.environ.get("MYSTERY_SCAN_WORKERS")
                             or (self.cfg or {}).get("scan", {}).get("workers")
                             or 4))
        done = 0
        use_pool = workers > 1 and total >= 16
        if use_pool:
            import multiprocessing as mp
            from functools import partial
            try:
                ctx = mp.get_context("fork") if "fork" in mp.get_all_start_methods() \
                    else mp.get_context()
                # chunk 取 16（沿用 W15 实测最优并行粒度），落盘仍按 batch=10
                chunk = max(16, self.batch)
                chunks = [codes[i:i + chunk]
                          for i in range(0, len(codes), chunk)]
                with ctx.Pool(processes=workers) as pool:
                    for part, part_failed, part_filtered in pool.imap_unordered(
                            partial(_scan._scan_worker, cfg=self.cfg,
                                    include_detail=self.include_detail,
                                    min_score=self.min_score),
                            chunks, chunksize=1):
                        done += len(part) + part_failed + part_filtered
                        self._finish_batch(part, part_failed)
                        self._report(done, total)
                logger.info("[W89] 阶段③ 个股完成 %d 只（失败 %d，%d 批，"
                            "processes=%d）", self.n_ok, self.n_fail,
                            self.n_batches, workers)
                return
            except Exception as e:  # noqa: BLE001 进程池不可用回退线程池
                logger.warning("[W89] 进程池不可用，回退线程池: %s", str(e)[:100])

        # 线程池回退 / workers==1：共享 svc，仍按 batch 分批落盘
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from ..adapters.codes import db_code_of
        from .analyze import MarketContextCache
        norm = lambda c: db_code_of(c) if not c.startswith(("sh.", "sz.", "bj.")) else c
        db_codes = [norm(c) for c in codes]
        ctx_cache = MarketContextCache(
            industries=self.db.get_primary_industries(db_codes),
            financials=self.db.get_financials(db_codes))
        buf: List[Dict[str, Any]] = []
        buf_failed = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_scan._scan_thread_worker, c, self.svc,
                              self.include_detail, self.min_score, ctx_cache)
                    for c in codes]
            for fut in as_completed(futs):
                done += 1
                d, is_fail = fut.result()
                if is_fail:
                    buf_failed += 1
                elif d is not None:
                    buf.append(d)
                if len(buf) >= self.batch:
                    self._finish_batch(buf, buf_failed)
                    buf, buf_failed = [], 0
                self._report(done, total)
        if buf or buf_failed:
            self._finish_batch(buf, buf_failed)
        logger.info("[W89] 阶段③ 个股完成 %d 只（失败 %d，%d 批，threads=%d）",
                    self.n_ok, self.n_fail, self.n_batches, workers)

    # ---------- 阶段 ④ 组合层 ----------

    def stage_portfolio(self) -> None:
        """用 KB 级 assessment 合成组合层，SQL 层 json_set 回填，不读回 payload。"""
        if not self.w3_on or not self.assessments:
            return
        self._report(self.n_loaded, max(1, self.n_loaded), "④ 组合风险合成")
        try:
            res = wave3.evaluate_portfolio(
                evaluated=self.assessments, equity=self.eq, risk_pct=self.rp,
                returns_by_symbol=self.cand_returns, strict=self.strict)
        except Exception as e:  # noqa: BLE001 组合层失败不污染已落盘的个股结论
            logger.warning("[W89] 组合层合成跳过（个股结论已落盘）: %s",
                           str(e)[:120])
            return
        summary = _w3s._wave3_portfolio_summary(res)
        summary.update({
            "enabled": True, "strict": self.strict,
            "requested": len(self.codes), "loaded": self.n_loaded,
            "failed": len(self.status), "auto_limited": False,
            "cap_reason": "none", "cap": None, "pool_size": len(self.codes),
        })
        # assessment.symbol 是归一码，scan_results 主键是原始码 → 显式映射
        final: Dict[str, Any] = {}
        for a in res.get("results", []):
            norm = a.get("symbol", "")
            final[self.raw_by_norm.get(norm, norm)] = _w3s._wave3_compact(a)
        try:
            conn = self.db._connect()
            try:
                # ① 逐票最终 compact（含 scale_down 后的仓位）：只传 KB 级参数，
                #    SQLite 在 SQL 层做 json_set，Python 全程不持有完整 payload。
                if final:
                    conn.executemany(
                        "UPDATE scan_results SET payload_json = "
                        "json_set(payload_json, '$.wave3', json(?)) "
                        "WHERE job_id=? AND symbol=?",
                        [(json.dumps(c, ensure_ascii=False), self.job_id, sym)
                         for sym, c in final.items()])
                # ② 组合摘要挂到每条个股行（指数行无 wave3，自动排除），
                #    与 attach_wave3_to_results 的「任取一行可读总风险」同口径。
                conn.execute(
                    "UPDATE scan_results SET payload_json = "
                    "json_set(payload_json, '$.wave3_portfolio', json(?)) "
                    "WHERE job_id=? AND json_type(payload_json,'$.wave3') IS NOT NULL",
                    (json.dumps(summary, ensure_ascii=False), self.job_id))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001 回填失败不影响已落库个股结论
            logger.warning("[W89] 组合层回填跳过（个股结论已落盘）: %s",
                           str(e)[:120])
        if self.shards is not None:
            self.shards.write("04_portfolio", 1, [summary])
        logger.info("[W89] 阶段④ 组合层完成：评估 %d / 候选 %d / 缩放 ×%s",
                    self.n_loaded, len(self.cand_returns),
                    summary.get("scale_down"))

    # ---------- 入口 ----------

    def run(self) -> List[Dict[str, Any]]:
        from ..adapters.calendar import get_latest_trade_date
        # W94/D：回放以 as_of 为 job 的 trade_date（历史截面），不用今天；
        # 这样台账里能一眼区分「哪一天的回放」，且不占用当日正式 job。
        _as_of = getattr(self.client, "as_of", "") or ""
        if _as_of:
            td = _as_of
        else:
            try:
                td = get_latest_trade_date() or ""
            except Exception:  # noqa: BLE001
                td = ""
        if self.resume:
            self.job_id, done = _scan._open_resume_job(
                self.db, self.scan_type, td)
            if done:
                self.codes = [c for c in self.codes if c not in done]
        else:
            self.job_id, _ = _open_fresh_job(self.db, self.scan_type, td)
        if self.job_holder is not None:
            try:
                self.job_holder.append(self.job_id)
            except Exception:  # noqa: BLE001
                pass
        self.shards = _Shards(self.db, self.job_id)
        logger.info("[W89] 流式扫描启动 job=%s batch=%d 池=%d 阶段=大盘→板块→个股→组合",
                    self.job_id, self.batch, len(self.codes))

        if self.codes:
            self.stage_indexes()
            self.stage_sectors()
            self.stage_stocks(self.codes)
            self.stage_portfolio()

        _scan._finalize_resume_job(self.db, self.job_id, meta=self.meta)
        if not self.resume:
            try:
                _prune_old_jobs(self.db, self.scan_type, self.job_id)
            except Exception as e:  # noqa: BLE001 清理失败不影响本次结果
                logger.warning("[W89] 老 job 清理跳过: %s", str(e)[:100])
        logger.info("[W89] 流式扫描完成 job=%s n_ok=%d n_fail=%d batches=%d",
                    self.job_id, self.n_ok, self.n_fail, self.n_batches)
        # 展示契约：落盘完成后读回轻量结果（无 K 线）供 CLI 打印 / 日报 / Web。
        # 这是扫描结束后的只读展示，不是执行期数据；Web 页面直接读库表+台账。
        rows = _scan.scan_results_of(self.job_id, cfg=self.cfg)
        # 与旧路径 _scan_market_impl 的 results.sort + 指数末尾 append 同口径：
        # 按分降序、score=None 的指数排在末尾，避免进程池 imap_unordered 乱序
        # 打乱 CLI 逐票打印与日报排序。
        rows.sort(key=lambda x: (x.get("score") is not None,
                                 float(x.get("score") or -1)), reverse=True)
        return rows


def run_streaming_scan(*, svc, codes: List[str], scan_type: str, cfg,
                       meta, progress_cb, include_detail: bool, min_score,
                       resume: bool, job_holder) -> List[Dict[str, Any]]:
    """``scan_market`` 在大池场景下的流式入口。"""
    return StreamingScan(
        svc=svc, codes=codes, scan_type=scan_type, cfg=cfg, meta=meta,
        progress_cb=progress_cb, include_detail=include_detail,
        min_score=min_score, resume=resume, job_holder=job_holder).run()


__all__ = ["streaming_enabled", "run_streaming_scan", "StreamingScan"]
