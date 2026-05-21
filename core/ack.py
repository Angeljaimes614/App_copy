# -*- coding: utf-8 -*-
"""
core.ack  -  AckEmitter persistente y resiliente.

Diseno:
  enqueue() es SINCRONICO y rapido: solo escribe en pending_acks (SQLite).
  El loop run() es ASYNCRONICO: vacia la cola enviando al bot.
  Si Telegram cae, los ACKs sobreviven en SQLite y se reintentan con
  backoff exponencial cuando vuelve.

Garantias:
  - Persistencia: un ACK nunca se pierde por crash/reinicio.
  - Idempotencia: UNIQUE (signal_id, seq) impide duplicados.
  - Orden por seq: el listener del panel descarta seq <= last_seq.

NO bloquea buy(): tracker.transition() solo persiste en SQLite y
retorna; el envio real lo hace el worker.
"""

import asyncio
import json
import sqlite3
import time

from .logging_setup import get_logger
from .state_machine import ALL_STATES

log = get_logger(__name__)


class AckEmitter:
    """Persistente, async, con retry+backoff."""

    def __init__(self, db, tg, bot_username,
                 max_batch=50, idle_sleep=1.0, max_backoff=300):
        self.db = db
        self.tg = tg
        self.bot = bot_username
        self.max_batch = max_batch
        self.idle_sleep = idle_sleep
        self.max_backoff = max_backoff
        self._seq_cache = {}  # signal_id -> max_seq vista

    # ---------------------------------------------------------------
    # API publico (sincronico) - lo llama el StateTracker
    # ---------------------------------------------------------------
    def enqueue(self, signal_id, state,
                at=None, order_id=None, error=None,
                error_kind=None, latency_ms=None):
        """Persiste un ACK en pending_acks. Sincrono, rapido."""
        if state not in ALL_STATES:
            log.warning("ack_invalid_state", extra={"signal_id": signal_id,
                                                     "state": state})
            return None
        at = at if at is not None else time.time()
        seq = self._next_seq(signal_id)
        payload = {
            "v": 1, "type": "ack",
            "signal_id": signal_id, "state": state,
            "seq": seq, "at": at,
        }
        if order_id is not None:    payload["order_id"]   = str(order_id)
        if error is not None:       payload["error"]      = str(error)[:200]
        if error_kind is not None:  payload["error_kind"] = error_kind
        if latency_ms is not None:  payload["latency_ms"] = int(latency_ms)
        try:
            self.db.exec("""
              INSERT INTO pending_acks
                (signal_id, seq, state, payload, created_at, next_attempt_at)
              VALUES (?, ?, ?, ?, ?, ?)
            """, (signal_id, seq, state,
                  json.dumps(payload, separators=(",", ":")),
                  at, at))
            log.debug("ack_enqueued", extra={
                "signal_id": signal_id, "seq": seq, "state": state})
            return seq
        except sqlite3.IntegrityError:
            # Carrera improbable: ya hay un ACK con ese (signal_id, seq)
            log.warning("ack_duplicate_seq",
                        extra={"signal_id": signal_id, "seq": seq})
            return None

    def _next_seq(self, signal_id):
        if signal_id not in self._seq_cache:
            r = self.db.fetchone(
                "SELECT COALESCE(MAX(seq), 0) AS s "
                "FROM pending_acks WHERE signal_id = ?",
                (signal_id,))
            self._seq_cache[signal_id] = r["s"] if r else 0
        self._seq_cache[signal_id] += 1
        return self._seq_cache[signal_id]

    # ---------------------------------------------------------------
    # Worker async - vacia la cola
    # ---------------------------------------------------------------
    async def run(self):
        log.info("ack_emitter_start")
        while True:
            try:
                await self._flush_once()
            except Exception:
                log.exception("ack_emitter_unhandled")
            await asyncio.sleep(self.idle_sleep)

    async def _flush_once(self):
        now = time.time()
        rows = self.db.fetchall("""
          SELECT id, signal_id, payload, retry_count
          FROM pending_acks
          WHERE sent_at IS NULL AND next_attempt_at <= ?
          ORDER BY id ASC LIMIT ?
        """, (now, self.max_batch))
        if not rows:
            return

        for r in rows:
            try:
                await self.tg.send_message(self.bot, "ACK " + r["payload"])
                self.db.exec("UPDATE pending_acks SET sent_at=? WHERE id=?",
                             (time.time(), r["id"]))
                log.debug("ack_sent", extra={"signal_id": r["signal_id"]})
            except Exception as e:
                rc = r["retry_count"] + 1
                backoff = min(2 ** rc, self.max_backoff)
                self.db.exec("""
                  UPDATE pending_acks
                  SET retry_count=?, next_attempt_at=?
                  WHERE id=?
                """, (rc, time.time() + backoff, r["id"]))
                log.warning("ack_send_failed", extra={
                    "signal_id": r["signal_id"], "retry": rc,
                    "backoff": backoff, "err": str(e)[:120]})

    # ---------------------------------------------------------------
    # GC: borra ACKs ya enviados con > N horas (housekeeping)
    # ---------------------------------------------------------------
    def gc(self, max_age_seconds=86400):
        cutoff = time.time() - max_age_seconds
        cur = self.db.exec(
            "DELETE FROM pending_acks WHERE sent_at IS NOT NULL AND sent_at < ?",
            (cutoff,))
        try:
            n = cur.rowcount if cur else 0
        except Exception:
            n = 0
        if n > 0:
            log.info("ack_gc", extra={"deleted": n})


class StateTracker:
    """Atajo: tracker.transition(signal_id, state, ...) encola ACK."""

    def __init__(self, emitter):
        self.emitter = emitter

    def transition(self, signal_id, state, **kw):
        return self.emitter.enqueue(signal_id, state, **kw)
