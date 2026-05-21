# -*- coding: utf-8 -*-
"""
core.executor  -  Cola asincrona profesional para ejecutar trades.

Reemplaza al `asyncio.Lock` simple por una arquitectura con:
  - asyncio.Queue con BACKPRESSURE real (rechaza si esta llena)
  - Workers asincronos (max_workers=1 hoy por restriccion de la API IQ)
  - Retry declarativo SOLO para errores 'system' (broker/unknown jamas)
  - Timeout configurable sobre cada buy()
  - Metricas en memoria expuestas via .stats()
  - Transiciones de estado correctas via StateTracker

Flujo:
  submit(signal)  -> sync, encola o rechaza
                     marca 'encolada' o 'fallida_sistema' (queue_full)
  worker pulls    -> chequea frescura (puede marcar fallida_sistema)
                  -> 'ejecutando' + buy_async
                  -> result -> 'ejecutada' | 'fallida_broker'
                              | 'fallida_sistema' | 'timeout'
  Si error system -> retry con backoff exponencial hasta MAX_ATTEMPTS
"""

import asyncio
import collections
import time
from dataclasses import dataclass, field

from .errors import classify_buy_result
from .logging_setup import get_logger, set_correlation
from .policy import (
    BUY_TIMEOUT_SEC, MAX_DELAY_IN_QUEUE_SEC,
    QUEUE_MAX_SIZE, MAX_WORKERS, RETRY_POLICY,
    BACKOFF_BASE, MAX_BACKOFF_SEC,
)

log = get_logger("executor")


@dataclass
class Job:
    signal: dict
    attempts: int = 0
    max_attempts: int = 2


class TradeExecutor:
    """Worker pool + queue + retry + metricas."""

    def __init__(self, buy_async, tracker, *,
                 max_workers=MAX_WORKERS,
                 max_queue=QUEUE_MAX_SIZE,
                 buy_timeout=BUY_TIMEOUT_SEC,
                 max_delay_in_queue=MAX_DELAY_IN_QUEUE_SEC):
        self.q: asyncio.Queue = asyncio.Queue(maxsize=max_queue)
        self.buy_async = buy_async       # async def buy_async(signal) -> (ok, info)
        self.tracker = tracker
        self.max_workers = max_workers
        self.buy_timeout = buy_timeout
        self.max_delay_in_queue = max_delay_in_queue
        self.workers = []
        self.metrics = collections.Counter()
        self._stopping = False

    # -----------------------------------------------------------------
    #                       API publica
    # -----------------------------------------------------------------
    def submit(self, signal):
        """Encola una senal. SINCRONICO. Retorna True/False.

        Si la queue esta llena marca la senal como fallida_sistema
        con error 'queue_full_backpressure' y retorna False.
        """
        sid = signal["signal_id"]
        try:
            job = Job(signal=signal,
                      max_attempts=RETRY_POLICY["system"]["max_attempts"])
            self.q.put_nowait(job)
            self.metrics["enqueued"] += 1
            self.tracker.transition(sid, "encolada")
            return True
        except asyncio.QueueFull:
            self.metrics["dropped_queue_full"] += 1
            log.warning("queue_full_backpressure", extra={
                "signal_id": sid, "queue_size": self.q.qsize()})
            self.tracker.transition(sid, "fallida_sistema",
                                    error="queue_full_backpressure",
                                    error_kind="system")
            return False

    async def start(self):
        """Lanza los workers. Llamar desde un context con event loop."""
        for i in range(self.max_workers):
            t = asyncio.create_task(self._worker(i))
            self.workers.append(t)
        log.info("executor_started", extra={"v": self.max_workers})

    async def stop(self):
        """Cancela workers y espera limpieza. Para tests / shutdown."""
        self._stopping = True
        for w in self.workers:
            w.cancel()
        for w in self.workers:
            try:
                await w
            except asyncio.CancelledError:
                pass
        self.workers.clear()

    def stats(self):
        return {
            **self.metrics,
            "queue_size": self.q.qsize(),
            "queue_max": self.q.maxsize,
            "queue_full": self.q.full(),
            "workers": len(self.workers),
        }

    # -----------------------------------------------------------------
    #                         Worker loop
    # -----------------------------------------------------------------
    async def _worker(self, idx):
        log.info("worker_start", extra={"v": idx})
        try:
            while True:
                job = await self.q.get()
                try:
                    await self._process(job)
                except Exception:
                    log.exception("worker_unhandled", extra={
                        "v": idx,
                        "signal_id": job.signal.get("signal_id")})
                finally:
                    self.q.task_done()
        except asyncio.CancelledError:
            log.info("worker_stop", extra={"v": idx})
            raise

    async def _process(self, job):
        sig = job.signal
        sid = sig["signal_id"]
        set_correlation(sid)

        # Si la senal envejecio en la queue (backpressure), descartar.
        creado = int(sig.get("creado", time.time()))
        retraso = time.time() - creado
        if retraso > self.max_delay_in_queue:
            self.metrics["dropped_stale"] += 1
            log.warning("dropped_stale_in_queue", extra={
                "signal_id": sid, "latency_ms": int(retraso * 1000)})
            self.tracker.transition(sid, "fallida_sistema",
                                    error="too_old_in_queue_%ds" % int(retraso),
                                    error_kind="system",
                                    latency_ms=int(retraso * 1000))
            return

        # Loop de reintentos
        while job.attempts < job.max_attempts:
            job.attempts += 1
            self.tracker.transition(sid, "ejecutando")
            t0 = time.time()

            # ---- ejecutar buy con timeout ----
            try:
                ok, info = await asyncio.wait_for(
                    self.buy_async(sig),
                    timeout=self.buy_timeout)
                latency = int((time.time() - t0) * 1000)
            except asyncio.TimeoutError:
                latency = int((time.time() - t0) * 1000)
                if job.attempts >= job.max_attempts:
                    self.metrics["timeout"] += 1
                    log.warning("buy_timeout_final", extra={
                        "signal_id": sid, "latency_ms": latency,
                        "retry": job.attempts})
                    self.tracker.transition(sid, "timeout",
                                            latency_ms=latency)
                    return
                self.metrics["retried"] += 1
                delay = min(BACKOFF_BASE ** job.attempts, MAX_BACKOFF_SEC)
                log.warning("buy_timeout_retry", extra={
                    "signal_id": sid, "retry": job.attempts,
                    "latency_ms": latency})
                await asyncio.sleep(delay)
                continue
            except Exception as e:
                latency = int((time.time() - t0) * 1000)
                # Excepcion no anticipada -> sistema, NO se reintenta
                self.metrics["executed_fail_system"] += 1
                log.exception("buy_unexpected_exception", extra={
                    "signal_id": sid, "latency_ms": latency})
                self.tracker.transition(sid, "fallida_sistema",
                                        error=repr(e)[:120],
                                        error_kind="system",
                                        latency_ms=latency)
                return

            # ---- clasificar resultado ----
            if ok:
                self.metrics["executed_ok"] += 1
                log.info("execution_ok", extra={
                    "signal_id": sid, "order_id": str(info),
                    "latency_ms": latency, "retry": job.attempts - 1})
                self.tracker.transition(sid, "ejecutada",
                                        order_id=str(info),
                                        latency_ms=latency)
                return

            # ok=False -> ver tipo de error
            kind = classify_buy_result(ok, info)
            policy = RETRY_POLICY[kind]

            if not policy["retry"]:
                # Terminal sin reintento
                if kind == "broker":
                    self.metrics["executed_fail_broker"] += 1
                    final_state = "fallida_broker"
                else:
                    self.metrics["executed_fail_unknown"] += 1
                    final_state = "fallida_sistema"
                log.warning("execution_fail_terminal", extra={
                    "signal_id": sid, "error_kind": kind,
                    "latency_ms": latency})
                self.tracker.transition(sid, final_state,
                                        error=str(info)[:120],
                                        error_kind=kind,
                                        latency_ms=latency)
                return

            # kind == "system" y la policy permite reintentar
            if job.attempts >= job.max_attempts:
                # Agotado
                self.metrics["executed_fail_system_exhausted"] += 1
                log.warning("system_retry_exhausted", extra={
                    "signal_id": sid, "retry": job.attempts,
                    "latency_ms": latency})
                self.tracker.transition(sid, "fallida_sistema",
                                        error=str(info)[:120],
                                        error_kind="system",
                                        latency_ms=latency)
                return

            self.metrics["retried"] += 1
            delay = min(BACKOFF_BASE ** job.attempts, MAX_BACKOFF_SEC)
            log.warning("retry_after_system", extra={
                "signal_id": sid, "retry": job.attempts,
                "latency_ms": latency})
            await asyncio.sleep(delay)
            # vuelve al while
