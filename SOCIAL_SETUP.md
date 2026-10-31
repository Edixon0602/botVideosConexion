# Publicación en YouTube y TikTok desde Telegram

Esta guía explica cómo dejar funcionando los botones **📺 YouTube** y **🎵 TikTok**
del bot. El cliente **no necesita instalar nada ni entregar contraseñas**: solo
autoriza una vez con un código de Google o escaneando un QR de TikTok.

## 1. Qué hace el bot

- Recibe un video por Telegram y lo sube al FTP (como ya hacía).
- A los administradores les muestra botones:
  - `📺 YouTube`
  - `🎵 TikTok`
  - `📲 Publicar en Redes (IG + FB)`
  - `🎵 Agregar a Lista Musical`
  - opciones de autodestrucción
- Al pulsar YouTube o TikTok muestra una vista previa con el texto y permite:
  - confirmar la publicación,
  - editar el texto,
  - cambiar la privacidad,
  - cancelar.
- Al terminar, guarda el enlace/ID y cambia el botón a `✅`.

Los tokens se guardan en `social_tokens.json` (o en `/app/data/social_tokens.json`
dentro de Docker), **fuera de git**, y se renuevan automáticamente.

## 2. Configurar YouTube

### 2.1 Habilitar la API

1. Entra a <https://console.cloud.google.com/apis/library/youtube.googleapis.com?project=pagina-web-462517>
2. Pulsa **Habilitar**.

### 2.2 Crear la credencial correcta

La credencial que ya existe (`client_secret_...apps.googleusercontent.com.json`)
es de tipo **app de escritorio** y **no sirve** para vincular el canal con un
código. Hay que crear una credencial de tipo **TVs and Limited Input devices**.

Si no tienes acceso al proyecto de Google Cloud `pagina-web-462517`, pídele a
quien lo administre que cree la credencial y te pase el Client ID y el Client
Secret.

1. Entra a <https://console.cloud.google.com/apis/credentials?project=pagina-web-462517>
2. **Crear credenciales** → **ID de cliente de OAuth**.
3. Tipo de aplicación: **TVs and Limited Input devices**.
4. Ponle un nombre, por ejemplo `Bot Telegram YouTube`.
5. Copia el **Client ID** y el **Client Secret**.

### 2.3 Variables en `.env`

```env
YOUTUBE_DEVICE_CLIENT_ID=...
YOUTUBE_DEVICE_CLIENT_SECRET=...
YOUTUBE_PRIVACY_STATUS=public
YOUTUBE_CATEGORY_ID=25
YOUTUBE_ADD_SHORTS_HASHTAG=True
```

### 2.4 Pantalla de consentimiento

1. Entra a <https://console.cloud.google.com/apis/credentials/consent?project=pagina-web-462517>
2. Configura la pantalla si es la primera vez.
3. Si la app está en **Testing**, agrega la cuenta de Google del cliente como
   **usuario de prueba**. En ese modo el permiso caduca a los 7 días.
4. Para producción, publica la app. Google puede pedir verificación porque el
   permiso `youtube` es sensible (política de privacidad, dominio, video de
   demostración).

### 2.5 Conectar el canal

1. En Telegram, como administrador, ejecuta `/conectar_youtube`.
2. El bot responde con una URL (`https://www.google.com/device`) y un código.
3. Reenvía ese mensaje al cliente.
4. El cliente abre la URL, escribe el código, inicia sesión con la cuenta dueña
   del canal (si es una cuenta Brand, la selecciona) y acepta el permiso.
   Si Google muestra el aviso de **app no verificada**, debe entrar en
   **Configuración avanzada** y continuar; es normal mientras la app está en
   modo de prueba.
5. El bot detecta la autorización y avisa por Telegram.

### 2.6 Límites de YouTube

- La cuota gratuita permite aproximadamente **6 subidas por día**
  (1.600 unidades por subida y 10.000 diarias). Si se necesita más, hay que
  solicitar aumento de cuota en Google Cloud.
- Un canal sin verificar no puede subir videos de más de 15 minutos.
- En modo Testing, el `refresh_token` caduca a los 7 días. Para producción hay
  que publicar la app o volver a conectar.

## 3. Configurar TikTok

### 3.1 Estado actual de la app

La app de TikTok ya tiene los scopes:

- `user.info.basic`
- `video.upload`

Con `video.upload` el bot puede enviar el video a la **bandeja de entrada** de
TikTok. El cliente recibe una notificación en la app y solo tiene que abrirla,
escribir el texto, elegir la privacidad y confirmar la publicación. Es el modo
que funciona hoy sin trámites adicionales.

Importante: mientras la app no esté auditada, TikTok puede restringir la
visibilidad de lo publicado por API a modo privado. La bandeja de entrada sirve
para probar el flujo, pero para garantizar publicaciones públicas hay que pasar
la auditoría.

Para **publicar directamente sin que el cliente confirme**, la app necesita:

1. El producto **Content Posting API** agregado.
2. El scope **`video.publish`** aprobado.
3. Pasar la **auditoría** de TikTok. Sin auditoría, TikTok solo permite
   publicaciones privadas (`SELF_ONLY`).

La auditoría se solicita aquí:
<https://developers.tiktok.com/application/content-posting-api>

Suele pedir política de privacidad, términos, descripción de la app y un video
de demostración.

### 3.2 Variables en `.env`

```env
TIKTOK_CLIENT_KEY=...
TIKTOK_CLIENT_SECRET=...
TIKTOK_PRIVACY_LEVEL=PUBLIC_TO_EVERYONE
```

No hace falta `redirect_uri` para el flujo QR. Si tu app lo exige, configura
`TIKTOK_QR_REDIRECT_URI`.

### 3.3 Conectar la cuenta

Si la app está en modo **Sandbox**, agrega la cuenta de TikTok del cliente como
**Target User** en el portal de TikTok antes de generar el QR.

1. En Telegram, como administrador, ejecuta `/conectar_tiktok`.
2. El bot genera un **código QR** y lo envía.
3. Reenvía la imagen al cliente.
4. El cliente abre TikTok en el teléfono, toca el escáner de QR y escanea la
   imagen (si la recibe en el teléfono, puede abrirla en una computadora o
   guardarla y usar el escáner de TikTok).
5. El cliente acepta el permiso.
6. El bot detecta la autorización y avisa por Telegram.

### 3.4 Límites de TikTok

- El `access_token` dura 24 horas; el bot lo renueva solo con el `refresh_token`.
- El `refresh_token` dura 365 días y puede rotar; el bot guarda el nuevo.
- Hay límites diarios de publicaciones y de videos pendientes en la bandeja.
- El video debe ser MP4/MOV/WebM, H.264 recomendado, 23–60 FPS, 360–4096 px,
  máximo 4 GB y 10 minutos por API.
- Si la app no está auditada, solo se permite privacidad `SELF_ONLY`.

## Cuentas esperadas (opcional)

Puedes definir en `.env` los datos de la cuenta que debe conectar el cliente.
El bot los mostrará en `/conectar_youtube`, `/conectar_tiktok` y `/redes`:

```env
YOUTUBE_CHANNEL_HANDLE=@MiCanal
YOUTUBE_ACCOUNT_EMAIL=cuenta@gmail.com
TIKTOK_USERNAME=@MiUsuario
```

Estos valores no se guardan en el repositorio; van solo en el `.env` del
servidor.

## 4. Comandos de Telegram

Solo para administradores:

| Comando | Función |
|---|---|
| `/conectar_youtube` | Genera el código para vincular el canal |
| `/conectar_tiktok` | Genera el QR para vincular la cuenta |
| `/redes` | Muestra el estado y el modo de cada red |

## 5. Probar sin publicar

En `.env`:

```env
SOCIAL_DRY_RUN=True
```

Con eso, al pulsar YouTube o TikTok el bot valida la conexión y el video, pero
no publica nada. Cuando todo esté probado, cambia a `False`.

## 6. Despliegue

### Local

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe main.py
```

### Docker

```bash
docker compose up -d --build
```

El `docker-compose.yml` monta `./botVideosConexion/data` en `/app/data` y define
`SOCIAL_TOKENS_FILE=/app/data/social_tokens.json`. La carpeta se crea sola.

Si el bot corre en **Render**, el sistema de archivos es efímero: monta un disco
persistente en `/app/data` y define
`SOCIAL_TOKENS_FILE=/app/data/social_tokens.json`. De lo contrario, los tokens
se pierden al reiniciar y habrá que volver a conectar las cuentas.

## 7. Problemas frecuentes

| Síntoma | Causa / solución |
|---|---|
| `invalid_client` al generar código de YouTube | La credencial no es de tipo **TVs and Limited Input devices**. Crea una nueva. |
| YouTube conecta pero no sube | Falta habilitar **YouTube Data API v3** en el proyecto. |
| `invalid_grant` en YouTube | El permiso venció o fue revocado. Ejecuta `/conectar_youtube` otra vez. |
| `invalid_scope` en TikTok | La app no tiene aprobado `video.upload` o `video.publish`. Revisa **Scopes** en el portal. |
| TikTok conecta pero no publica | Revisa `/redes`; si el modo es **Bandeja de entrada**, el cliente debe confirmar en la app. |
| TikTok solo permite `SELF_ONLY` | La app no está auditada. Es el comportamiento normal hasta pasar la auditoría. |
| Error de formato/duración en TikTok | Convierte el video a MP4 H.264, 23–60 FPS, 360–4096 px, máximo 10 minutos. |

## 8. Descarga de videos de YouTube

Si al enviar un enlace de YouTube aparece `403 Forbidden` o
`Sign in to confirm you're not a bot`, revisa
[`YOUTUBE_DOWNLOAD.md`](YOUTUBE_DOWNLOAD.md). Ahí están los pasos para
actualizar cookies, instalar Deno/EJS y configurar un proxy.

## 9. Seguridad

- No subas `.env` ni `social_tokens.json` al repositorio.
- El `client_secret` de TikTok que se compartió por chat conviene rotarlo después
  de terminar la configuración.
- El bot nunca pide contraseñas: solo usa los flujos oficiales de Google y TikTok.
