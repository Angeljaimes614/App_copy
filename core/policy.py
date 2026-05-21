# -*- coding: utf-8 -*-
"""
core.policy  -  Constantes de comportamiento del executor.

Centralizadas aqui para que el comportamiento operativo sea
declarativo y modificable sin tocar la logica del executor.
"""

# === Frescura ==============================================================
# Si una senal llega mas vieja que esto al RECIBIRLA -> rechazo
MAX_DELAY_RECEIVED_SEC = 15
# Si una senal salio de la queue pero ya esta vieja -> no se ejecuta
MAX_DELAY_IN_QUEUE_SEC = 20

# === Buy ===================================================================
# asyncio.wait_for sobre buy(). La API blockea normalmente ~1-3s; mas que
# esto significa timeout / cuelgue.
BUY_TIMEOUT_SEC = 10

# === Reintentos ============================================================
# Total de intentos (1 = sin retry). Solo errores 'system' se reintentan.
MAX_ATTEMPTS_SYSTEM_ERROR = 2
# Backoff exponencial entre reintentos
BACKOFF_BASE = 2.0
MAX_BACKOFF_SEC = 10

# === Queue =================================================================
QUEUE_MAX_SIZE = 200
MAX_WORKERS = 1                     # la API no es thread-safe (Sem 3 sube a N)

# === Retry policy por tipo de error =======================================
#   broker  -> NUNCA reintenta (la cuenta ya quedo en cierto estado)
#   system  -> reintenta hasta MAX_ATTEMPTS_SYSTEM_ERROR
#   unknown -> NO reintenta (conservador: prefiero perder una copia
#              antes que duplicar un trade)
RETRY_POLICY = {
    "broker":  {"retry": False},
    "system":  {"retry": True,  "max_attempts": MAX_ATTEMPTS_SYSTEM_ERROR},
    "unknown": {"retry": False},
}
