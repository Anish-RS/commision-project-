import os
import atexit
import threading
import time
import psycopg2
from psycopg2 import pool
from flask import g, has_request_context, after_this_request

# ---------------------------------------------------------------------------
# Connection pool
#
# Opening a brand-new TCP+TLS connection to Neon on every request is the
# single biggest cause of slow page loads. This module keeps a small pool
# of warm connections alive and hands them out instead.
#
# IMPORTANT: app.py is untouched everywhere else — it still calls
# get_connection() and conn.close() exactly as before. We make that safe by
# wrapping the real connection so that .close() returns it to the pool
# instead of actually closing the socket.
#
# ENHANCEMENT (this version): connections are now ALSO released
# automatically at the end of every Flask request, even if a route raises
# an exception before it reaches its own conn.close(). Previously, any
# unhandled error in a route permanently leaked that connection out of the
# pool. With a small, fixed-size pool (see DB_POOL_MAX below), a handful
# of errors over the app's lifetime was enough to exhaust it completely,
# causing every subsequent request (from any user) to fail with
# "pool exhausted" — which is what produced the 500 errors under normal
# multi-user usage.
# ---------------------------------------------------------------------------

_pool = None
_pool_lock = threading.Lock()


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
        with _pool_lock:
            # double-checked locking: another thread may have already
            # created the pool while we were waiting for the lock
            if _pool is None:
                db_url = _resolve_db_url()
                min_conn = int(os.environ.get("DB_POOL_MIN", "1"))
                max_conn = int(os.environ.get("DB_POOL_MAX", "10"))
                _pool = pool.ThreadedConnectionPool(
                    min_conn,
                    max_conn,
                    dsn=db_url,
                    connect_timeout=10,
                    # keepalives stop Neon/your host from silently dropping
                    # idle connections and you finding out only when a
                    # query hangs
                    keepalives=1,
                    keepalives_idle=30,
                    keepalives_interval=10,
                    keepalives_count=3,
                )
    return _pool


def _checkout_connection():
    """Get a raw connection from the pool, retrying briefly if the pool is
    momentarily exhausted (e.g. a genuine short burst of concurrent
    requests) instead of failing the request instantly."""
    conn_pool = _get_pool()
    attempts = int(os.environ.get("DB_POOL_CHECKOUT_RETRIES", "3"))
    delay = 0.2
    last_err = None
    for attempt in range(attempts):
        try:
            return conn_pool.getconn()
        except pool.PoolError as e:
            last_err = e
            if attempt < attempts - 1:
                time.sleep(delay)
                delay *= 2
    raise last_err


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

    @property
    def returned(self):
        return self._returned

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
    # Outside of a Flask request (e.g. a one-off script), just hand back a
    # pooled connection directly — nothing to auto-release later.
    if not has_request_context():
        conn_pool = _get_pool()
        raw_conn = _checkout_connection()
        return _PooledConnection(conn_pool, raw_conn)

    # Inside a request: reuse the same connection if this request already
    # has one checked out and it hasn't been closed yet.
    existing = getattr(g, "_db_conn", None)
    if existing is not None and not existing.returned:
        return existing

    conn_pool = _get_pool()
    raw_conn = _checkout_connection()
    wrapped = _PooledConnection(conn_pool, raw_conn)
    g._db_conn = wrapped

    @after_this_request
    def _release_db_connection(response):
        # Safety net: runs at the end of every request — success, handled
        # error, or unhandled exception — so a connection can never leak
        # out of the pool just because a route forgot to close it or
        # crashed before reaching its own conn.close().
        try:
            current = getattr(g, "_db_conn", None)
            if current is not None:
                current.close()
        except Exception:
            pass
        return response

    return wrapped


@atexit.register
def _close_pool():
    global _pool
    if _pool is not None:
        try:
            _pool.closeall()
        except Exception:
            pass
