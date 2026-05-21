# -*- coding: utf-8 -*-
"""
core.auth  -  Hashing y verificacion de contrasenas del panel.

Usa PBKDF2-SHA256 (stdlib). Sin dependencias externas.
Guarda el hash + salt en la tabla settings.
"""

import hashlib
import hmac
import secrets

ITERACIONES = 200_000
ALGO = "pbkdf2_sha256"


def hashear(plain: str) -> str:
    """Devuelve 'pbkdf2_sha256$200000$<salt_hex>$<hash_hex>'."""
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"),
                            salt.encode("utf-8"), ITERACIONES)
    return f"{ALGO}${ITERACIONES}${salt}${h.hex()}"


def verificar(plain: str, stored: str) -> bool:
    """Compara constante-tiempo. False si el formato del stored es invalido."""
    if not stored or not plain:
        return False
    try:
        algo, iters, salt, expected = stored.split("$")
        if algo != ALGO:
            return False
        iters = int(iters)
        candidato = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"),
                                         salt.encode("utf-8"), iters)
        return hmac.compare_digest(candidato.hex(), expected)
    except Exception:
        return False


def set_password(settings, plain: str):
    settings.set("panel_password_hash", hashear(plain))


def check_password(settings, plain: str) -> bool:
    return verificar(plain, settings.get("panel_password_hash"))


def has_password(settings) -> bool:
    return bool(settings.get("panel_password_hash"))


def ensure_secret_key(settings) -> str:
    """Devuelve la secret_key de Flask. La crea si no existe."""
    sk = settings.get("panel_secret_key")
    if not sk:
        sk = secrets.token_urlsafe(48)
        settings.set("panel_secret_key", sk)
    return sk
