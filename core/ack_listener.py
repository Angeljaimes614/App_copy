# -*- coding: utf-8 -*-
"""
core.ack_listener  -  Handler que actualiza executions con ACKs entrantes.

Se registra en el bot de Telegram del panel. Cada mensaje "ACK {json}"
del seguidor dispara una actualizacion idempotente y ordenada.

Defensas:
  1) seq strictly mayor que el persistido
  2) precedencia: terminal no se sobrescribe
  3) UPSERT: si la fila no existe, se crea (el seguidor envio ACK
     antes de que madre pre-creara la fila)
"""

import json
import time

from telethon import events

from .logging_setup import get_logger, set_correlation
from .state_machine import (
    ALL_STATES, STATE_TIMESTAMP_COL,
    can_apply, is_terminal,
)

log = get_logger(__name__)


def register_ack_handler(bot, db):
    """Registra el handler de ACKs en el TelegramClient del panel."""

    @bot.on(events.NewMessage(pattern=r"^ACK "))
    async def on_ack(event):
        await _process_ack(event, db)

    log.info("ack_listener_registered")
    return on_ack


async def _process_ack(event, db):
    raw = event.message.text[4:]
    try:
        d = json.loads(raw)
    except Exception:
        log.warning("ack_bad_json")
        return

    if d.get("v") != 1 or d.get("type") != "ack":
        return

    sid = d.get("signal_id")
    state = d.get("state")
    seq = int(d.get("seq", 0))
    at = float(d.get("at", time.time()))
    tg_uid = event.sender_id

    if not sid or state not in ALL_STATES:
        log.warning("ack_invalid", extra={"signal_id": sid, "state": state})
        return

    set_correlation(sid)

    # Resolver follower_id
    f = db.fetchone("SELECT id FROM followers WHERE telegram_id=?", (tg_uid,))
    if not f:
        log.warning("ack_unknown_follower",
                    extra={"tg_id": tg_uid, "signal_id": sid})
        return
    fid = f["id"]

    # Aplicar con doble guard (seq + precedencia) en una sola tx
    ts_col = STATE_TIMESTAMP_COL[state]
    order_id = d.get("order_id")
    error = d.get("error")
    error_kind = d.get("error_kind")
    latency_ms = d.get("latency_ms")

    with db.tx() as c:
        cur = c.execute(
            "SELECT state, last_seq FROM executions "
            "WHERE signal_id=? AND follower_id=?",
            (sid, fid)
        ).fetchone()

        cur_state = cur["state"] if cur else None
        cur_seq   = cur["last_seq"] if cur else 0

        if not can_apply(cur_state, cur_seq, state, seq):
            log.info("ack_dropped_ordering", extra={
                "signal_id": sid, "follower_id": fid, "seq": seq,
                "state": state, "was_state": cur_state})
            return

        # UPSERT idempotente
        # NOTA: COALESCE en order_id/error/etc preserva valores ya guardados
        if cur is None:
            c.execute(f"""
              INSERT INTO executions
                (signal_id, follower_id, state, last_seq, {ts_col},
                 order_id, error, error_kind, latency_ms)
              VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (sid, fid, state, seq, at,
                  order_id, error, error_kind, latency_ms))
        else:
            # Solo seteamos la columna ts del estado nuevo
            c.execute(f"""
              UPDATE executions SET
                state      = ?,
                last_seq   = ?,
                {ts_col}   = ?,
                order_id   = COALESCE(?, order_id),
                error      = COALESCE(?, error),
                error_kind = COALESCE(?, error_kind),
                latency_ms = COALESCE(?, latency_ms)
              WHERE signal_id=? AND follower_id=?
            """, (state, seq, at, order_id, error, error_kind, latency_ms,
                  sid, fid))

    log.info("ack_applied", extra={
        "signal_id": sid, "follower_id": fid,
        "state": state, "seq": seq,
        "latency_ms": latency_ms})
