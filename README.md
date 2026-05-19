# Bot Copy — App para Android (en desarrollo)

App con interfaz gráfica que reemplazará al método Termux para los seguidores
sin PC. El mismo código funciona en Windows/Mac/Linux (para probar) y se
compila a `.apk` para Android.

```
app_android/
  main.py                       ← la app (Kivy)
  buildozer.spec                ← config para compilar Android
  iqoptionapi/                  ← API de IQ Option (ya corregida)
  requirements.txt              ← deps para probar en PC
  .github/workflows/build_apk.yml ← GitHub Actions compila el APK gratis
```

---

## Probarla en PC (rápido — antes del APK)

Ya que el APK tarda en compilar la primera vez, conviene probar en PC primero
para ver el flujo de pantallas.

```
cd app_android
pip install -r requirements.txt
python main.py
```

Te debe abrir una ventana con la pantalla de "Tus datos". Si funciona el
flujo aquí, el APK también funcionará.

---

## Compilar el APK con GitHub Actions (gratis, sin Linux propio)

GitHub compila el APK en sus servidores cuando subes el código. **Una sola vez:**

### 1. Crear cuenta de GitHub
- Entra a https://github.com → "Sign up" → cuenta gratis.

### 2. Crear un repositorio
- Click en **+** (arriba derecha) → **New repository**.
- Nombre cualquiera (ej. `copybot`).
- Déjalo **Private** (privado — no quieres que todos vean tu código).
- "Create repository".

### 3. Subir el código del proyecto
La forma más fácil sin conocer git: usar **GitHub Desktop** (app gratis).
- https://desktop.github.com/ → instalar → login con tu cuenta.
- "Add an Existing Repository from your Hard Drive…"
- Elige `D:\copy_iq_binarias\copytrade\app_android` (toda esta carpeta).
- Click "Publish repository" → confirma → sube.

### 4. Esperar a que compile
- En GitHub.com, abre tu repositorio → pestaña **"Actions"**.
- Verás un job corriendo (la primera vez tarda **30–50 min**: descarga el SDK
  de Android y Buildozer compila todo). Las siguientes builds son rápidas
  (~5 min) porque usa cache.

### 5. Bajar el APK
- Cuando el job termine con check verde, entra al job → al final dice
  **"Artifacts" → "copybot-apk"** → bájalo (es un .zip que adentro tiene el `.apk`).

### 6. Instalar en el celular
- Pásale el `.apk` al celular (Telegram, correo, USB).
- En el celular: activar *"Instalar apps desconocidas"* para el navegador o
  gestor de archivos donde recibas el APK (Ajustes → Seguridad → Instalar
  apps desconocidas).
- Abrir el `.apk` → "Instalar".
- Listo: ya hay un icono "Bot Copy" en el celular.

---

## Estado actual

| | |
|---|---|
| Código Kivy | ✅ listo (probado: compila) |
| UI multi-pantalla | ✅ (setup, login Telegram, principal) |
| Buildozer.spec | ✅ |
| GitHub Actions | ✅ |
| Compilado y probado en celular real | ⏳ pendiente |
| Servicio en segundo plano (Android no la mate) | ⏳ futuro |

Esto es **v0.1**. Después de probar el APK habrá bugs que arreglar
(pantallas que no se ven bien, errores de login, etc.) — iteramos.
