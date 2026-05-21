# -*- coding: utf-8 -*-
"""
core.settings  -  Configuracion dinamica en caliente.

Diseno:
  - Tabla settings(key, value) en la DB
  - Cache en memoria con TTL corto (2s) para no machacar la DB
  - Setters invalidan el cache local (otros procesos refrescan en su TTL)
  - Tipados utiles: get_bool, get_float, get_int

Esto permite, por ejemplo:
    settings.set('madre_activa', False)
y que madre.py lo vea en a lo sumo ~2 segundos sin reinicio.
"""

import time
import threading

from .logging_setup import get_logger

log = get_logger("settings")


class Settings:
    def __init__(self, db, *, ttl=2.0):
        self.db = db
        self.ttl = ttl
        self._cache = None
        self._ts = 0
        self._lock = threading.Lock()

    # ---- helpers ----
    def _refresh(self):
        rows = self.db.fetchall("SELECT key, value FROM settings")
        self._cache = {r["key"]: r["value"] for r in rows}
        self._ts = time.time()

    def _ensure(self):
        if self._cache is None or (time.time() - self._ts) > self.ttl:
            with self._lock:
                if self._cache is None or (time.time() - self._ts) > self.ttl:
                    self._refresh()

    # ---- getters ----
    def get(self, key, default=None):
        self._ensure()
        v = self._cache.get(key)
        if v is None or v == "":
            return default
        return v

    def get_bool(self, key, default=False):
        v = self.get(key)
        if v is None:
            return default
        return str(v).strip().lower() in ("true", "1", "yes", "on", "si", "sí")

    def get_int(self, key, default=0):
        v = self.get(key)
        if v is None:
            return default
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return default

    def get_float(self, key, default=0.0):
        v = self.get(key)
        if v is None:
            return default
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    # ---- setters ----
    def set(self, key, value):
        if isinstance(value, bool):
            sv = "true" if value else "false"
        else:
            sv = str(value)
        self.db.exec(
            "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "  value=excluded.value, updated_at=excluded.updated_at",
            (key, sv, time.time())
        )
        self._ts = 0   # invalidar cache local
        log.info("setting_changed", extra={"v": key})

    def all(self):
        """Snapshot de todas las settings (cacheado)."""
        self._ensure()
        return dict(self._cache)

    def toggle(self, key):
        """Invierte el bool. Util para toggles en el panel."""
        new_val = not self.get_bool(key)
        self.set(key, new_val)
        return new_val
