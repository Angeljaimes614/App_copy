# -*- coding: utf-8 -*-
"""
core.stops  -  Stop win / Stop loss / Max trades por dia.

Cada seguidor tiene SUS PROPIOS limites (viven en su seguidor_local.db).
Auto-pausa al cruzar threshold, auto-reset al día siguiente.

Estado en la tabla `settings`:
  CONFIG (la edita el seguidor):
    stop_win              float  monto positivo en USD, '' = sin limite
    stop_loss             float  monto positivo en USD, '' = sin limite
    max_trades_dia        int    '' = sin limite

  ESTADO RUNTIME (el bot lo maneja solo):
    session_date              YYYY-MM-DD del dia actual
    session_start_balance     saldo IQ al primer signal del dia
    trades_today              contador
    bot_pausado_por_stop      true cuando se alcanzo un stop
    stop_reason               'stop_win_reached' / 'stop_loss_reached' / etc.
"""

from datetime import date

from .logging_setup import get_logger

log = get_logger("stops")


class StopGuard:
    """Decide si se puede seguir operando. Maneja estado de sesion diaria."""

    def __init__(self, api, settings):
        self.api = api
        self.settings = settings

    # ----------------------------------------------------------------
    def check(self):
        """Returns (permitido: bool, razon: str | None).

        - permitido=True  => OK, sigue el flow normal.
        - permitido=False => bloquear; razon explica por que.

        Si la razon es "manual_off" debe interpretarse como kill-switch
        manual (silencio total). Cualquier otra razon viene de los stops
        automaticos y vale la pena ACK con error_kind=system.
        """
        # 0) Reset de sesion si es dia nuevo
        today = date.today().isoformat()
        if self.settings.get("session_date") != today:
            self._reset_session(today)

        # 1) Si ya estamos auto-pausados, devolvemos la misma razon
        if self.settings.get_bool("bot_pausado_por_stop"):
            return False, (self.settings.get("stop_reason") or "auto_paused")

        # 2) Limite duro de trades por dia
        max_trades = self.settings.get_int("max_trades_dia", 0)
        if max_trades > 0:
            n = self.settings.get_int("trades_today", 0)
            if n >= max_trades:
                self._trigger("max_trades_reached",
                              extra={"v": n, "max": max_trades})
                return False, "max_trades_reached"

        # 3) Stops por P&L del dia
        stop_win = self.settings.get_float("stop_win", 0)
        stop_loss = self.settings.get_float("stop_loss", 0)

        if stop_win > 0 or stop_loss > 0:
            try:
                bal = float(self.api.get_balance())
            except Exception as e:
                log.warning("balance_unavailable", extra={"err": str(e)[:80]})
                return True, None    # fallback: si no podemos leer saldo,
                                     # mejor no bloquear

            start = self.settings.get_float("session_start_balance", bal)
            delta = bal - start

            if stop_win > 0 and delta >= stop_win:
                self._trigger("stop_win_reached",
                              extra={"v": round(delta, 2)})
                return False, "stop_win_reached"

            if stop_loss > 0 and delta <= -abs(stop_loss):
                self._trigger("stop_loss_reached",
                              extra={"v": round(delta, 2)})
                return False, "stop_loss_reached"

        return True, None

    # ----------------------------------------------------------------
    def increment_trades(self):
        """Suma 1 al contador de trades del dia. Llamar tras submit OK."""
        n = self.settings.get_int("trades_today", 0) + 1
        self.settings.set("trades_today", n)
        return n

    # ----------------------------------------------------------------
    def estado(self):
        """Snapshot para mostrar en panel/UI."""
        return {
            "session_date":          self.settings.get("session_date") or "-",
            "session_start_balance": self.settings.get_float("session_start_balance", 0),
            "trades_today":          self.settings.get_int("trades_today", 0),
            "bot_pausado_por_stop":  self.settings.get_bool("bot_pausado_por_stop"),
            "stop_reason":           self.settings.get("stop_reason") or "",
            "stop_win":              self.settings.get_float("stop_win", 0),
            "stop_loss":             self.settings.get_float("stop_loss", 0),
            "max_trades_dia":        self.settings.get_int("max_trades_dia", 0),
        }

    # ----------------------------------------------------------------
    # Privados
    # ----------------------------------------------------------------
    def _reset_session(self, today):
        try:
            bal = float(self.api.get_balance())
        except Exception:
            bal = self.settings.get_float("session_start_balance", 0)
        self.settings.set("session_date", today)
        self.settings.set("session_start_balance", bal)
        self.settings.set("trades_today", 0)
        self.settings.set("bot_pausado_por_stop", False)
        self.settings.set("stop_reason", "")
        log.info("session_reset", extra={"v": today})

    def _trigger(self, reason, extra=None):
        self.settings.set("bot_pausado_por_stop", True)
        self.settings.set("stop_reason", reason)
        log.warning("stop_triggered", extra={"reason": reason, **(extra or {})})
