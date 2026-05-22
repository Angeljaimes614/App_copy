# -*- coding: utf-8 -*-
"""
main.py  -  Bot SEGUIDOR de copy trading (Kivy, Android + Windows).

v0.3: UI con controles + Martingala + Foreground Service para background.

Pantallas:
  - SetupScreen      : datos IQ
  - TelegramScreen   : login telegram (phone + code)
  - MainScreen       : status + ON/OFF + log + acceso a ajustes
  - SettingsScreen   : monto, stop_win, stop_loss, max_trades, martingala

En Android arranca un foreground service para sobrevivir background.
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
from kivy.uix.checkbox import CheckBox
from kivy.uix.label import Label
from kivy.uix.screenmanager import ScreenManager, Screen, SlideTransition
from kivy.uix.scrollview import ScrollView
from kivy.uix.spinner import Spinner
from kivy.uix.textinput import TextInput
from kivy.utils import platform

# Telethon + IQ
from iqoptionapi.stable_api import IQ_Option
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError

# Cimiento + Martingala
from core.db import open_db
from core.ack import AckEmitter, StateTracker
from core.executor import TradeExecutor
from core.settings import Settings
from core.stops import StopGuard
from core.martingala import Martingala
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
    """Maneja IQ Option + Telegram + martingala. Vive en su propio hilo
    con asyncio loop independiente del de Kivy."""

    def __init__(self, app_dir, log_fn, estado_fn):
        self.app_dir = app_dir
        self.datos_path = os.path.join(app_dir, ARCHIVO_DATOS)
        self.sesion_path = os.path.join(app_dir, "seguidor_session")
        self.db_path = os.path.join(app_dir, "seguidor_local.db")

        self.log_fn = log_fn
        self.estado_fn = estado_fn

        self.db = open_db(self.db_path)
        self.settings = Settings(self.db)
        if self.settings.get("seguidor_activo") is None:
            self.settings.set("seguidor_activo", True)

        self.loop = None
        self.thread = None
        self.tg = None
        self.api = None
        self.ack_emitter = None
        self.tracker = None
        self.executor = None
        self.stop_guard = None
        self.martingala = None
        self.datos = None

        self._code_future = None
        self._pwd_future = None
        self._stop_event = None

        # Cache para la UI (evita llamar get_balance() desde el hilo Kivy)
        self._saldo_cache = None
        self._last_saldo_at = 0

    # ------------------------------------------------------------------
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
        if self.settings.get("monto") is None:
            self.settings.set("monto", datos.get("monto", 5.0))

    # ------------------------------------------------------------------
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

    def dar_codigo(self, codigo):
        if self._code_future and not self._code_future.done():
            self.loop.call_soon_threadsafe(self._code_future.set_result, codigo)

    def dar_password(self, pwd):
        if self._pwd_future and not self._pwd_future.done():
            self.loop.call_soon_threadsafe(self._pwd_future.set_result, pwd)

    # ------------------------------------------------------------------
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
        try:
            self._saldo_cache = float(api.get_balance())
            self._last_saldo_at = time.time()
            self.log_fn("IQ Option OK   |   saldo $%s" % self._saldo_cache)
        except Exception:
            self.log_fn("IQ Option OK (saldo no disponible)")
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

    # ------------------------------------------------------------------
    async def _escuchar(self):
        # Pipeline completo
        self.ack_emitter = AckEmitter(self.db, self.tg, BOT_USERNAME_PANEL)
        self.tracker = StateTracker(self.ack_emitter)
        self.stop_guard = StopGuard(self.api, self.settings)

        async def buy_async(signal):
            if signal.get("_monto_override") is not None:
                monto_actual = float(signal["_monto_override"])
            else:
                monto_actual = self.settings.get_float(
                    "monto", self.datos.get("monto", 5.0))
            return await self.loop.run_in_executor(
                None, self._buy_sync, monto_actual,
                signal["par"], signal["dir"], int(signal.get("tf", 1)))

        self.martingala = Martingala(self.api, self.settings,
                                      executor=None,
                                      stop_guard=self.stop_guard)

        async def on_trade_executed(signal, order_id):
            await self.martingala.tras_ejecutar(signal, order_id)

        self.executor = TradeExecutor(
            buy_async=buy_async, tracker=self.tracker,
            on_executed=on_trade_executed)
        self.martingala.executor = self.executor

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

            # Kill-switch local
            if not self.settings.get_bool("seguidor_activo", True):
                return

            self.tracker.transition(signal_id, "recibida")

            v = s.get("v", 1)
            if v not in PROTO_SOPORTADO:
                self.tracker.transition(signal_id, "fallida_sistema",
                                         error="v_%s_unsupported" % v,
                                         error_kind="system")
                return

            try:
                self.db.exec(
                    "INSERT INTO processed_signals (signal_id, seen_at) "
                    "VALUES (?, ?)", (signal_id, time.time()))
            except sqlite3.IntegrityError:
                self.tracker.transition(signal_id, "duplicada")
                return

            # Stops
            ok_stops, reason = self.stop_guard.check()
            if not ok_stops:
                self.log_fn("BLOQUEO por stop: %s" % reason)
                self.tracker.transition(signal_id, "fallida_sistema",
                                         error=reason, error_kind="system")
                return

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

            if self.executor.submit(s):
                self.stop_guard.increment_trades()

        async def heartbeat():
            while True:
                try:
                    yo = await self.tg.get_me()
                    info = {"nombre": yo.first_name or "",
                            "correo": self.datos.get("correo", ""),
                            "cuenta": self.datos.get("cuenta", ""),
                            "saldo": self._saldo_cache}
                    await self.tg.send_message(BOT_USERNAME_PANEL,
                                                "ESTADO " + json.dumps(info))
                except Exception:
                    pass
                await asyncio.sleep(600)

        async def saldo_updater():
            """Actualiza el saldo cached cada 30s. UI lo lee sin bloquear."""
            while True:
                try:
                    self._saldo_cache = float(self.api.get_balance())
                    self._last_saldo_at = time.time()
                except Exception:
                    pass
                await asyncio.sleep(30)

        asyncio.create_task(heartbeat())
        asyncio.create_task(saldo_updater())
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
#                    UI HELPERS
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


def _seccion(txt):
    return Label(text="[b][color=94a3b8]" + txt + "[/color][/b]",
                 markup=True, font_size=dp(13),
                 size_hint_y=None, height=dp(36),
                 halign="left", valign="bottom",
                 text_size=(None, dp(36)))


def _boton(txt, color):
    return Button(text=txt, size_hint_y=None, height=dp(56),
                  font_size=dp(18), background_color=color,
                  background_normal="")


# ============================================================
#                    PANTALLAS
# ============================================================
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
    """Pantalla principal con ON/OFF prominente + status + log."""

    def __init__(self, app, **kw):
        super().__init__(**kw)
        self.app = app
        L = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(8))

        # Header: titulo + boton ajustes
        header = BoxLayout(size_hint_y=None, height=dp(50))
        header.add_widget(Label(text="[b]Bot Copy[/b]", markup=True,
                                 font_size=dp(22), halign="left",
                                 text_size=(None, dp(50))))
        btn_aj = Button(text="⚙ Ajustes", size_hint_x=None, width=dp(110),
                         font_size=dp(15),
                         background_color=(0.3, 0.3, 0.4, 1),
                         background_normal="")
        btn_aj.bind(on_press=lambda _: setattr(app.sm, "current", "settings"))
        header.add_widget(btn_aj)
        L.add_widget(header)

        # Estado actual
        self.estado = Label(text="Iniciando...", size_hint_y=None,
                             height=dp(38), font_size=dp(16),
                             color=(1, 0.8, 0.2, 1))
        L.add_widget(self.estado)

        # GRAN boton ON/OFF
        self.btn_toggle = Button(text="...", size_hint_y=None, height=dp(72),
                                  font_size=dp(20), bold=True,
                                  background_normal="",
                                  background_color=(0.5, 0.5, 0.5, 1))
        self.btn_toggle.bind(on_press=self._toggle)
        L.add_widget(self.btn_toggle)

        # Linea de stats
        self.stats = Label(text="Saldo: -   |   Monto: -",
                           size_hint_y=None, height=dp(30),
                           font_size=dp(13), color=(0.7, 0.7, 0.8, 1))
        L.add_widget(self.stats)

        # Log
        L.add_widget(_seccion("LOG"))
        scroll = ScrollView()
        self.log = Label(text="", size_hint_y=None, font_size=dp(12),
                          halign="left", valign="top",
                          color=(0.9, 0.9, 0.9, 1))
        self.log.bind(width=lambda inst, val:
                       setattr(inst, "text_size", (val - dp(8), None)))
        self.log.bind(texture_size=lambda inst, val:
                       setattr(inst, "height", val[1]))
        scroll.add_widget(self.log)
        L.add_widget(scroll)

        self.add_widget(L)
        # Repintar el boton segun estado al entrar
        Clock.schedule_interval(self._refresh_ui, 2)

    def _toggle(self, _):
        activo = self.app.engine.settings.get_bool("seguidor_activo", True)
        self.app.engine.settings.set("seguidor_activo", not activo)
        self._refresh_ui(0)
        self.app.main_s.agregar_log(
            "Bot " + ("PAUSADO" if activo else "REACTIVADO") + " manualmente")

    def _refresh_ui(self, _dt):
        # Lectura no-bloqueante. Solo settings (cache local) y _saldo_cache.
        try:
            if not getattr(self.app, "engine", None):
                return
            activo = self.app.engine.settings.get_bool("seguidor_activo", True)
            monto = self.app.engine.settings.get_float("monto", 0)
            if activo:
                self.btn_toggle.text = "🟢 ACTIVO — toca para PAUSAR"
                self.btn_toggle.background_color = (0.85, 0.30, 0.30, 1)
            else:
                self.btn_toggle.text = "⏸ PAUSADO — toca para REACTIVAR"
                self.btn_toggle.background_color = (0.20, 0.65, 0.30, 1)
            # Saldo SIEMPRE del cache, jamas se llama get_balance() aqui.
            saldo = "-"
            if self.app.engine._saldo_cache is not None:
                saldo = "$%0.2f" % self.app.engine._saldo_cache
            self.stats.text = "Saldo: %s   |   Monto: $%0.2f" % (saldo, monto)
        except Exception:
            pass

    def agregar_log(self, msg):
        marca = datetime.now().strftime("%H:%M:%S")
        if self.log.text:
            self.log.text = self.log.text + "\n[%s] %s" % (marca, msg)
        else:
            self.log.text = "[%s] %s" % (marca, msg)
        lineas = self.log.text.split("\n")
        if len(lineas) > 200:
            self.log.text = "\n".join(lineas[-200:])


class SettingsScreen(Screen):
    """Pantalla de ajustes: monto, stops, martingala."""

    def __init__(self, app, **kw):
        super().__init__(**kw)
        self.app = app

        outer = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(6))

        # Header
        header = BoxLayout(size_hint_y=None, height=dp(50))
        b_back = Button(text="← Volver", size_hint_x=None, width=dp(110),
                         font_size=dp(15),
                         background_color=(0.3, 0.3, 0.4, 1),
                         background_normal="")
        b_back.bind(on_press=lambda _: setattr(app.sm, "current", "main"))
        header.add_widget(b_back)
        header.add_widget(Label(text="[b]Ajustes[/b]", markup=True,
                                 font_size=dp(20)))
        outer.add_widget(header)

        # Scroll de campos
        scroll = ScrollView()
        cont = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(8),
                          size_hint_y=None)
        cont.bind(minimum_height=cont.setter("height"))

        cont.add_widget(_seccion("💰 OPERACION"))
        cont.add_widget(_etiqueta("Monto por operacion (USD):"))
        self.monto = _input(input_filter="float")
        cont.add_widget(self.monto)

        cont.add_widget(_seccion("🛡 GESTION DE RIESGO"))
        cont.add_widget(_etiqueta("Stop WIN (ganar X y parar — vacio = sin tope):"))
        self.stop_win = _input(input_filter="float")
        cont.add_widget(self.stop_win)
        cont.add_widget(_etiqueta("Stop LOSS (perder X y parar — vacio = sin tope):"))
        self.stop_loss = _input(input_filter="float")
        cont.add_widget(self.stop_loss)
        cont.add_widget(_etiqueta("Max trades/dia (vacio = sin tope):"))
        self.max_trades = _input(input_filter="int")
        cont.add_widget(self.max_trades)

        cont.add_widget(_seccion("🎲 MARTINGALA"))
        cont.add_widget(Label(
            text="⚠ Riesgo alto. SOLO funciona si Stop LOSS esta configurado.",
            color=(1, 0.7, 0.2, 1), size_hint_y=None, height=dp(40),
            font_size=dp(13), text_size=(None, dp(40))))
        mg_row = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(8))
        mg_row.add_widget(Label(text="Activar martingala", font_size=dp(15),
                                  halign="left"))
        self.mg_activa = CheckBox(size_hint_x=None, width=dp(50))
        mg_row.add_widget(self.mg_activa)
        cont.add_widget(mg_row)
        cont.add_widget(_etiqueta("Multiplicador (ej. 2.2):"))
        self.mg_mult = _input(input_filter="float")
        cont.add_widget(self.mg_mult)
        cont.add_widget(_etiqueta("Niveles maximos (ej. 2):"))
        self.mg_niveles = _input(input_filter="int")
        cont.add_widget(self.mg_niveles)

        scroll.add_widget(cont)
        outer.add_widget(scroll)

        # Aviso + Guardar
        self.aviso = Label(text="", size_hint_y=None, height=dp(30),
                            color=(0.4, 0.85, 0.5, 1), font_size=dp(14))
        outer.add_widget(self.aviso)
        b_save = _boton("Guardar cambios", (0.20, 0.65, 0.30, 1))
        b_save.bind(on_press=self._guardar)
        outer.add_widget(b_save)

        self.add_widget(outer)

    def on_enter(self):
        """Cuando entra a la pantalla, carga valores actuales."""
        s = self.app.engine.settings
        self.monto.text       = str(s.get_float("monto", 0) or "")
        self.stop_win.text    = s.get("stop_win") or ""
        self.stop_loss.text   = s.get("stop_loss") or ""
        self.max_trades.text  = s.get("max_trades_dia") or ""
        self.mg_activa.active = s.get_bool("martingala_activa", False)
        self.mg_mult.text     = str(s.get_float("martingala_mult", 2.2))
        self.mg_niveles.text  = str(s.get_int("martingala_niveles", 2))

    def _guardar(self, _):
        s = self.app.engine.settings
        try:
            if self.monto.text.strip():
                s.set("monto", float(self.monto.text))
            s.set("stop_win", self.stop_win.text.strip())
            s.set("stop_loss", self.stop_loss.text.strip())
            s.set("max_trades_dia", self.max_trades.text.strip())
            s.set("martingala_activa", self.mg_activa.active)
            if self.mg_mult.text.strip():
                s.set("martingala_mult", float(self.mg_mult.text))
            if self.mg_niveles.text.strip():
                s.set("martingala_niveles", int(self.mg_niveles.text))
            self.aviso.text = "✓ Guardado. Cambios aplican en proxima senal."
        except Exception as e:
            self.aviso.text = "Error: " + str(e)[:60]


class LoadingScreen(Screen):
    """Pantalla mostrada antes de inicializar nada. Evita black-screen.
    Fondo azul para que si hay bug de SDL la pantalla NO sea 100% negra."""

    def __init__(self, **kw):
        super().__init__(**kw)
        # Pintar fondo azul para distinguir 'cargando' de 'roto'
        from kivy.graphics import Color, Rectangle
        with self.canvas.before:
            Color(0.10, 0.16, 0.27, 1)  # azul oscuro Tailwind slate-900
            self._bg = Rectangle(size=self.size, pos=self.pos)
        self.bind(size=self._update_bg, pos=self._update_bg)

        L = BoxLayout(orientation="vertical", padding=dp(40))
        L.add_widget(Label(text="", size_hint_y=1))
        L.add_widget(Label(text="[b]Bot Copy[/b]", markup=True,
                            font_size=dp(28), size_hint_y=None, height=dp(40)))
        L.add_widget(Label(text="", size_hint_y=None, height=dp(12)))
        L.add_widget(Label(text="⏳ Cargando...",
                            font_size=dp(16), size_hint_y=None, height=dp(30),
                            color=(0.7, 0.85, 1.0, 1)))
        L.add_widget(Label(text="(si esta pantalla persiste 30s,\n"
                                "cierra y reabre la app)",
                            font_size=dp(11), size_hint_y=None, height=dp(40),
                            color=(0.5, 0.6, 0.7, 1)))
        L.add_widget(Label(text="", size_hint_y=1))
        self.add_widget(L)

    def _update_bg(self, *_):
        self._bg.size = self.size
        self._bg.pos = self.pos


# ============================================================
#                          APP
# ============================================================
class CopyBotApp(App):
    title = "Bot Copy"

    def build(self):
        """build() debe ser RAPIDO. Solo monta la pantalla de carga.
        El trabajo pesado va a on_start -> _after_first_frame."""
        self.engine = None
        self.sm = ScreenManager(transition=SlideTransition(direction="left"))
        self.loading_s = LoadingScreen(name="loading")
        self.sm.add_widget(self.loading_s)
        self.sm.current = "loading"
        return self.sm

    def on_start(self):
        """Se llama DESPUES del primer frame. Aqui inicializamos todo."""
        # 0.5s de margen para que Android termine de dibujar.
        Clock.schedule_once(self._after_first_frame, 0.5)
        # Service en Android: lo intentamos AUN MAS tarde (3s) y defensivo.
        Clock.schedule_once(self._intentar_servicio, 3.0)

    def _after_first_frame(self, _dt):
        """Inicializacion pesada, ya con la UI visible."""
        try:
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

            # Crear el resto de pantallas AHORA
            self.setup_s = SetupScreen(self, name="setup")
            self.tg_s = TelegramScreen(self, name="telegram")
            self.main_s = MainScreen(self, name="main")
            self.settings_s = SettingsScreen(self, name="settings")
            self.sm.add_widget(self.setup_s)
            self.sm.add_widget(self.tg_s)
            self.sm.add_widget(self.main_s)
            self.sm.add_widget(self.settings_s)

            # Decidir pantalla inicial
            if self.engine.cargar_datos():
                self.sm.current = "main"
                Clock.schedule_once(lambda dt: self._iniciar_sin_telefono(), 0.5)
            else:
                self.sm.current = "setup"
        except Exception as e:
            log.exception("after_first_frame_failed")
            # Si algo grave fallo, al menos mostrar el error en la UI
            try:
                self.loading_s.children[0].add_widget(
                    Label(text="Error: " + str(e)[:120],
                          color=(1, 0.4, 0.4, 1),
                          font_size=dp(13)))
            except Exception:
                pass

    def _intentar_servicio(self, _dt):
        """Arranca el foreground service en Android. Falla silenciosamente
        si no se puede — la app sigue funcionando aunque mas vulnerable
        a que Android la mate en background."""
        if platform != "android":
            return
        try:
            from jnius import autoclass
        except Exception as e:
            log.warning("jnius_not_available: " + repr(e))
            return

        # p4a genera el class name como {domain}.{package}.Service{Name}.
        # Probamos varios prefijos por si la convencion cambia.
        candidatos = [
            "com.zayrex.copybot.copybot.ServiceCopybot",
            "com.zayrex.copybot.ServiceCopybot",
            "org.copybot.ServiceCopybot",
        ]
        service_cls = None
        for nombre in candidatos:
            try:
                service_cls = autoclass(nombre)
                log.info("service_class_found: " + nombre)
                break
            except Exception:
                continue
        if service_cls is None:
            log.warning("service_class_not_found_in_any_path")
            return

        try:
            activity = autoclass("org.kivy.android.PythonActivity").mActivity
            Intent = autoclass("android.content.Intent")
            intent = Intent(activity, service_cls)
            # En Android 8+ usar startForegroundService para apps en bg
            try:
                activity.startForegroundService(intent)
                log.info("startForegroundService_ok")
            except Exception:
                # Fallback Android < 8 o si falla la version foreground
                activity.startService(intent)
                log.info("startService_ok")
        except Exception as e:
            log.warning("foreground_service_failed: " + repr(e))

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
            self.main_s.estado.text = "🟢 Conectado - escuchando senales"
            self.main_s.estado.color = (0.25, 0.85, 0.45, 1)
        elif nombre == "error_iq":
            self.sm.current = "main"
            self.main_s.estado.text = "❌ Error al conectar IQ Option"
            self.main_s.estado.color = (1, 0.35, 0.35, 1)
        elif nombre == "error_tg":
            self.sm.current = "main"
            self.main_s.estado.text = "❌ Error al conectar Telegram"
            self.main_s.estado.color = (1, 0.35, 0.35, 1)

    def ir_a_telegram_telefono(self):
        self.sm.current = "telegram"
        self.tg_s.set_modo("telefono")

    def iniciar_login(self, telefono):
        self.engine.lanzar(self.engine.correr(telefono))

    def _iniciar_sin_telefono(self):
        self.engine.lanzar(self.engine.correr(None))

    def on_pause(self):
        # IMPORTANTE: devolvemos FALSE para que Android mate el proceso
        # cuando vamos a background. Sin esto, SDL2 pierde el contexto
        # OpenGL y al regresar queda pantalla NEGRA (bug clasico Kivy).
        #
        # El bot sigue corriendo en background gracias al foreground
        # service (otro proceso). La UI es solo "viewer".
        #
        # En cada apertura del app es un fresh start: sin pantalla negra.
        return False

    def on_resume(self):
        # Por si Android decide NO matar el proceso a pesar de on_pause=False,
        # forzamos un repintado para combatir la pantalla negra.
        try:
            from kivy.core.window import Window
            Window.canvas.ask_update()
            if self.root:
                self.root.canvas.ask_update()
            log.info("on_resume_forced_redraw")
        except Exception:
            pass

    def on_stop(self):
        try:
            if self.engine.loop:
                self.engine.loop.call_soon_threadsafe(self.engine.loop.stop)
        except Exception:
            pass


if __name__ == "__main__":
    CopyBotApp().run()
