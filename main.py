# -*- coding: utf-8 -*-
"""
main.py  -  Bot SEGUIDOR de copy trading (Kivy, Android + Windows).

Esta version integra el cimiento de endurecimiento:
  - SQLite WAL + 3 migraciones
  - signal_id ULID + dedupe persistente
  - State machine de 10 estados con ACK pipeline
  - TradeExecutor (asyncio.Queue + retry policy)
  - StopGuard (stop_win / stop_loss / max_trades_dia)
  - Settings dinamicos (monto, seguidor_activo en caliente)
"""

import asyncio
import json
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime

# Kivy UI
from kivy.app import App
from kivy.clock import Clock
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.screenmanager import ScreenManager, Screen, SlideTransition
from kivy.uix.scrollview import ScrollView
from kivy.uix.spinner import Spinner
from kivy.uix.textinput import TextInput

# Telethon + IQ
from iqoptionapi.stable_api import IQ_Option
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError

# Endurecimiento (mismo cimiento que seguidor.py)
from core.db import open_db
from core.ack import AckEmitter, StateTracker
from core.executor import TradeExecutor
from core.settings import Settings
from core.stops import StopGuard
from core.logging_setup import setup as setup_logging, get_logger, set_correlation

setup_logging("app_seguidor")
log = get_logger("app")

# ===== Datos FIJOS del sistema =============================================
TG_API_ID = 31988246
TG_API_HASH = "5095046560758805ca1abf53b6acf2f8"
TG_CANAL = -1003745991669
BOT_USERNAME_PANEL = "@Copy_zayrex_bot"
# ===========================================================================

PROTO_SOPORTADO = (1,)
PREFIJO = "COPYSENAL "
MAX_DELAY = 15
ARCHIVO_DATOS = "mis_datos.json"


# ============================================================
#                   MOTOR DEL BOT
# ============================================================
class BotEngine:
    """Maneja IQ Option + Telegram en su propio hilo con asyncio loop.
    Plumbing del cimiento: ack emitter, executor, stop guard, settings."""

    def __init__(self, app_dir, log_fn, estado_fn):
        # Paths
        self.app_dir = app_dir
        self.datos_path = os.path.join(app_dir, ARCHIVO_DATOS)
        self.sesion_path = os.path.join(app_dir, "seguidor_session")
        self.db_path = os.path.join(app_dir, "seguidor_local.db")

        # Callbacks a la UI
        self.log_fn = log_fn
        self.estado_fn = estado_fn

        # SQLite + settings (no requieren IQ aun)
        self.db = open_db(self.db_path)
        self.settings = Settings(self.db)
        # Por defecto bot activo
        if self.settings.get("seguidor_activo") is None:
            self.settings.set("seguidor_activo", True)

        # Async / threading
        self.loop = None
        self.thread = None

        # Conexiones (se rellenan en runtime)
        self.tg = None
        self.api = None
        self.ack_emitter = None
        self.tracker = None
        self.executor = None
        self.stop_guard = None

        # Datos personales
        self.datos = None

        # Telegram async coordination
        self._code_future = None
        self._pwd_future = None
        self._stop_event = None

    # ----- persistencia de IQ creds -------------------------------------
    def cargar_datos(self):
        if os.path.exists(self.datos_path):
            try:
                self.datos = json.load(open(self.datos_path, encoding="utf-8"))
                return True
            except Exception:
                pass
        return False

    def guardar_datos(self, datos):
        self.datos = datos
        with open(self.datos_path, "w", encoding="utf-8") as f:
            json.dump(datos, f, indent=2, ensure_ascii=False)
        # Sincroniza monto a settings para uso live
        if self.settings.get("monto") is None:
            self.settings.set("monto", datos.get("monto", 5.0))

    # ----- hilo dedicado -----------------------------------------------
    def iniciar_hilo(self):
        if self.thread and self.thread.is_alive():
            return
        listo = threading.Event()

        def _correr():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            listo.set()
            self.loop.run_forever()

        self.thread = threading.Thread(target=_correr, daemon=True)
        self.thread.start()
        listo.wait()

    def lanzar(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    # ----- UI -> async loop (codigo / password) ------------------------
    def dar_codigo(self, codigo):
        if self._code_future and not self._code_future.done():
            self.loop.call_soon_threadsafe(self._code_future.set_result, codigo)

    def dar_password(self, pwd):
        if self._pwd_future and not self._pwd_future.done():
            self.loop.call_soon_threadsafe(self._pwd_future.set_result, pwd)

    # ----- conexiones --------------------------------------------------
    async def _conectar_iq(self):
        self.log_fn("Conectando a IQ Option...")

        def _hacerlo():
            api = IQ_Option(self.datos["correo"], self.datos["clave"])
            api.connect()
            for _ in range(20):
                if api.check_connect():
                    api.change_balance(self.datos["cuenta"])
                    return api
                time.sleep(1)
                api.connect()
            return None

        api = await self.loop.run_in_executor(None, _hacerlo)
        if not api:
            self.log_fn("ERROR: no se pudo conectar a IQ Option.")
            return False
        self.api = api
        self.log_fn("IQ Option OK   |   saldo $%s" % api.get_balance())

        # Cargar TODOS los activos para soportar pares nuevos
        await self.loop.run_in_executor(None, self._refrescar_activos)
        return True

    def _refrescar_activos(self):
        import iqoptionapi.constants as OP
        try:
            init = self.api.get_all_init()
            for tipo in ("turbo", "binary"):
                actives = init.get("result", {}).get(tipo, {}).get("actives", {})
                for aid, info in actives.items():
                    name = info.get("name", "")
                    if "." in name:
                        name = name[name.index(".") + 1:]
                    if name:
                        OP.ACTIVES[name] = int(aid)
            self.log_fn("Activos cargados: %d" % len(OP.ACTIVES))
        except Exception as e:
            self.log_fn("aviso activos: " + repr(e))

    async def _login_telegram(self, telefono):
        if self.tg is None:
            self.tg = TelegramClient(self.sesion_path, TG_API_ID, TG_API_HASH)
        if not self.tg.is_connected():
            await self.tg.connect()

        if await self.tg.is_user_authorized():
            yo = await self.tg.get_me()
            self.log_fn("Telegram OK   |   " + (yo.first_name or "?"))
            return True

        if not telefono:
            self.estado_fn("pedir_telefono")
            return False

        self.log_fn("Enviando codigo a Telegram...")
        try:
            await self.tg.send_code_request(telefono)
        except Exception as e:
            self.log_fn("ERROR enviando codigo: " + repr(e))
            self.estado_fn("error_tg")
            return False

        self._code_future = self.loop.create_future()
        self.estado_fn("pedir_codigo")
        try:
            codigo = await asyncio.wait_for(self._code_future, timeout=300)
        except asyncio.TimeoutError:
            self.log_fn("Timeout esperando codigo.")
            return False

        try:
            await self.tg.sign_in(phone=telefono, code=codigo)
        except SessionPasswordNeededError:
            self._pwd_future = self.loop.create_future()
            self.estado_fn("pedir_password")
            try:
                pwd = await asyncio.wait_for(self._pwd_future, timeout=300)
            except asyncio.TimeoutError:
                self.log_fn("Timeout password.")
                return False
            try:
                await self.tg.sign_in(password=pwd)
            except Exception as e:
                self.log_fn("ERROR contrasena: " + repr(e))
                self.estado_fn("error_tg")
                return False
        except Exception as e:
            self.log_fn("ERROR codigo: " + repr(e))
            self.estado_fn("error_tg")
            return False

        yo = await self.tg.get_me()
        self.log_fn("Telegram OK   |   " + (yo.first_name or "?"))
        return True

    # ----- handler de senales + executor ------------------------------
    async def _escuchar(self):
        # Inicializa pipeline ahora que IQ y TG estan listos
        self.ack_emitter = AckEmitter(self.db, self.tg, BOT_USERNAME_PANEL)
        self.tracker = StateTracker(self.ack_emitter)
        self.stop_guard = StopGuard(self.api, self.settings)

        async def buy_async(signal):
            monto_actual = self.settings.get_float(
                "monto", self.datos.get("monto", 5.0))
            return await self.loop.run_in_executor(
                None, self._buy_sync,
                monto_actual, signal["par"], signal["dir"],
                int(signal.get("tf", 1)))

        self.executor = TradeExecutor(
            buy_async=buy_async, tracker=self.tracker)

        @self.tg.on(events.NewMessage(chats=TG_CANAL))
        async def manejador(event):
            texto = event.message.message or ""
            if not texto.startswith(PREFIJO):
                return
            try:
                s = json.loads(texto[len(PREFIJO):])
            except Exception:
                return

            signal_id = s.get("signal_id")
            if not signal_id:
                return

            set_correlation(signal_id)

            # Kill-switch manual: silencio total
            if not self.settings.get_bool("seguidor_activo", True):
                return

            self.tracker.transition(signal_id, "recibida")

            # v check
            v = s.get("v", 1)
            if v not in PROTO_SOPORTADO:
                self.tracker.transition(signal_id, "fallida_sistema",
                                         error="v_%s_unsupported" % v,
                                         error_kind="system")
                return

            # dedupe persistente
            try:
                self.db.exec(
                    "INSERT INTO processed_signals (signal_id, seen_at) "
                    "VALUES (?, ?)", (signal_id, time.time()))
            except sqlite3.IntegrityError:
                self.tracker.transition(signal_id, "duplicada")
                return

            # stops
            ok_stops, reason = self.stop_guard.check()
            if not ok_stops:
                self.log_fn("BLOQUEO por stop: %s" % reason)
                self.tracker.transition(signal_id, "fallida_sistema",
                                         error=reason, error_kind="system")
                return

            # payload + freshness
            par = s.get("par")
            direccion = s.get("dir")
            tf = int(s.get("tf", 1))
            if not par or direccion not in ("call", "put"):
                self.tracker.transition(signal_id, "fallida_sistema",
                                         error="invalid_payload",
                                         error_kind="system")
                return

            creado = int(s.get("creado", time.time()))
            retraso = time.time() - creado
            if retraso > MAX_DELAY:
                self.tracker.transition(signal_id, "fallida_sistema",
                                         error="too_old_%ds" % int(retraso),
                                         error_kind="system")
                return

            self.log_fn("SENAL %s %s M%d (retraso %.1fs)" % (
                par, direccion.upper(), tf, retraso))
            self.tracker.transition(signal_id, "validada")

            # delegar al executor (queue + workers + retry)
            if self.executor.submit(s):
                self.stop_guard.increment_trades()

        async def heartbeat():
            while True:
                try:
                    yo = await self.tg.get_me()
                    info = {"nombre": yo.first_name or "",
                            "correo": self.datos.get("correo", ""),
                            "cuenta": self.datos.get("cuenta", ""),
                            "saldo": self.api.get_balance()}
                    await self.tg.send_message(BOT_USERNAME_PANEL,
                                                "ESTADO " + json.dumps(info))
                except Exception:
                    pass
                await asyncio.sleep(600)

        # tareas de background
        asyncio.create_task(heartbeat())
        asyncio.create_task(self.ack_emitter.run())
        asyncio.create_task(self.executor.start())

        self.estado_fn("escuchando")
        self.log_fn("=== Esperando senales ===")
        self._stop_event = asyncio.Event()
        await self._stop_event.wait()

    def _buy_sync(self, monto, par, direccion, tf):
        try:
            if not self.api.check_connect():
                self.api.connect()
                time.sleep(2)
            return self.api.buy(monto, par, direccion, tf)
        except Exception as e:
            return False, repr(e)

    async def correr(self, telefono=None):
        if not await self._login_telegram(telefono):
            return
        if not await self._conectar_iq():
            self.estado_fn("error_iq")
            return
        await self._escuchar()


# ============================================================
#                    PANTALLAS DE LA UI
# ============================================================
def _input(**kw):
    return TextInput(multiline=False, write_tab=False,
                     size_hint_y=None, height=dp(50),
                     font_size=dp(18), **kw)


def _titulo(txt):
    return Label(text="[b]" + txt + "[/b]", markup=True,
                 font_size=dp(24), size_hint_y=None, height=dp(60))


def _etiqueta(txt):
    return Label(text=txt, size_hint_y=None, height=dp(28),
                 font_size=dp(15), halign="left", valign="middle",
                 text_size=(None, dp(28)))


def _boton(txt, color):
    return Button(text=txt, size_hint_y=None, height=dp(56),
                  font_size=dp(18), background_color=color,
                  background_normal="")


class SetupScreen(Screen):
    def __init__(self, app, **kw):
        super().__init__(**kw)
        self.app = app
        L = BoxLayout(orientation="vertical", padding=dp(20), spacing=dp(10))
        L.add_widget(_titulo("Tus datos"))
        L.add_widget(_etiqueta("Correo de IQ Option:"))
        self.correo = _input()
        L.add_widget(self.correo)
        L.add_widget(_etiqueta("Contrasena:"))
        self.clave = _input(password=True)
        L.add_widget(self.clave)
        L.add_widget(_etiqueta("Tipo de cuenta:"))
        self.cuenta = Spinner(text="PRACTICE (demo)",
                              values=("PRACTICE (demo)", "REAL"),
                              size_hint_y=None, height=dp(50), font_size=dp(17))
        L.add_widget(self.cuenta)
        L.add_widget(_etiqueta("Monto por operacion (USD):"))
        self.monto = _input(text="5", input_filter="float")
        L.add_widget(self.monto)
        L.add_widget(Label(text="", size_hint_y=1))
        self.aviso = Label(text="", size_hint_y=None, height=dp(28),
                           color=(1, 0.4, 0.4, 1), font_size=dp(14))
        L.add_widget(self.aviso)
        b = _boton("Guardar y continuar", (0.20, 0.65, 0.30, 1))
        b.bind(on_press=self.continuar)
        L.add_widget(b)
        self.add_widget(L)

    def continuar(self, _):
        c, k, m = self.correo.text.strip(), self.clave.text, self.monto.text.strip()
        if not c or not k or not m:
            self.aviso.text = "Llena todos los campos"
            return
        try:
            monto = float(m)
        except ValueError:
            self.aviso.text = "Monto debe ser numero"
            return
        cuenta = "PRACTICE" if "PRACTICE" in self.cuenta.text else "REAL"
        self.app.engine.guardar_datos({"correo": c, "clave": k,
                                        "cuenta": cuenta, "monto": monto})
        self.app.ir_a_telegram_telefono()


class TelegramScreen(Screen):
    def __init__(self, app, **kw):
        super().__init__(**kw)
        self.app = app
        self.layout = BoxLayout(orientation="vertical",
                                 padding=dp(20), spacing=dp(10))
        self.add_widget(self.layout)
        self.modo = None
        self.set_modo("telefono")

    def set_modo(self, modo):
        self.modo = modo
        self.layout.clear_widgets()
        self.layout.add_widget(_titulo("Conectar Telegram"))
        if modo == "telefono":
            self.layout.add_widget(Label(
                text="Tu numero con codigo de pais\n(ej. +573001234567):",
                size_hint_y=None, height=dp(60), font_size=dp(15)))
            self.tel = _input()
            self.layout.add_widget(self.tel)
        elif modo == "codigo":
            self.layout.add_widget(Label(
                text="Escribe el codigo que llego\na tu app de Telegram:",
                size_hint_y=None, height=dp(60), font_size=dp(15)))
            self.codigo = _input(input_filter="int")
            self.layout.add_widget(self.codigo)
        elif modo == "password":
            self.layout.add_widget(Label(
                text="Tu contrasena de Telegram\n(verificacion en 2 pasos):",
                size_hint_y=None, height=dp(60), font_size=dp(15)))
            self.pwd = _input(password=True)
            self.layout.add_widget(self.pwd)
        self.layout.add_widget(Label(text="", size_hint_y=1))
        self.aviso = Label(text="", size_hint_y=None, height=dp(28),
                           color=(1, 0.7, 0.2, 1), font_size=dp(14))
        self.layout.add_widget(self.aviso)
        b = _boton("Enviar codigo" if modo == "telefono" else "Confirmar",
                   (0.20, 0.55, 0.90, 1))
        b.bind(on_press=self._enviar)
        self.layout.add_widget(b)

    def _enviar(self, _):
        if self.modo == "telefono":
            ph = self.tel.text.strip()
            if not ph:
                self.aviso.text = "Pon tu telefono"
                return
            self.aviso.text = "Enviando codigo..."
            self.app.iniciar_login(ph)
        elif self.modo == "codigo":
            c = self.codigo.text.strip()
            if not c:
                self.aviso.text = "Escribe el codigo"
                return
            self.aviso.text = "Verificando..."
            self.app.engine.dar_codigo(c)
        elif self.modo == "password":
            p = self.pwd.text
            if not p:
                self.aviso.text = "Escribe tu contrasena"
                return
            self.aviso.text = "Verificando..."
            self.app.engine.dar_password(p)


class MainScreen(Screen):
    def __init__(self, app, **kw):
        super().__init__(**kw)
        self.app = app
        L = BoxLayout(orientation="vertical", padding=dp(16), spacing=dp(8))
        L.add_widget(_titulo("Bot Copy"))
        self.estado = Label(text="Iniciando...", size_hint_y=None,
                             height=dp(40), color=(1, 0.8, 0.2, 1),
                             font_size=dp(16))
        L.add_widget(self.estado)
        scroll = ScrollView()
        self.log = Label(text="", size_hint_y=None, font_size=dp(13),
                          halign="left", valign="top",
                          color=(0.9, 0.9, 0.9, 1))
        self.log.bind(width=lambda inst, val:
                       setattr(inst, "text_size", (val - dp(8), None)))
        self.log.bind(texture_size=lambda inst, val:
                       setattr(inst, "height", val[1]))
        scroll.add_widget(self.log)
        L.add_widget(scroll)
        self.add_widget(L)

    def agregar_log(self, msg):
        marca = datetime.now().strftime("%H:%M:%S")
        if self.log.text:
            self.log.text = self.log.text + "\n[%s] %s" % (marca, msg)
        else:
            self.log.text = "[%s] %s" % (marca, msg)
        lineas = self.log.text.split("\n")
        if len(lineas) > 200:
            self.log.text = "\n".join(lineas[-200:])


# ============================================================
#                          APP
# ============================================================
class CopyBotApp(App):
    title = "Bot Copy"

    def build(self):
        ddir = self.user_data_dir
        try:
            os.makedirs(ddir, exist_ok=True)
        except Exception:
            pass

        self.engine = BotEngine(
            app_dir=ddir,
            log_fn=self._log_thread_safe,
            estado_fn=self._estado_thread_safe,
        )
        self.engine.iniciar_hilo()

        self.sm = ScreenManager(transition=SlideTransition(direction="left"))
        self.setup_s = SetupScreen(self, name="setup")
        self.tg_s = TelegramScreen(self, name="telegram")
        self.main_s = MainScreen(self, name="main")
        self.sm.add_widget(self.setup_s)
        self.sm.add_widget(self.tg_s)
        self.sm.add_widget(self.main_s)

        if self.engine.cargar_datos():
            self.sm.current = "main"
            Clock.schedule_once(lambda dt: self._iniciar_sin_telefono(), 0.3)
        else:
            self.sm.current = "setup"
        return self.sm

    def _log_thread_safe(self, msg):
        Clock.schedule_once(lambda dt: self.main_s.agregar_log(msg), 0)

    def _estado_thread_safe(self, nombre):
        Clock.schedule_once(lambda dt: self._cambiar_estado(nombre), 0)

    def _cambiar_estado(self, nombre):
        if nombre == "pedir_telefono":
            self.sm.current = "telegram"
            self.tg_s.set_modo("telefono")
        elif nombre == "pedir_codigo":
            self.sm.current = "telegram"
            self.tg_s.set_modo("codigo")
        elif nombre == "pedir_password":
            self.sm.current = "telegram"
            self.tg_s.set_modo("password")
        elif nombre == "escuchando":
            self.sm.current = "main"
            self.main_s.estado.text = "Conectado - escuchando senales"
            self.main_s.estado.color = (0.25, 0.85, 0.45, 1)
        elif nombre == "error_iq":
            self.sm.current = "main"
            self.main_s.estado.text = "Error al conectar IQ Option"
            self.main_s.estado.color = (1, 0.35, 0.35, 1)
        elif nombre == "error_tg":
            self.sm.current = "main"
            self.main_s.estado.text = "Error al conectar Telegram"
            self.main_s.estado.color = (1, 0.35, 0.35, 1)

    def ir_a_telegram_telefono(self):
        self.sm.current = "telegram"
        self.tg_s.set_modo("telefono")

    def iniciar_login(self, telefono):
        self.engine.lanzar(self.engine.correr(telefono))

    def _iniciar_sin_telefono(self):
        self.engine.lanzar(self.engine.correr(None))

    def on_pause(self):
        # Android: mantener la app viva en background
        return True

    def on_resume(self):
        pass

    def on_stop(self):
        try:
            if self.engine.loop:
                self.engine.loop.call_soon_threadsafe(self.engine.loop.stop)
        except Exception:
            pass


if __name__ == "__main__":
    CopyBotApp().run()
