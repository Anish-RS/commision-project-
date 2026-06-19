import os
import atexit
import psycopg2
from psycopg2 import pool

# ---------------------------------------------------------------------------
# Connection pool
#
# Opening a brand-new TCP+TLS connection to Neon on every request is the
# single biggest cause of slow page loads. This module now keeps a small
# pool of warm connections alive and hands them out instead.
#
# IMPORTANT: app.py is untouched everywhere else — it still calls
# get_connection() and conn.close() exactly as before. We make that safe by
# wrapping the real connection so that .close() returns it to the pool
# instead of actually closing the socket.
# ---------------------------------------------------------------------------

_pool = None


def _resolve_db_url():
    db_url = os.environ.get("POSTGRES_URL") or os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError(
            "Database connection URL string not found in environment variables. "
            "Please configure POSTGRES_URL or DATABASE_URL in Vercel settings."
        )
    return db_url


def _get_pool():
    global _pool
    if _pool is None:
        db_url = _resolve_db_url()
        min_conn = int(os.environ.get("DB_POOL_MIN", "1"))
        max_conn = int(os.environ.get("DB_POOL_MAX", "5"))
        _pool = psycopg2.pool.ThreadedConnectionPool(
            min_conn,
            max_conn,
            dsn=db_url,
            connect_timeout=10,
            # keepalives stop Neon/your host from silently dropping idle
            # connections and you finding out only when a query hangs
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=3,
        )
    return _pool


class _PooledConnection:
    """Thin wrapper so existing `conn.close()` calls return the connection
    to the pool instead of dropping it. Everything else (cursor, commit,
    rollback, etc.) passes straight through to the real connection."""

    __slots__ = ("_pool", "_conn", "_returned")

    def __init__(self, conn_pool, conn):
        self._pool = conn_pool
        self._conn = conn
        self._returned = False

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        if self._returned:
            return
        self._returned = True
        try:
            if self._conn.closed == 0:
                # leaving an open transaction on a pooled connection is a
                # common source of mysterious bugs for the next borrower
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                self._pool.putconn(self._conn)
            else:
                self._pool.putconn(self._conn, close=True)
        except Exception:
            try:
                self._conn.close()
            except Exception:
                pass


def get_connection():
    conn_pool = _get_pool()
    raw_conn = conn_pool.getconn()
    return _PooledConnection(conn_pool, raw_conn)


@atexit.register
def _close_pool():
    global _pool
    if _pool is not None:
        try:
            _pool.closeall()
        except Exception:
            pass
