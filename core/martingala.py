# -*- coding: utf-8 -*-
"""
core.martingala  -  Estrategia de martingala con guardrails serios.

Filosofia de seguridad (no negociables):
  G1) Solo se activa si el seguidor configuro stop_loss explicito.
  G2) El monto martingala JAMAS supera el 20% del saldo actual.
  G3) Respeta el maximo de niveles configurado (martingala_niveles).
  G4) Respeta stop_win / stop_loss / max_trades_dia del StopGuard.
  G5) Si no podemos leer saldo, NO disparamos martingala (fail-closed).
  G6) Si no podemos confirmar el resultado, NO reintentamos (fail-closed).

Flujo:
  buy() -> on_executed callback
                |
                v
       Martingala.tras_ejecutar(signal, order_id, level)
                |
                v
       sleep hasta exp_time + 5s
                |
                v
       get_optioninfo_v2 -> buscar order_id en closed_options
                |
       +---WIN---+
       |         |
       |         v
       |    (cadena termina, todo bien)
       v
      LOSS
       |
       v
       Validar guardrails (G1..G6)
       |
       v
       Generar signal sintetico (mismo par/dir/tf, monto multiplicado)
       executor.submit(sintetico)
       (su propio on_executed dispara la recursion)
"""

import asyncio
import time

from .ids import new_signal_id
from .logging_setup import get_logger, set_correlation

log = get_logger("martingala")


# Hard cap absoluto: jamas se invierte mas de 20% del saldo en un trade
HARD_CAP_PCT_SALDO = 0.20


class Martingala:
    """Encapsula la logica de martingala con todos los guardrails."""

    def __init__(self, api, settings, executor, stop_guard=None,
                 db=None, *, tracker=None):
        self.api = api
        self.settings = settings
        self.executor = executor
        self.stop_guard = stop_guard
        self.db = db
        self.tracker = tracker

    # ------------------------------------------------------------
    def _activada(self):
        return self.settings.get_bool("martingala_activa", False)

    def _niveles_max(self):
        return self.settings.get_int("martingala_niveles", 2)

    def _multiplicador(self):
        return self.settings.get_float("martingala_mult", 2.2)

    # ------------------------------------------------------------
    async def tras_ejecutar(self, signal, order_id):
        """Hook que se llama tras CADA buy() exitoso.

        Si la martingala esta activada, agenda el chequeo del resultado.
        Si el trade fue una martingala mismo, sigue la cadena.
        """
        if not self._activada():
            return
        if not order_id:
            return

        nivel_actual = int(signal.get("_martingala_level", 0))
        # Encadenar el watcher como background task
        asyncio.create_task(self._observar_resultado(signal, order_id, nivel_actual))

    # ------------------------------------------------------------
    async def _observar_resultado(self, signal, order_id, nivel):
        set_correlation(signal.get("signal_id"))
        try:
            # Esperar hasta despues del cierre de la vela
            try:
                exp = int(signal.get("exp", 0))
                wait = exp - time.time() + 5
            except Exception:
                wait = signal.get("tf", 1) * 60 + 5
            if wait > 0:
                await asyncio.sleep(min(wait, 600))   # cap 10 min

            resultado, profit = await self._consultar_resultado(order_id)
            if resultado is None:
                # G6: no pudimos confirmar -> no reintentar
                log.warning("martingala_resultado_no_disponible",
                            extra={"signal_id": signal.get("signal_id"),
                                   "order_id": str(order_id)})
                return

            log.info("martingala_resultado", extra={
                "signal_id": signal.get("signal_id"),
                "v": resultado, "latency_ms": None,
                "order_id": str(order_id)})

            # WIN o equal -> cadena se cierra
            if resultado != "loss":
                return

            # LOSS -> Validar guardrails
            if not self._validar_guardrails(nivel):
                return

            # Calcular monto siguiente
            monto_base = float(signal.get("_monto_base") or
                                self.settings.get_float("monto", 5))
            siguiente_nivel = nivel + 1
            mult = self._multiplicador()
            monto_mg = round(monto_base * (mult ** siguiente_nivel), 2)

            # G2: hard cap 20% del saldo
            try:
                saldo = float(self.api.get_balance())
            except Exception as e:
                log.warning("martingala_saldo_no_leido", extra={"err": str(e)[:80]})
                return
            tope = saldo * HARD_CAP_PCT_SALDO
            if monto_mg > tope:
                log.warning("martingala_excede_safety_cap", extra={
                    "v": monto_mg, "max": round(tope, 2)})
                return

            # Construir signal sintetico
            ahora = int(time.time())
            tf = int(signal.get("tf", 1))
            sintetico = {
                "v": 1,
                "signal_id": new_signal_id(),
                "par": signal["par"],
                "dir": signal["dir"],
                "tf": tf,
                "creado": ahora,
                "exp": ahora + tf * 60,
                "tipo": "turbo",
                # campos internos para el executor
                "_monto_override": monto_mg,
                "_monto_base": monto_base,
                "_martingala_level": siguiente_nivel,
                "_martingala_parent_signal_id": signal.get("signal_id"),
            }
            log.warning("martingala_disparada", extra={
                "v": siguiente_nivel, "signal_id": sintetico["signal_id"]})
            self.executor.submit(sintetico)
        except Exception:
            log.exception("martingala_observador_error")

    # ------------------------------------------------------------
    async def _consultar_resultado(self, order_id):
        """Devuelve ('win'/'loss'/'equal', profit_neto) o (None, 0)."""
        try:
            info = await asyncio.get_event_loop().run_in_executor(
                None, self.api.get_optioninfo_v2, 30)
        except Exception as e:
            log.warning("get_optioninfo_failed", extra={"err": str(e)[:80]})
            return None, 0

        try:
            cerradas = info["msg"].get("closed_options", [])
        except Exception:
            return None, 0

        oid = int(order_id)
        for c in cerradas:
            ids = c.get("id") or []
            if oid in ids:
                win = (c.get("win") or "").lower()
                amount = float(c.get("amount", 0)) / 1_000_000.0
                win_amount = float(c.get("win_amount", 0))
                if win == "win":
                    return "win", round(win_amount - amount, 2)
                if win == "loose" or win == "loss":
                    return "loss", round(-amount, 2)
                return "equal", 0
        return None, 0   # no encontrada (vela todavia no cerro o expiro)

    # ------------------------------------------------------------
    def _validar_guardrails(self, nivel_actual):
        """Devuelve True si TODAS las guardrails permiten otro paso."""
        # G3: niveles maximos
        if nivel_actual + 1 > self._niveles_max():
            log.info("martingala_max_niveles_alcanzado",
                     extra={"v": nivel_actual})
            return False

        # G1: requiere stop_loss configurado (proteccion del cliente)
        if self.settings.get_float("stop_loss", 0) <= 0:
            log.warning("martingala_bloqueada_sin_stop_loss")
            return False

        # G4: respetar StopGuard (stop_win/stop_loss/max_trades)
        if self.stop_guard is not None:
            ok, razon = self.stop_guard.check()
            if not ok:
                log.info("martingala_bloqueada_por_stop_guard",
                         extra={"v": razon})
                return False

        return True
