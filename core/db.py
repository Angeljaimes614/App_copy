# -*- coding: utf-8 -*-
"""
core.db  -  Capa de persistencia SQLite (WAL + thread-safe).

Diseno:
  - Una conexion por thread (gracias a threading.local)
  - WAL para lecturas concurrentes sin bloquear
  - busy_timeout=10s para tolerar contencion
  - Migraciones declarativas (corre las que faltan al abrir)
  - tx() context manager con BEGIN IMMEDIATE para escrituras
  - PRAGMA foreign_keys=ON desde el inicio

Uso:
  from core.db import open_db
  db = open_db("copytrade.db")            # madre
  db_seguidor = open_db("seguidor.db")    # seguidor (mismo schema, otro archivo)
"""

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

MIGRACIONES_DIR = Path(__file__).resolve().parent.parent / "migrations"


class DB:
    """Wrapper de sqlite3 thread-safe con WAL y migraciones automaticas."""

    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        self._lock = threading.RLock()        # solo para escrituras
        self._ensure_schema()

    # ----- conexion por hilo ---------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "c"):
            c = sqlite3.connect(self.path, timeout=30,
                                isolation_level=None,
                                check_same_thread=False)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA busy_timeout=10000")
            c.row_factory = sqlite3.Row
            self._local.c = c
        return self._local.c

    # ----- transaccion segura --------------------------------------------
    @contextmanager
    def tx(self):
        """Bloqueo exclusivo para escrituras. Rollback automatico ante error."""
        with self._lock:
            c = self._conn()
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise

    # ----- helpers de consulta -------------------------------------------
    def exec(self, sql: str, params=()):
        with self.tx() as c:
            return c.execute(sql, params)

    def execmany(self, sql: str, seq):
        with self.tx() as c:
            return c.executemany(sql, seq)

    def fetchone(self, sql: str, params=()):
        return self._conn().execute(sql, params).fetchone()

    def fetchall(self, sql: str, params=()):
        return self._conn().execute(sql, params).fetchall()

    # ----- migraciones ---------------------------------------------------
    def _ensure_schema(self):
        """Aplica las migraciones .sql que falten."""
        c = self._conn()
        c.execute("""
          CREATE TABLE IF NOT EXISTS schema_meta(
            key TEXT PRIMARY KEY, value TEXT NOT NULL)
        """)
        aplicadas = set()
        for row in c.execute("SELECT value FROM schema_meta WHERE key LIKE 'mig:%'"):
            aplicadas.add(row[0])

        if not MIGRACIONES_DIR.exists():
            return

        for sql_file in sorted(MIGRACIONES_DIR.glob("*.sql")):
            name = sql_file.name
            if name in aplicadas:
                continue
            sql = sql_file.read_text(encoding="utf-8")
            with self._lock:
                c.executescript(sql)
                c.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES (?,?)",
                          (f"mig:{name}", name))

    def schema_version(self):
        r = self.fetchone("SELECT value FROM schema_meta WHERE key='version'")
        return int(r["value"]) if r else 0


_dbs = {}


def open_db(path: str) -> DB:
    """Devuelve una instancia DB compartida para la misma ruta."""
    path = os.path.abspath(path)
    if path not in _dbs:
        _dbs[path] = DB(path)
    return _dbs[path]
