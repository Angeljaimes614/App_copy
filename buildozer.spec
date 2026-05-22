[app]
# Nombre visible y package
title = Bot Copy
package.name = copybot
package.domain = com.zayrex.copybot

# Codigo fuente: esta misma carpeta
source.dir = .
source.include_exts = py,kv,json,pem,png,jpg,jpeg,atlas,ttf,sql
# Incluye paquetes locales: iqoptionapi (broker) + core (cimiento) + migrations
source.include_patterns = iqoptionapi/*,iqoptionapi/**/*,core/*,core/**/*,migrations/*

version = 0.3.4

# Dependencias de Python
requirements = python3,kivy,requests,urllib3,charset-normalizer,certifi,idna,websocket-client,colorama,python-dateutil,six,telethon,pyaes,rsa,pyasn1

orientation = portrait
fullscreen = 0

# Permisos de Android (FOREGROUND_SERVICE_DATA_SYNC = Android 14+)
android.permissions = INTERNET, WAKE_LOCK, FOREGROUND_SERVICE, FOREGROUND_SERVICE_DATA_SYNC, POST_NOTIFICATIONS

# Foreground service: corre el engine en background indefinidamente
# Formato: NOMBRE:script.py:foreground
services = Copybot:service_copybot.py:foreground

# Niveles de API
android.api = 31
android.minapi = 21
android.ndk_api = 21

# Arquitecturas (arm64 cubre la mayoria de celulares modernos)
android.archs = arm64-v8a, armeabi-v7a

# Mantener datos del usuario
android.allow_backup = True

# Icono y splash (opcional, dejar comentado por ahora)
# icon.filename = icon.png

[buildozer]
log_level = 2
warn_on_root = 1
