"""基准：向量化写入 vs 旧式 iterrows+逐行 execute（P2-4 量化）。

只量化"写入机制"本身：
 - 单票 upsert_kline：NEW(向量化构建+executemany) vs OLD(iterrows 构建 + 逐行 execute)
 - 多票 upsert_kline_many：NEW(向量化构建+executemany) vs OLD(iterrows 构建 + executemany)
数据用每周一条、N 行，周期取 weekly 以排除 daily 的 prune 逻辑干扰。
"""
import time
import tempfile

import numpy as np
import pandas as pd

from mystery.store import db as dbmod
from mystery.store._repos import _helpers as H
from mystery.store._repos import kline_repo as K

N = 5000
np.random.seed(0)

# 单票 DataFrame（中文列，与 _KLINE_CN 对齐）
sdf = pd.DataFrame({
    '日期': pd.date_range('2010-01-01', periods=N, freq='W').strftime('%Y-%m-%d').tolist(),
    '开盘价': np.random.uniform(10, 20, N),
    '最高价': np.random.uniform(10, 22, N),
    '最低价': np.random.uniform(8, 20, N),
    '收盘价': np.random.uniform(10, 21, N),
    '成交量': np.random.randint(int(1e5), int(1e6), N).astype(float),
    '成交额': np.random.uniform(1e7, 1e9, N),
    '换手率': np.random.uniform(0, 0.05, N),
    '涨跌幅': np.random.uniform(-0.1, 0.1, N),
})

# 多票 DataFrame（多一列 股票代码）
codes = np.random.choice(['600000.SH', '000001.SZ', '300750.SZ', '601318.SH'], N)
mdf = sdf.copy()
mdf.insert(0, '代码', codes)

tmp = tempfile.mktemp(suffix='.db')
db = dbmod.MysteryDB(db_path=tmp)
db.upsert_kline(sdf.iloc[:2], '600000.SH', 'weekly')  # 仅用于建立表结构


def _clear():
    with db._lock:
        conn = db._connect()
        conn.execute('DELETE FROM stock_kline_data')
        conn.commit()


_SQL = K._UPSERT_KLINE_SQL


def _old_single():
    """忠实复刻改造前的单票写入：iterrows 构建 + 逐行 execute。"""
    with db._lock:
        conn = db._connect()
        for _, r in sdf.iterrows():
            conn.execute(_SQL, (
                '600000.SH', H._d(r.get('日期')), 'weekly',
                H._f(r.get('开盘价')), H._f(r.get('最高价')), H._f(r.get('最低价')),
                H._f(r.get('收盘价')), H._f(r.get('成交量')), H._f(r.get('成交额')),
                H._t(r.get('换手率')), H._f(r.get('涨跌幅')),
            ))
        conn.commit()


def _old_many():
    """忠实复刻改造前的多票写入：iterrows 构建 + executemany。"""
    data = []
    for _, r in mdf.iterrows():
        code = H._dbcode(str(r.get('代码')))
        data.append((
            code, H._d(r.get('日期')), 'weekly',
            H._f(r.get('开盘价')), H._f(r.get('最高价')), H._f(r.get('最低价')),
            H._f(r.get('收盘价')), H._f(r.get('成交量')), H._f(r.get('成交额')),
            H._t(r.get('换手率')), H._f(r.get('涨跌幅')),
        ))
    with db._lock:
        conn = db._connect()
        conn.executemany(_SQL, data)
        conn.commit()


def _count():
    with db._lock:
        conn = db._connect()
        return conn.execute('SELECT COUNT(*) FROM stock_kline_data').fetchone()[0]


# ---- 单票 ----
_clear()
t0 = time.perf_counter()
db.upsert_kline(sdf, '600000.SH', 'weekly')
t_new_s = time.perf_counter() - t0
assert _count() == N

_clear()
t0 = time.perf_counter()
_old_single()
t_old_s = time.perf_counter() - t0
assert _count() == N

# ---- 多票 ----
_clear()
t0 = time.perf_counter()
db.upsert_kline_many(mdf, 'weekly')
t_new_m = time.perf_counter() - t0
assert _count() == N

_clear()
t0 = time.perf_counter()
_old_many()
t_old_m = time.perf_counter() - t0
assert _count() == N

print(f"N={N} 行")
print(f"  单票 upsert_kline     : NEW={t_new_s*1000:7.1f}ms  OLD={t_old_s*1000:7.1f}ms  "
      f"加速={t_old_s/t_new_s:5.2f}x")
print(f"  多票 upsert_kline_many: NEW={t_new_m*1000:7.1f}ms  OLD={t_old_m*1000:7.1f}ms  "
      f"加速={t_old_m/t_new_m:5.2f}x")
