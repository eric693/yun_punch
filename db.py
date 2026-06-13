"""PostgreSQL 連線池與連線取得。

優先使用 psycopg_pool 連線池;若不可用或初始化失敗,退回每次直接連線。
其他模組一律 `from db import get_db`。
"""
import psycopg
from psycopg.rows import dict_row

try:
    from psycopg_pool import ConnectionPool as _ConnectionPool
    _pool_available = True
except ImportError:
    _pool_available = False

from config import DATABASE_URL

_pool = None


def _init_pool():
    global _pool
    if _pool_available and DATABASE_URL:
        try:
            _pool = _ConnectionPool(
                DATABASE_URL,
                min_size=1,
                max_size=10,
                kwargs={'row_factory': dict_row},
            )
            print("[OK] Connection pool initialised")
        except Exception as e:
            print(f"[WARN] Pool init failed, falling back to direct connect: {e}")


def get_db():
    if _pool is not None:
        return _pool.connection()
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)
