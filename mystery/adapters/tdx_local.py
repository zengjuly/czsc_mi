"""mystery.adapters.tdx_local — 通达信本地数据（.day 文件，自研 struct 解析）。

迁自 tdx_incremental.py + tdx_local_client.py（指数豁免换手率/落后检查）。
行情目录优先级：TDX_HOME(/mnt/new_tdx) → TDX_VIPDOC_DIR。
换手率从 SQLite 缓存补齐（无则 None，与旧仓一致）。
"""
from __future__ import annotations

import logging
import os
import struct
import json
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

DAY_RECORD_SIZE = 32
DAY_RECORD_FMT = '<IIIIIfII'

STANDARD_COLS = ['日期', '代码', '开盘价', '最高价', '最低价', '收盘价',
                 '成交量', '成交额', '换手率', '涨跌幅']


def resolve_kline_dirs() -> List[str]:
    """行情目录优先级：TDX_HOME/vipdoc → TDX_VIPDOC_DIR（本机路径由环境变量注入）。"""
    dirs: List[str] = []
    home = os.environ.get('TDX_HOME')
    if home:
        vip = os.path.join(home, 'vipdoc')
        if os.path.isdir(vip) and _has_lday(vip):
            dirs.append(vip)
    extra = os.environ.get('TDX_VIPDOC_DIR', '')
    if extra and os.path.isdir(extra) and _has_lday(extra) and extra not in dirs:
        dirs.append(extra)
    return dirs


def _has_lday(vipdoc: str) -> bool:
    return any(os.path.isdir(os.path.join(vipdoc, m, 'lday'))
               for m in ('sh', 'sz', 'bj'))


def get_market(code6: str) -> str:
    if code6.startswith(('6', '9', '5')):
        return 'sh'
    if code6.startswith(('0', '2', '3')):
        return 'sz'
    if code6.startswith(('4', '8')):
        return 'bj'
    return 'sh'


# ---------------- 通达信自选/自定义板块（.blk，只读解析） ----------------
def resolve_blocknew_dirs() -> List[str]:
    """自选板块目录 {TDX}/T0002/blocknew（按序命中即停，只读不写通达信）。

    优先级：TDX_VIPDOC_DIR 上一级 /T0002/blocknew → TDX_BLOCKNEW_DIR →
    常见 /mnt/c/new_tdx、/mnt/new_tdx 同级 T0002/blocknew。
    """
    dirs: List[str] = []
    vip = os.environ.get('TDX_VIPDOC_DIR', '')
    if vip:
        cand = os.path.join(os.path.dirname(vip.rstrip('/')), 'T0002', 'blocknew')
        if os.path.isdir(cand):
            dirs.append(cand)
    extra = os.environ.get('TDX_BLOCKNEW_DIR', '')
    if extra and os.path.isdir(extra) and extra not in dirs:
        dirs.append(extra)
    for base in ('/mnt/c/new_tdx', '/mnt/new_tdx', '/mnt/tdx', '/mnt/tdx2'):
        cand = os.path.join(base, 'T0002', 'blocknew')
        if os.path.isdir(cand) and cand not in dirs:
            dirs.append(cand)
    return dirs


def find_blk_file(filename: str = 'zxg.blk') -> Optional[str]:
    """在 blocknew 目录序列里找第一个存在的 .blk 文件。"""
    for d in resolve_blocknew_dirs():
        fp = os.path.join(d, filename)
        if os.path.isfile(fp):
            return fp
    return None


def parse_blk_file(path: str) -> List[str]:
    """解析通达信 .blk 文件 → 内部代码列表（600519.SH / 000001.SZ / xxxxxx.BJ）。

    每行 7 位：首位 1=上海 / 0=深圳 / 2=北交，后 6 位代码；兼容 6 位纯数字
    （按 get_market 推断）。GBK / UTF-8 / latin-1 依次尝试。去重保序。
    """
    out: List[str] = []
    try:
        with open(path, 'rb') as f:
            raw = f.read()
    except OSError:
        return out
    text = None
    for enc in ('gbk', 'utf-8', 'latin-1'):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return out
    for line in text.splitlines():
        token = line.strip().strip('"').strip("'").strip()
        if not token:
            continue
        digits = ''.join(ch for ch in token if ch.isdigit())
        if len(digits) == 7 and digits[0] in ('0', '1', '2'):
            exch = {'1': 'SH', '0': 'SZ', '2': 'BJ'}[digits[0]]
            out.append(f'{digits[1:]}.{exch}')
        elif len(digits) == 6:
            out.append(f'{digits}.{get_market(digits).upper()}')
    seen: set = set()
    dedup: List[str] = []
    for c in out:
        if c not in seen:
            seen.add(c)
            dedup.append(c)
    return dedup


# ---------------- 自选板块（多 .blk 枚举） ----------------
# 板块名 → 展示名（通达信默认自选文件 zxg.blk 显示为「自选股」）。
BOARD_NAME_MAP = {"zxg": "自选股"}

# 外部代码→中文名映射（用户维护，覆盖 blocknew.cfg/文件名推导）。
# 约 116 个通达信本就缩写的板块（如 AI/0131/MI）可在此补全行业名。
# 文件不存在时自动创建空 {}，用户填入 {"代码(大写)": "中文名"} 即生效。
BOARD_OVERRIDE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "data", "board_display_overrides.json")
_BOARD_OVERRIDE_CACHE: Optional[Dict[str, str]] = None

# 板块代码 → 展示名 的缓存（key = blocknew 目录元组，避免重复读盘）。
_BLOCK_DISPLAY_CACHE = {}


def _load_board_overrides() -> Dict[str, str]:
    """读取外部板块名覆盖映射（带缓存；文件缺失则建空模板）。"""
    global _BOARD_OVERRIDE_CACHE
    if _BOARD_OVERRIDE_CACHE is not None:
        return _BOARD_OVERRIDE_CACHE
    try:
        if not os.path.exists(BOARD_OVERRIDE_PATH):
            try:
                os.makedirs(os.path.dirname(BOARD_OVERRIDE_PATH), exist_ok=True)
                with open(BOARD_OVERRIDE_PATH, "w", encoding="utf-8") as f:
                    f.write("{}\n")
            except OSError:
                pass
        with open(BOARD_OVERRIDE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        _BOARD_OVERRIDE_CACHE = {str(k).upper(): str(v)
                                 for k, v in (data or {}).items()}
    except Exception:
        _BOARD_OVERRIDE_CACHE = {}
    return _BOARD_OVERRIDE_CACHE


def reload_board_overrides() -> None:
    """清空缓存，下次读取重新加载（CLI/web 改完映射后调用）。"""
    global _BOARD_OVERRIDE_CACHE
    _BOARD_OVERRIDE_CACHE = None


def _decode_filename(name: str) -> str:
    '''尽力从 latin-1/utf-8 混合字节里还原 GBK 中文文件名（乱码回退原名）。'''
    try:
        return name.encode('latin-1').decode('gbk')
    except (UnicodeError, LookupError):
        return name


def _parse_block_display_map(dirs) -> Dict[str, str]:
    '''从 blocknew 目录解析「板块代码 → 展示名」映射。

    通达信自定义板块（blocknew）的真实展示名通常存于：
    - ``blockname.txt``：每行 ``CODE 展示名``（或仅展示名，按 .blk 顺序）；
    - ``blocknew.cfg``：以 \x00 分隔的 (展示名, 代码) 成对记录（本环境实测格式）。

    返回 {板块代码(大写): 展示名}。
    '''
    m: Dict[str, str] = {}
    for d in dirs:
        # 1) blockname.txt（部分通达信版本）
        bn = os.path.join(d, "blockname.txt")
        if os.path.isfile(bn):
            try:
                with open(bn, "r", encoding="gbk", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        parts = line.split()
                        if len(parts) >= 2:
                            m[parts[0].upper()] = parts[1]
                        else:
                            m[parts[0].upper()] = parts[0]
            except OSError:
                pass
        # 2) blocknew.cfg（\x00 分隔的 展示名/代码 对，实测为本环境格式）
        cfg = os.path.join(d, "blocknew.cfg")
        if os.path.isfile(cfg):
            try:
                raw = open(cfg, "rb").read()
                toks = [t for t in raw.decode("gbk", "replace").split("\x00")
                        if t.strip()]
                for i in range(0, len(toks) - 1, 2):
                    name, code = toks[i], toks[i + 1]
                    if code:
                        m[code.upper()] = name
            except OSError:
                pass
    return m


def _block_display_map() -> Dict[str, str]:
    '''带缓存的板块代码→展示名映射（按 blocknew 目录区分）。'''
    dirs = tuple(resolve_blocknew_dirs())
    if dirs not in _BLOCK_DISPLAY_CACHE:
        _BLOCK_DISPLAY_CACHE[dirs] = _parse_block_display_map(dirs)
    return _BLOCK_DISPLAY_CACHE[dirs]


def _board_name_of(filename: str) -> str:
    '''从 .blk 文件推导板块展示名。

    优先级：
    1. BOARD_NAME_MAP 特例（如 zxg → 自选股）；
    2. 外部覆盖映射（data/board_display_overrides.json，补全缩写板块）；
    3. blocknew.cfg / blockname.txt 中的真实展示名（如 DL-S → 电力-算）；
    4. 文件名 stem 的 GBK 还原（兜底，与通达信未命名板块一致）。
    '''
    stem = os.path.splitext(os.path.basename(filename))[0]
    if stem in BOARD_NAME_MAP:
        return BOARD_NAME_MAP[stem]
    overrides = _load_board_overrides()
    if stem.upper() in overrides:
        return overrides[stem.upper()]
    dmap = _block_display_map()
    if stem.upper() in dmap:
        return dmap[stem.upper()]
    return _decode_filename(stem)



def list_blk_files() -> List[str]:
    """枚举所有自选板块 .blk 文件（跨 blocknew 目录；同板块名仅取首个目录）。

    :return: 绝对路径列表（按目录优先级 + 文件名升序）。
    """
    out: List[str] = []
    seen_names: set = set()
    for d in resolve_blocknew_dirs():
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for fn in sorted(names):
            if not fn.lower().endswith('.blk'):
                continue
            bname = _board_name_of(fn)
            if bname in seen_names:
                continue  # 同板块名以首个命中目录为准
            seen_names.add(bname)
            out.append(os.path.join(d, fn))
    return out


class TdxLocalClient:
    """通达信本地客户端。"""

    def __init__(self, cfg: Optional[Dict] = None):
        self.cfg = cfg or {}
        self.kline_dirs = resolve_kline_dirs()
        self.vipdoc_dir = self.kline_dirs[0] if self.kline_dirs else None

    def _normalize_code(self, stock_code: str) -> str:
        code = str(stock_code).strip().lower()
        for prefix in ['sh.', 'sz.', 'bj.', 'sh', 'sz', 'bj']:
            code = code.replace(prefix, '')
        digits = ''.join(ch for ch in code if ch.isdigit())
        return digits.zfill(6)[-6:]

    def _day_file_path(self, code6: str) -> Optional[str]:
        mkt = get_market(code6)
        for d in self.kline_dirs:
            std = os.path.join(d, mkt, 'lday', f'{mkt}{code6}.day')
            if os.path.exists(std):
                return std
        return None

    def last_date_of(self, code: str) -> Optional[str]:
        """.day 文件最后交易日（用于新鲜度对比，读最后一条记录）。"""
        code6 = self._normalize_code(code)
        fp = self._day_file_path(code6)
        if not fp or os.path.getsize(fp) < DAY_RECORD_SIZE:
            return None
        try:
            with open(fp, 'rb') as f:
                f.seek(-DAY_RECORD_SIZE, 2)
                date_int, = struct.unpack('<I', f.read(4))
            s = str(date_int)
            return f'{s[:4]}-{s[4:6]}-{s[6:8]}' if len(s) == 8 else None
        except Exception:
            return None

    def _read_day_file(self, code6: str) -> pd.DataFrame:
        """读全量 .day（标准中文列；换手率 None，涨跌幅 pct_change）。"""
        fp = self._day_file_path(code6)
        if not fp:
            return pd.DataFrame()
        records = []
        try:
            with open(fp, 'rb') as f:
                while True:
                    chunk = f.read(DAY_RECORD_SIZE)
                    if not chunk or len(chunk) < DAY_RECORD_SIZE:
                        break
                    date, open_, high, low, close, amount, volume, _ = \
                        struct.unpack(DAY_RECORD_FMT, chunk)
                    date_str = str(date)
                    if date_str == '0' or date_str < '19900101':
                        continue
                    records.append({
                        '日期': f'{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}',
                        '代码': code6,
                        '开盘价': open_ / 100.0,
                        '最高价': high / 100.0,
                        '最低价': low / 100.0,
                        '收盘价': close / 100.0,
                        '成交量': volume / 100.0,   # 股 → 手
                        '成交额': amount,
                        '换手率': None,
                        '涨跌幅': None,
                    })
        except Exception as e:
            logger.error(f"❌ 读取 {fp} 失败: {e}")
            return pd.DataFrame()
        if not records:
            return pd.DataFrame()
        df = pd.DataFrame(records)
        df['日期'] = pd.to_datetime(df['日期'])
        df = df.sort_values('日期')
        df['涨跌幅'] = df['收盘价'].pct_change() * 100
        return df

    def _fill_turnover(self, df: pd.DataFrame, code6: str) -> pd.DataFrame:
        """换手率从 SQLite 缓存补齐（.day 无此字段），未覆盖最新行 ffill。"""
        if df.empty or df['换手率'].notna().any():
            return df
        try:
            from ..store.db import MysteryDB
            db = MysteryDB(db_path=self.cfg.get('db_path') or None)
            mkt = get_market(code6)
            cached = db.load_kline(f'{mkt}.{code6}', 'daily')
            if not cached.empty and 'turn' in cached.columns:
                turn_map = dict(zip(cached['date'], cached['turn']))
                df['换手率'] = df['日期'].dt.strftime('%Y-%m-%d').map(turn_map)
                df['换手率'] = df['换手率'].ffill()
        except Exception as e:
            logger.debug(f"换手率补齐失败 {code6}: {str(e)[:60]}")
        return df

    def get_daily(self, symbol: str, start: Optional[str] = None,
                  end: Optional[str] = None,
                  is_index: bool = False) -> Optional[pd.DataFrame]:
        """读本地日K（中文列，升序）。"""
        code6 = self._normalize_code(symbol)
        df = self._read_day_file(code6)
        if df is None or df.empty:
            return pd.DataFrame(columns=STANDARD_COLS)
        df = self._fill_turnover(df, code6)
        df['日期'] = df['日期'].dt.strftime('%Y-%m-%d')
        if start:
            df = df[df['日期'] >= str(start)]
        if end:
            df = df[df['日期'] <= str(end)]
        df = df.sort_values('日期').reset_index(drop=True)
        for col in STANDARD_COLS:
            if col not in df.columns:
                df[col] = None
        return df[STANDARD_COLS].copy()

    def get_index(self, code: str, start: Optional[str] = None,
                  end: Optional[str] = None) -> Optional[pd.DataFrame]:
        """指数：豁免换手率检查（.day 即权威）。"""
        return self.get_daily(code, start, end, is_index=True)

    def get_weekly(self, symbol: str, start: Optional[str] = None,
                   end: Optional[str] = None) -> Optional[pd.DataFrame]:
        return self.get_daily(symbol, start, end)
