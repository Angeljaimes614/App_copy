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

version = 0.2

# Dependencias de Python
requirements = python3,kivy,requests,urllib3,charset-normalizer,certifi,idna,websocket-client,colorama,python-dateutil,six,telethon,pyaes,rsa,pyasn1

orientation = portrait
fullscreen = 0

# Permisos de Android
android.permissions = INTERNET, WAKE_LOCK, FOREGROUND_SERVICE

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
