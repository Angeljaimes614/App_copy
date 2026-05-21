# -*- coding: utf-8 -*-
"""
core.ids  -  Generador de IDs unicos ordenados por tiempo (ULID-like).

Por que ULID y no UUID4:
  - Ordenado por tiempo => indices SQL eficientes
  - 26 caracteres legibles (Crockford Base32)
  - 80 bits aleatorios => practica colision imposible
  - Util como correlation_id para tracing entre madre y seguidor

Compatibilidad: solo stdlib, sin dependencias externas.
"""

import secrets
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford (sin I, L, O, U)


def _encode_base32(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = []
    for _ in range(26):
        n, r = divmod(n, 32)
        out.append(_ALPHABET[r])
    return "".join(reversed(out))


def new_signal_id() -> str:
    """Genera un ULID-like de 26 caracteres."""
    ts_ms = int(time.time() * 1000)
    payload = ts_ms.to_bytes(6, "big") + secrets.token_bytes(10)
    return _encode_base32(payload)


def correlation_id() -> str:
    """Alias para usar como correlation_id de logs."""
    return new_signal_id()


if __name__ == "__main__":
    # demostracion: los IDs son ordenados en el tiempo
    for _ in range(3):
        print(new_signal_id())
        time.sleep(0.001)
