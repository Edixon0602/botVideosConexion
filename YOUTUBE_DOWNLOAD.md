# Descarga de videos de YouTube

## Qué significaba el error

El mensaje:

```
Error API: HTTP Error 403: Forbidden
Error yt-dlp: Sign in to confirm you're not a bot
```

significa que fallaron los dos caminos:

1. **La API externa `loader.to`** respondió `403 Forbidden`. Ese servicio es poco
   confiable y quedó **desactivado por defecto**.
2. **yt-dlp** no pudo resolver la descarga porque:
   - las cookies de `cookies.txt` ya no son válidas (Google las rotó), y/o
   - al servidor le faltaba **Deno + EJS** para resolver los desafíos JavaScript
     de YouTube, y/o
   - la IP del VPS está marcada por YouTube.

## Lo que se cambió

- `yt-dlp` ahora se instala como `yt-dlp[default]`, que incluye `yt-dlp-ejs`.
- El `Dockerfile` instala **Deno**.
- La descarga intenta primero **yt-dlp con cookies**; si falla, reintenta
  **sin cookies**.
- La API externa `loader.to` ya no se intenta por defecto. Solo se usa si
  `YOUTUBE_EXTERNAL_API_ENABLED=True`.
- `cookies.txt` se monta como volumen en Docker, así se puede actualizar sin
  reconstruir la imagen.
- Se prefieren formatos H.264 (avc1) para que el video sea compatible con el VDO
  Panel y TikTok.

## Qué tienes que hacer

### 1. Reconstruir la imagen

```bash
docker compose up -d --build
```

Esto instala Deno y `yt-dlp[default]`.

### 2. Actualizar las cookies

Las cookies actuales fueron exportadas el 18/10 y Google ya las rotó. Exporta
cookies nuevas:

1. Inicia sesión en YouTube en un navegador.
2. Usa una extensión como **Get cookies.txt LOCALLY** para exportar las cookies
   de `youtube.com` en formato Netscape.
3. Guarda el archivo como `cookies.txt` en la raíz del proyecto.
4. El `docker-compose.yml` lo monta en `/app/cookies.txt`, así que el contenedor
   lo lee sin reconstruir.

Si no tienes acceso a la cuenta, puedes exportar cookies de una cuenta de Google
secundaria; para descargar videos públicos no hace falta la cuenta del canal.

Nota: yt-dlp ya no soporta el login OAuth2 para YouTube. El método soportado es
`cookies.txt` o `--cookies-from-browser` (este último no sirve en un servidor
sin navegador).

### 3. Si la IP del VPS está bloqueada

YouTube puede pedir verificación incluso con cookies nuevas. En ese caso
configura un proxy residencial en `.env`:

```env
YOUTUBE_PROXY=http://usuario:clave@host:puerto
```

También puedes usar un proxy SOCKS5:

```env
YOUTUBE_PROXY=socks5://usuario:clave@host:puerto
```

### 4. Variables opcionales

```env
# Ruta de las cookies (por defecto cookies.txt o /app/cookies.txt)
# YOUTUBE_COOKIES_FILE=cookies.txt

# Proxy para las descargas
# YOUTUBE_PROXY=http://usuario:clave@host:puerto

# Ruta de Deno si no está en el PATH (en Docker está en /usr/local/bin/deno)
# YOUTUBE_DENO_PATH=/usr/local/bin/deno

# La API externa loader.to queda desactivada por defecto
YOUTUBE_EXTERNAL_API_ENABLED=False
```

## Cómo probar

Desde el servidor:

```bash
docker compose exec bot-videos sh -lc 'deno --version && python -c "import yt_dlp, yt_dlp_ejs; print(yt_dlp.version.__version__)"'
```

Luego envía un enlace de YouTube al bot. En los logs deberías ver algo como:

```
[YouTube] Deno disponible: /usr/local/bin/deno
[YouTube] Intentando descarga con cookies: ...
[youtube] [jsc:deno] Solving JS challenges using deno
[YouTube] Descarga exitosa (con cookies): ...
```

Si ves `Sign in to confirm`, actualiza `cookies.txt` o configura
`YOUTUBE_PROXY`. Si ves `JavaScript runtime`, reconstruye la imagen.

## Notas

- `cookies.txt` contiene cookies de sesión de Google. No lo subas a un repo
  público ni lo compartas. Si el repo es público, sácalo de git y usa un volumen
  o un secreto:

  ```bash
  git rm --cached cookies.txt
  ```

  Después rota las cookies (cierra sesión en los dispositivos o cambia la
  contraseña) y exporta unas nuevas.

  Si quitas `cookies.txt` de git, tendrás que copiarlo manualmente al servidor
  (por ejemplo con `scp`) o montarlo como secreto. El volumen de
  `docker-compose.yml` lo toma del host, no del repositorio.
- El bot intenta primero con cookies y luego sin cookies. Si la IP del VPS está
  bloqueada, la segunda opción también puede fallar; ahí es donde entra el proxy.
- La API externa `loader.to` ya no es el camino principal. Si quieres volver a
  usarla, activa `YOUTUBE_EXTERNAL_API_ENABLED=True`, pero puede seguir dando 403.
