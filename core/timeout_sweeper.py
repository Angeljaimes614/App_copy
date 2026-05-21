# -*- coding: utf-8 -*-
"""
core.timeout_sweeper  -  Marca ejecuciones zombi como timeout.

Una ejecucion se considera "colgada" si esta en un estado NO terminal
y su timestamp mas reciente es anterior al cutoff (default 60s).

Esto:
  - Cierra el ciclo: nunca queda nada en 'enviada' eterna.
  - Permite al panel mostrar metricas de timeout reales.
  - Evita zombies que enredan la lectura de "que esta pasando".
"""

import asyncio
import time

from .logging_setup import get_logger
from .state_machine import TERMINAL

log = get_logger(__name__)


SQL_FIND_HUNG = """
SELECT id, signal_id, follower_id, state,
       executing_at, enqueued_at, validated_at, received_at, enviada_at
FROM executions
WHERE state NOT IN ('ejecutada','fallida_broker','fallida_sistema',
                    'timeout','duplicada')
  AND finished_at IS NULL
  AND COALESCE(executing_at, enqueued_at, validated_at,
               received_at, enviada_at, 0) < ?
"""


async def run_timeout_sweeper(db, *, threshold_sec=60, interval_sec=30):
    """Loop infinito. Cancelar con task.cancel() al apagar."""
    log.info("timeout_sweeper_start",
             extra={"v": 1})
    while True:
        try:
            await _sweep_once(db, threshold_sec=threshold_sec)
        except Exception:
            log.exception("timeout_sweeper_unhandled")
        await asyncio.sleep(interval_sec)


async def _sweep_once(db, *, threshold_sec):
    cutoff = time.time() - threshold_sec
    rows = db.fetchall(SQL_FIND_HUNG, (cutoff,))
    if not rows:
        return

    now = time.time()
    n = 0
    with db.tx() as c:
        for r in rows:
            c.execute("""
              UPDATE executions
              SET state='timeout', finished_at=?
              WHERE id=?
            """, (now, r["id"]))
            n += 1
            log.warning("execution_timeout", extra={
                "signal_id": r["signal_id"],
                "follower_id": r["follower_id"],
                "was_state": r["state"]})
    if n:
        log.info("timeout_sweeper_marked", extra={"v": n})
