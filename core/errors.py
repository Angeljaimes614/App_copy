# -*- coding: utf-8 -*-
"""
core.errors  -  Clasificacion clara de errores: broker vs system.

REGLA DE ORO:
  - BrokerError   = el mercado/broker rechazo => NO reintentar
  - SystemError   = falla tecnica transitoria  => SI reintentar (con backoff)

Esto evita el peor bug posible: reintentar un trade que el broker ya
acepto -> trade duplicado en la cuenta del seguidor.
"""

# Palabras que delatan un error del broker (no se reintenta).
BROKER_KEYWORDS = (
    "not enough money",
    "instrument not available",
    "instrument is not available",
    "market closed",
    "active is closed",
    "invalid expiration",
    "invalid expiration time",
    "expiration is closed",
    "price not available",
    "amount is less",
    "amount is more",
    "not allowed",
    "wrong",
)


class CopyError(Exception):
    kind = "unknown"


class BrokerError(CopyError):
    """El broker dijo NO. Ej: mercado cerrado, saldo insuficiente.
    NUNCA se reintenta."""
    kind = "broker"


class SystemError(CopyError):
    """Error tecnico transitorio: timeout, red, websocket caido.
    SI se reintenta (con backoff)."""
    kind = "system"


def classify_buy_result(ok: bool, info) -> str:
    """Analiza el (ok, info) que devuelve API.buy() del seguidor
    y devuelve 'broker' | 'system' | 'unknown'.

    - ok=True  => no es error
    - info=None (sin mensaje) => system (timeout)
    - info contiene una palabra de BROKER_KEYWORDS => broker
    - en otro caso => unknown (por seguridad NO se reintenta)
    """
    if ok:
        return "ok"
    if info is None:
        return "system"
    s = str(info).lower()
    for w in BROKER_KEYWORDS:
        if w in s:
            return "broker"
    return "unknown"


def is_retryable(error_kind: str) -> bool:
    """Solo 'system' es reintentable. 'broker' y 'unknown' no."""
    return error_kind == "system"
