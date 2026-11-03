"""mystery.store.db - SQLite 数据中枢（schema 复用现网 mystery_cache.db）。

拆分说明：MysteryDB 的读写方法已按职责抽到 mystery.store._repos 的各 RepoMixin
（kline / share / scan / sector / analysis / meta + base），本类仅做多重继承聚合，
公开方法签名与行为完全不变，调用方 `MysteryDB().xxx()` 无需改动。
"""
from __future__ import annotations
import logging
import os
import sqlite3
import threading
from typing import Any, Dict, List, Optional
import pandas as pd
from ._repos._helpers import *  # 保留 to_cn_columns 等公开符号
from ._repos.kline_repo import KlineRepoMixin
from ._repos.share_repo import ShareRepoMixin
from ._repos.scan_repo import ScanRepoMixin
from ._repos.sector_repo import SectorRepoMixin
from ._repos.analysis_repo import AnalysisRepoMixin
from ._repos.meta_repo import MetaRepoMixin

class MysteryDB(KlineRepoMixin, ShareRepoMixin, ScanRepoMixin, SectorRepoMixin,
                AnalysisRepoMixin, MetaRepoMixin):
    """本地库客户端（读为主，写带锁）。"""
