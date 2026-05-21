# -*- coding: utf-8 -*-
"""
core.logging_setup  -  Logger JSON estructurado con correlation_id.

Uso:
  from core.logging_setup import setup, get_logger, set_correlation
  setup("madre")                       # configura una sola vez al arranque
  log = get_logger(__name__)
  set_correlation(signal_id)           # antes de procesar una senal
  log.info("signal_received", extra={"par": "EURUSD-OTC"})

Salida:
  - logs/<app>.log        rotativo 20MB x 10 (todo)
  - logs/<app>.error.log  rotativo 10MB x 20 (WARN+)
  - consola               legible humano
"""

import contextvars
import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_correlation = contextvars.ContextVar("correlation_id", default=None)


def set_correlation(value):
    """Setea el correlation_id para todos los logs en este contexto async/thread."""
    _correlation.set(value)


def get_correlation():
    return _correlation.get()


class JsonFormatter(logging.Formatter):
    """Una linea JSON por log. Apto para grep, jq, ingestion."""

    EXTRA_KEYS = (
        "signal_id", "follower_id", "seq", "state",
        "par", "dir", "tf", "order_id", "error_kind",
        "latency_ms", "retry", "backoff", "component",
        "v", "tg_id", "err", "was_state",
    )

    def format(self, record):
        d = {
            "ts": int(record.created * 1000),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "correlation_id": _correlation.get(),
        }
        for k in self.EXTRA_KEYS:
            if hasattr(record, k):
                d[k] = getattr(record, k)
        if record.exc_info:
            d["exc"] = self.formatException(record.exc_info)
        return json.dumps(d, ensure_ascii=False, default=str)


class HumanFormatter(logging.Formatter):
    """Una linea concisa para la consola."""

    def format(self, record):
        base = "%s [%s] %s: %s" % (
            self.formatTime(record, "%H:%M:%S"),
            record.levelname[0],
            record.name.split(".")[-1],
            record.getMessage(),
        )
        cid = _correlation.get()
        if cid:
            base += "  cid=" + cid[-8:]
        for k in ("signal_id", "follower_id", "state", "seq",
                  "latency_ms", "retry"):
            if hasattr(record, k):
                base += "  %s=%s" % (k, getattr(record, k))
        return base


_configured = False


def setup(app_name="copytrade", log_dir="logs", level=logging.INFO):
    """Configura el logging global. Idempotente."""
    global _configured
    if _configured:
        return
    _configured = True

    Path(log_dir).mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)

    # archivo JSON
    j = RotatingFileHandler(
        f"{log_dir}/{app_name}.log",
        maxBytes=20 * 1024 * 1024, backupCount=10, encoding="utf-8")
    j.setFormatter(JsonFormatter())
    j.setLevel(level)
    root.addHandler(j)

    # archivo de errores aparte
    e = RotatingFileHandler(
        f"{log_dir}/{app_name}.error.log",
        maxBytes=10 * 1024 * 1024, backupCount=20, encoding="utf-8")
    e.setFormatter(JsonFormatter())
    e.setLevel(logging.WARNING)
    root.addHandler(e)

    # consola legible
    c = logging.StreamHandler(sys.stdout)
    c.setFormatter(HumanFormatter())
    c.setLevel(level)
    root.addHandler(c)


def get_logger(name):
    return logging.getLogger(name)
