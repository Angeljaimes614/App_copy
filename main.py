# -*- coding: utf-8 -*-
"""
main.py  -  Bot SEGUIDOR de copy trading con interfaz grafica.

Funciona en:
  - Windows / Linux / Mac (corre con: python main.py)
  - Android (compilado a APK con Buildozer)

La primera vez te pide tus datos (correo IQ, contrasena, monto) y
te conecta a Telegram. Despues, en cada arranque, sigue copiando solo.
"""

import asyncio
import json
import os
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

# Bot
from iqoptionapi.stable_api import IQ_Option
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError

# ===== Datos FIJOS del sistema (iguales para todos los seguidores) =========
TG_API_ID = 31988246
TG_API_HASH = "5095046560758805ca1abf53b6acf2f8"
TG_CANAL = -1003745991669
BOT_USERNAME = "@Copy_zayrex_bot"
# ===========================================================================

PREFIJO = "COPYSENAL "
MAX_DELAY = 15


# ============================================================
#                   MOTOR DEL BOT
# ============================================================
class BotEngine:
    """Maneja IQ Option + Telegram en un hilo aparte con su propio asyncio loop.
    La UI le habla por metodos thread-safe (schedule, provide_*)."""

    def __init__(self, datos_path, sesion_path, log_fn, estado_fn):
        self.datos_path = datos_path
        self.sesion_path = sesion_path
        self.log_fn = log_fn          # callback(msg): UI agrega al log
        self.estado_fn = estado_fn    # callback(nombre): UI cambia de pantalla
        self.loop = None
        self.thread = None
        self.tg = None
        self.api = None
        self.datos = None
        self._code_future = None
        self._pwd_future = None
        self._stop_event = None
        # Candado para serializar las compras en IQ (la API no oficial no
        # es thread-safe, en paralelo puede perder ordenes).
        self._buy_lock = None

    # ------- persistencia ---------
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

    # ------- hilo del bot ---------
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

    # ------- la UI pasa codigo / contrasena al loop --------
    def dar_codigo(self, codigo):
        if self._code_future and not self._code_future.done():
            self.loop.call_soon_threadsafe(self._code_future.set_result, codigo)

    def dar_password(self, pwd):
        if self._pwd_future and not self._pwd_future.done():
            self.loop.call_soon_threadsafe(self._pwd_future.set_result, pwd)

    # ------- conexiones ---------
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
            self.log_fn("ERROR: no se pudo conectar a IQ Option. Revisa el correo/clave.")
            return False
        self.api = api
        self.log_fn("IQ Option OK   |   saldo $%s" % api.get_balance())

        # Carga TODOS los activos disponibles (incluye los nuevos como cripto,
        # memecoins, etc. que no estan en la lista estatica de la libreria).
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
            self.log_fn("Tiempo agotado esperando el codigo.")
            return False

        try:
            await self.tg.sign_in(phone=telefono, code=codigo)
        except SessionPasswordNeededError:
            self._pwd_future = self.loop.create_future()
            self.estado_fn("pedir_password")
            try:
                pwd = await asyncio.wait_for(self._pwd_future, timeout=300)
            except asyncio.TimeoutError:
                self.log_fn("Tiempo agotado esperando la contrasena.")
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

    # ------- nucleo: escuchar y copiar ---------
    async def _escuchar(self):
        @self.tg.on(events.NewMessage(chats=TG_CANAL))
        async def manejador(event):
            texto = event.message.message or ""
            if not texto.startswith(PREFIJO):
                return
            try:
                s = json.loads(texto[len(PREFIJO):])
            except Exception:
                return
            par = s.get("par"); dir_ = s.get("dir"); tf = int(s.get("tf", 1))
            creado = int(s.get("creado", time.time()))
            if not par or dir_ not in ("call", "put"):
                return
            retraso = time.time() - creado
            self.log_fn("SENAL: %s %s M%d  (retraso %.1fs)" % (par, dir_.upper(), tf, retraso))
            if retraso > MAX_DELAY:
                self.log_fn("  DESCARTADA: vieja")
                return

            def _comprar():
                if not self.api.check_connect():
                    self.api.connect(); time.sleep(2)
                return self.api.buy(self.datos["monto"], par, dir_, tf)

            # Serializa las compras: la API no oficial pisa su propio
            # estado interno si dos buy() corren a la vez.
            if self._buy_lock is None:
                self._buy_lock = asyncio.Lock()
            async with self._buy_lock:
                ok, info = await self.loop.run_in_executor(None, _comprar)
            if ok:
                self.log_fn("  COPIADA  $%s  |  id %s" % (self.datos["monto"], info))
            else:
                self.log_fn("  ERROR: " + str(info))

        async def heartbeat():
            while True:
                try:
                    yo = await self.tg.get_me()
                    info = {"nombre": yo.first_name or "",
                            "correo": self.datos["correo"],
                            "cuenta": self.datos["cuenta"],
                            "saldo": self.api.get_balance()}
                    await self.tg.send_message(BOT_USERNAME, "ESTADO " + json.dumps(info))
                except Exception:
                    pass
                await asyncio.sleep(600)

        asyncio.create_task(heartbeat())

        self.estado_fn("escuchando")
        self.log_fn("=== Esperando senales de la cuenta madre ===")
        self._stop_event = asyncio.Event()
        await self._stop_event.wait()

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
    t = TextInput(multiline=False, write_tab=False,
                  size_hint_y=None, height=dp(50), font_size=dp(18), **kw)
    return t


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
        self.layout = BoxLayout(orientation="vertical", padding=dp(20), spacing=dp(10))
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
                text="Escribe el codigo que te llego\na tu app de Telegram:",
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
                         halign="left", valign="top", color=(0.9, 0.9, 0.9, 1))
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
        # Limita a las ultimas 200 lineas
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
            datos_path=os.path.join(ddir, "mis_datos.json"),
            sesion_path=os.path.join(ddir, "seguidor_session"),
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

    # --- callbacks que llaman desde el hilo del bot, deben rebotar al hilo UI ---
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

    # --- acciones que dispara la UI ---
    def ir_a_telegram_telefono(self):
        self.sm.current = "telegram"
        self.tg_s.set_modo("telefono")

    def iniciar_login(self, telefono):
        self.engine.lanzar(self.engine.correr(telefono))

    def _iniciar_sin_telefono(self):
        self.engine.lanzar(self.engine.correr(None))

    def on_stop(self):
        try:
            if self.engine.loop:
                self.engine.loop.call_soon_threadsafe(self.engine.loop.stop)
        except Exception:
            pass


if __name__ == "__main__":
    CopyBotApp().run()
