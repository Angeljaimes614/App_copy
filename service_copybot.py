# -*- coding: utf-8 -*-
"""
service_copybot.py  -  Entry point del foreground service Android.

Cuando Android levanta el servicio, ejecuta este script. Mientras el
service este vivo (notificacion persistente), Android NO mata el proceso.

Importa BotEngine de main.py y lo corre indefinidamente.

NO se ejecuta en escritorio (es solo para Android).
"""

import asyncio
import os
import sys
import time


def _data_dir_android():
    """Localiza el directorio /data/data/<pkg>/files/ en Android."""
    # p4a pasa PYTHON_SERVICE_ARGUMENT con info util
    env = os.environ.get("PYTHON_SERVICE_ARGUMENT", "")
    # Por defecto data dir esperado
    pkg = "com.zayrex.copybot"
    candidates = [
        f"/data/data/{pkg}/files/app",
        f"/data/user/0/{pkg}/files/app",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    # Fallback: si nada existe, usar el cwd
    return os.getcwd()


def main():
    print("[service] arrancando foreground service")
    data_dir = _data_dir_android()
    print("[service] data_dir:", data_dir)
    sys.path.insert(0, data_dir)

    # user_data_dir donde guarda Kivy = /data/user/0/<pkg>/files/app/<app_name>
    # En servicio NO tenemos App. Usamos misma logica que Kivy:
    user_data_dir = os.environ.get(
        "ANDROID_PRIVATE",
        os.path.join(os.path.dirname(data_dir), "Bot Copy")
    )
    try:
        os.makedirs(user_data_dir, exist_ok=True)
    except Exception as e:
        print("[service] no pudo crear user_data_dir:", repr(e))

    # Importamos despues de configurar sys.path
    from main import BotEngine

    def log_fn(msg):
        print("[service]", msg)

    def estado_fn(nombre):
        print("[service] state:", nombre)

    engine = BotEngine(app_dir=user_data_dir,
                        log_fn=log_fn, estado_fn=estado_fn)
    engine.iniciar_hilo()

    if not engine.cargar_datos():
        print("[service] sin datos. UI debe completar setup primero.")
        # Esperar hasta que existan datos
        while not engine.cargar_datos():
            time.sleep(5)

    print("[service] datos OK, arrancando engine.correr()")
    engine.lanzar(engine.correr(None))

    # Mantener vivo el proceso del service para siempre
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("[service] detenido")


if __name__ == "__main__":
    main()
