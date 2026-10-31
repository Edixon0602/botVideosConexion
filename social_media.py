"""
Integración de YouTube y TikTok para el bot de Telegram.

Objetivos:
- Conectar las cuentas sin pedir contraseñas y sin que el cliente tenga que
  ejecutar scripts: YouTube usa el flujo de código de dispositivo de Google y
  TikTok usa autorización por código QR.
- Guardar los tokens en social_tokens.json (fuera de git) y renovarlos solos.
- Publicar videos desde el archivo local o desde la copia del FTP.

Este módulo es intencionalmente independiente de Pyrogram/Flask para poder
probarlo y reutilizarlo con facilidad.
"""

import json
import logging
import math
import mimetypes
import os
import secrets
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuración y almacenamiento de tokens
# ---------------------------------------------------------------------------

TOKENS_FILE = os.getenv("SOCIAL_TOKENS_FILE", "social_tokens.json")
_tokens_lock = threading.RLock()
_refresh_locks = {
    "youtube": threading.Lock(),
    "tiktok": threading.Lock(),
}

YOUTUBE_SCOPE = "https://www.googleapis.com/auth/youtube"
YOUTUBE_DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
YOUTUBE_TOKEN_URL = "https://oauth2.googleapis.com/token"
YOUTUBE_CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
YOUTUBE_UPLOAD_URL = (
    "https://www.googleapis.com/upload/youtube/v3/videos"
    "?uploadType=resumable&part=snippet,status"
)

TIKTOK_QRCODE_URL = "https://open.tiktokapis.com/v2/oauth/get_qrcode/"
TIKTOK_CHECK_QRCODE_URL = "https://open.tiktokapis.com/v2/oauth/check_qrcode/"
TIKTOK_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
TIKTOK_USER_INFO_URL = "https://open.tiktokapis.com/v2/user/info/"
TIKTOK_CREATOR_INFO_URL = (
    "https://open.tiktokapis.com/v2/post/publish/creator_info/query/"
)
TIKTOK_DIRECT_INIT_URL = (
    "https://open.tiktokapis.com/v2/post/publish/video/init/"
)
TIKTOK_INBOX_INIT_URL = (
    "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
)
TIKTOK_STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"

TIKTOK_SCOPE_CANDIDATES = (
    "video.publish,video.upload,user.info.basic",
    "video.publish,user.info.basic",
    "video.upload,user.info.basic",
    "video.publish,video.upload",
    "video.publish",
    "video.upload",
    "user.info.basic",
)

TIKTOK_PRIVACY_LABELS = {
    "PUBLIC_TO_EVERYONE": "Público",
    "MUTUAL_FOLLOW_FRIENDS": "Amigos",
    "FOLLOWER_OF_CREATOR": "Seguidores",
    "SELF_ONLY": "Solo yo",
}


class SocialError(Exception):
    """Error general de integración con redes sociales."""


class SocialAuthError(SocialError):
    """La cuenta no está conectada o el token ya no es válido."""


class SocialPublishError(SocialError):
    """La plataforma rechazó la publicación."""


class SocialHTTPError(SocialError):
    def __init__(self, status, body, headers, url):
        self.status = status
        self.body = body
        self.headers = headers or {}
        self.url = url
        text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
        super().__init__(f"HTTP {status} en {url}: {text[:400]}")

    def json(self):
        try:
            if isinstance(self.body, bytes):
                return json.loads(self.body.decode("utf-8", "replace"))
            return json.loads(str(self.body))
        except Exception:
            return {}


def _env(name, default=""):
    value = os.getenv(name)
    return default if value is None else value


def _env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "si", "sí", "on")


def _atomic_write_json(path, data):
    folder = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(folder, exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def load_tokens():
    with _tokens_lock:
        if not os.path.exists(TOKENS_FILE):
            return {}
        try:
            with open(TOKENS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.warning("No se pudo leer %s: %s", TOKENS_FILE, e)
            return {}


def save_tokens(data):
    with _tokens_lock:
        _atomic_write_json(TOKENS_FILE, data)


def update_tokens(platform, values):
    with _tokens_lock:
        data = load_tokens()
        current = data.get(platform, {})
        if not isinstance(current, dict):
            current = {}
        current.update(values or {})
        data[platform] = current
        save_tokens(data)
        return current


def disconnect(platform):
    with _tokens_lock:
        data = load_tokens()
        data.pop(platform, None)
        save_tokens(data)


def _token_is_fresh(platform, margin=180):
    token = load_tokens().get(platform, {})
    if not token.get("access_token"):
        return False
    try:
        return float(token.get("expires_at", 0)) > time.time() + margin
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _parse_json(raw):
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return {"raw": raw.decode("utf-8", "replace")}


def _http_json(url, data=None, headers=None, method=None, timeout=60,
               allow_http_errors=False):
    req = urllib.request.Request(
        url, data=data, headers=headers or {}, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _parse_json(resp.read()), dict(resp.headers)
    except urllib.error.HTTPError as e:
        raw = e.read()
        if allow_http_errors:
            return e.code, _parse_json(raw), dict(e.headers or {})
        raise SocialHTTPError(e.code, raw, dict(e.headers or {}), url)
    except urllib.error.URLError as e:
        raise SocialError(f"No se pudo conectar con {url}: {e.reason}")


def _google_error_message(body, status=None):
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            message = err.get("message") or err.get("error_description")
            if message:
                return str(message)
        if isinstance(err, str) and err:
            desc = body.get("error_description")
            return f"{err}: {desc}" if desc else err
        if body.get("error_description"):
            return str(body["error_description"])
    return f"Error de Google (HTTP {status})" if status else "Error de Google"


def _tiktok_error_message(code, message=""):
    messages = {
        "invalid_scope": (
            "La app de TikTok no tiene aprobado ese permiso. Hay que solicitar "
            "video.publish o video.upload en el portal de TikTok."
        ),
        "access_token_invalid": (
            "El permiso de TikTok expiró o fue revocado. Vuelve a conectar la cuenta."
        ),
        "scope_not_authorized": (
            "La cuenta no autorizó el permiso necesario. Vuelve a conectar la cuenta."
        ),
        "spam_risk_too_many_posts": (
            "TikTok alcanzó el límite diario de publicaciones de esta cuenta. "
            "Intenta mañana o publica manualmente desde la app."
        ),
        "spam_risk_too_many_pending_share": (
            "Hay demasiados videos pendientes en la bandeja de entrada de TikTok. "
            "Pide al cliente que abra TikTok y publique o descarte los pendientes."
        ),
        "spam_risk_user_banned_from_posting": (
            "TikTok bloqueó temporalmente la publicación de esta cuenta."
        ),
        "reached_active_user_cap": (
            "La app alcanzó su límite diario de usuarios publicando. Intenta más tarde."
        ),
        "unaudited_client_can_only_post_to_private_accounts": (
            "La app de TikTok no está auditada: solo puede publicar en modo privado. "
            "Para publicar en público hay que pasar la auditoría de TikTok."
        ),
        "privacy_level_option_mismatch": (
            "La opción de privacidad elegida no está permitida para esta cuenta. "
            "Elige otra opción."
        ),
        "rate_limit_exceeded": (
            "TikTok limitó la cantidad de solicitudes. Espera un minuto y reintenta."
        ),
        "url_ownership_unverified": (
            "TikTok exige verificar el dominio para descargar videos por URL."
        ),
        "invalid_param": "TikTok rechazó los datos del video.",
    }
    base = messages.get(code)
    if base:
        return f"{base} ({message})" if message else base
    return f"TikTok rechazó la operación: {code or 'error'}. {message}".strip()


def _raise_tiktok_body(body, status=None):
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        code = err.get("code") or ""
        if code and code != "ok":
            raise SocialPublishError(
                _tiktok_error_message(code, err.get("message") or "")
            )
    elif isinstance(err, str) and err:
        desc = body.get("error_description") or ""
        raise SocialAuthError(f"TikTok: {desc or err}")
    if status and status >= 400:
        raise SocialError(f"TikTok respondió HTTP {status}")


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------


def youtube_config():
    client_id = _env("YOUTUBE_DEVICE_CLIENT_ID") or _env("YOUTUBE_CLIENT_ID")
    client_secret = _env("YOUTUBE_DEVICE_CLIENT_SECRET") or _env(
        "YOUTUBE_CLIENT_SECRET"
    )
    if not client_id or not client_secret:
        raise SocialAuthError(
            "Faltan YOUTUBE_DEVICE_CLIENT_ID y YOUTUBE_DEVICE_CLIENT_SECRET en .env."
        )
    return client_id, client_secret


def youtube_device_start():
    """Solicita un código para vincular YouTube sin redirect URI."""
    client_id, _ = youtube_config()
    data = urllib.parse.urlencode(
        {"client_id": client_id, "scope": YOUTUBE_SCOPE}
    ).encode()
    status, body, _ = _http_json(
        YOUTUBE_DEVICE_CODE_URL,
        data,
        {"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
        allow_http_errors=True,
    )
    if status >= 400 or body.get("error"):
        msg = _google_error_message(body, status)
        if body.get("error") == "invalid_client":
            msg = (
                "El client_id de YouTube no es de tipo "
                "'TVs and Limited Input devices'. Crea ese tipo de credencial en "
                "Google Cloud y usa YOUTUBE_DEVICE_CLIENT_ID/SECRET."
            )
        raise SocialAuthError(msg)
    if not body.get("device_code") or not body.get("user_code"):
        raise SocialAuthError("Google no devolvió el código de vinculación.")
    return body


def youtube_device_poll(device_code, interval=5, expires_in=1800):
    """Espera a que el cliente autorice el código y guarda el refresh token."""
    client_id, client_secret = youtube_config()
    deadline = time.time() + max(60, int(expires_in or 1800))
    wait = max(5, int(interval or 5))
    while time.time() < deadline:
        time.sleep(wait)
        data = urllib.parse.urlencode(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            }
        ).encode()
        status, body, _ = _http_json(
            YOUTUBE_TOKEN_URL,
            data,
            {"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
            allow_http_errors=True,
        )
        if status < 400 and body.get("access_token"):
            access_token = body["access_token"]
            refresh_token = body.get("refresh_token") or ""
            expires_in_token = int(body.get("expires_in") or 3600)
            update_tokens(
                "youtube",
                {
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "expires_at": time.time() + expires_in_token,
                    "scope": body.get("scope", YOUTUBE_SCOPE),
                    "connected_at": int(time.time()),
                },
            )
            info = {}
            channel_error = ""
            try:
                info = youtube_channel_info(access_token)
                update_tokens("youtube", info)
            except Exception as e:
                # El token ya quedó guardado; el error puede ser que la API de
                # YouTube no esté habilitada en el proyecto de Google.
                channel_error = str(e)
                logger.warning("YouTube conectado, pero no se pudo leer el canal: %s", e)
            return {"tokens": body, "channel": info, "channel_error": channel_error}

        error = body.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            wait += 5
            continue
        if error == "access_denied":
            raise SocialAuthError("El cliente rechazó el permiso de YouTube.")
        if error == "expired_token":
            raise SocialAuthError("El código de YouTube expiró. Genera uno nuevo.")
        raise SocialAuthError(_google_error_message(body, status))
    raise SocialAuthError("El código de YouTube expiró. Genera uno nuevo.")


def youtube_access_token(force=False):
    with _refresh_locks["youtube"]:
        return _youtube_access_token_locked(force)


def _youtube_access_token_locked(force=False):
    token = load_tokens().get("youtube", {})
    if not force and _token_is_fresh("youtube"):
        return token["access_token"]
    refresh_token = token.get("refresh_token") or _env("YOUTUBE_REFRESH_TOKEN")
    if not refresh_token:
        # Modo de compatibilidad: access token corto sin refresh token.
        access_only = _env("YOUTUBE_ACCESS_TOKEN")
        if access_only:
            if (
                token.get("access_token") == access_only
                and float(token.get("expires_at", 0) or 0) <= time.time()
            ):
                raise SocialAuthError(
                    "El access token de YouTube expiró. Actualízalo en .env o "
                    "conecta el canal con /conectar_youtube."
                )
            update_tokens(
                "youtube",
                {
                    "access_token": access_only,
                    "refresh_token": "",
                    "expires_at": time.time() + 3600,
                    "scope": token.get("scope", YOUTUBE_SCOPE),
                },
            )
            return access_only
        raise SocialAuthError(
            "YouTube no está conectado. Usa /conectar_youtube para vincular el canal."
        )
    client_id, client_secret = youtube_config()
    data = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode()
    status, body, _ = _http_json(
        YOUTUBE_TOKEN_URL,
        data,
        {"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
        allow_http_errors=True,
    )
    if status >= 400 or not body.get("access_token"):
        msg = _google_error_message(body, status)
        if body.get("error") == "invalid_grant":
            msg = (
                "El permiso de YouTube venció o fue revocado. "
                "Vuelve a conectar el canal con /conectar_youtube."
            )
        raise SocialAuthError(msg)
    update_tokens(
        "youtube",
        {
            "access_token": body["access_token"],
            "refresh_token": refresh_token,
            "expires_at": time.time() + int(body.get("expires_in") or 3600),
            "scope": body.get("scope", token.get("scope", YOUTUBE_SCOPE)),
        },
    )
    return body["access_token"]


def youtube_channel_info(access_token=None):
    token = access_token or youtube_access_token()
    url = YOUTUBE_CHANNELS_URL + "?" + urllib.parse.urlencode(
        {"part": "snippet", "mine": "true"}
    )
    status, body, _ = _http_json(
        url,
        headers={"Authorization": f"Bearer {token}"},
        allow_http_errors=True,
    )
    if status >= 400:
        raise SocialAuthError(_google_error_message(body, status))
    items = body.get("items") or []
    if not items:
        raise SocialAuthError(
            "La cuenta de Google autorizada no tiene ningún canal de YouTube."
        )
    channel = items[0]
    snippet = channel.get("snippet", {})
    return {
        "channel_id": channel.get("id", ""),
        "channel_title": snippet.get("title", ""),
        "channel_thumbnail": (snippet.get("thumbnails", {}).get("default", {}) or {}).get(
            "url", ""
        ),
    }


def _youtube_upload_error(body, status=None):
    """Traduce los errores más comunes de la subida de YouTube."""
    err = body.get("error") if isinstance(body, dict) else {}
    message = ""
    reason = ""
    if isinstance(err, dict):
        message = err.get("message") or ""
        details = err.get("errors") or []
        if details and isinstance(details, list):
            reason = details[0].get("reason") or ""
    mapping = {
        "quotaExceeded": (
            "Se agotó la cuota diaria de YouTube. Intenta mañana o solicita "
            "un aumento de cuota en Google Cloud."
        ),
        "uploadLimitExceeded": (
            "El canal alcanzó el límite de subidas de YouTube. Intenta más tarde."
        ),
        "invalidTitle": "El título del video no es válido para YouTube.",
        "invalidDescription": "La descripción no es válida para YouTube.",
        "invalidCategoryId": "La categoría de YouTube configurada no es válida.",
        "youtubeSignupRequired": (
            "La cuenta de Google no tiene un canal de YouTube asociado."
        ),
        "authError": (
            "El permiso de YouTube venció o no es válido. Vuelve a conectar "
            "el canal con /conectar_youtube."
        ),
        "forbidden": (
            "La cuenta no tiene permiso para subir videos a este canal."
        ),
    }
    return mapping.get(reason) or message or _google_error_message(body, status)


def _clean_youtube_title(text):
    title = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    title = title.replace("<", " ").replace(">", " ").strip()
    if not title:
        title = "Video"
    return title[:100]


def _probe_video(path):
    """Devuelve duración, tamaño, códec, fps y resolución si ffprobe existe."""
    try:
        output = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                path,
            ],
            timeout=60,
            stderr=subprocess.DEVNULL,
        )
        data = json.loads(output.decode("utf-8", "replace"))
    except Exception:
        return {}
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    try:
        duration = float(
            data.get("format", {}).get("duration")
            or video.get("duration")
            or 0
        )
    except (TypeError, ValueError):
        duration = 0
    fps = 0.0
    rate = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"
    try:
        if "/" in rate:
            num, den = rate.split("/", 1)
            fps = float(num) / float(den) if float(den) else 0
        else:
            fps = float(rate)
    except (TypeError, ValueError, ZeroDivisionError):
        fps = 0.0
    return {
        "duration": duration,
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "codec": video.get("codec_name") or "",
        "audio_codec": audio.get("codec_name") or "",
        "fps": fps,
        "format": (data.get("format", {}).get("format_name") or "").lower(),
    }


def _youtube_upload_chunk(upload_url, chunk, start, end, total, mime_type):
    req = urllib.request.Request(
        upload_url,
        data=chunk,
        method="PUT",
        headers={
            "Content-Type": mime_type,
            "Content-Length": str(len(chunk)),
            "Content-Range": f"bytes {start}-{end}/{total}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})


def publish_video_to_youtube(local_path, caption="", title=None, description=None,
                             privacy=None, category_id=None, dry_run=False):
    """Sube un video al canal de YouTube conectado."""
    if not local_path or not os.path.exists(local_path):
        raise SocialPublishError("No se encontró el archivo de video para YouTube.")
    size = os.path.getsize(local_path)
    if size <= 0:
        raise SocialPublishError("El archivo de video está vacío.")

    privacy = (privacy or _env("YOUTUBE_PRIVACY_STATUS", "public")).strip().lower()
    if privacy not in ("public", "unlisted", "private"):
        privacy = "public"
    category_id = str(category_id or _env("YOUTUBE_CATEGORY_ID", "25"))
    title = _clean_youtube_title(title or caption or os.path.basename(local_path))
    description = description if description is not None else (caption or "")
    description = (description or "").strip()[:5000]

    # Ayuda a que YouTube lo detecte como Short si el video es vertical y corto.
    if _env_bool("YOUTUBE_ADD_SHORTS_HASHTAG", True):
        info = _probe_video(local_path)
        if (
            info
            and info.get("height", 0) > info.get("width", 0)
            and 0 < info.get("duration", 0) <= 180
            and "#shorts" not in description.lower()
        ):
            description = (description + "\n\n#Shorts").strip()[:5000]

    access_token = youtube_access_token()
    if dry_run:
        info = youtube_channel_info(access_token)
        return {
            "success": True,
            "dry_run": True,
            "video_id": "",
            "url": "",
            "title": title,
            "privacy": privacy,
            "channel": info.get("channel_title", ""),
        }

    mime_type = mimetypes.guess_type(local_path)[0] or "video/*"
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }
    status, response, headers = _http_json(
        YOUTUBE_UPLOAD_URL,
        json.dumps(body).encode("utf-8"),
        {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(size),
            "X-Upload-Content-Type": mime_type,
        },
        method="POST",
        timeout=60,
        allow_http_errors=True,
    )
    if status >= 400:
        raise SocialPublishError(_youtube_upload_error(response, status))
    upload_url = headers.get("Location") or headers.get("location")
    if not upload_url:
        raise SocialPublishError("YouTube no devolvió la URL de subida.")

    chunk_size = 8 * 1024 * 1024
    video_id = ""
    with open(local_path, "rb") as f:
        pos = 0
        retries = 0
        while pos < size:
            f.seek(pos)
            chunk = f.read(chunk_size)
            if not chunk:
                break
            start = pos
            end = pos + len(chunk) - 1
            chunk_status, chunk_body, chunk_headers = _youtube_upload_chunk(
                upload_url, chunk, start, end, size, mime_type
            )
            if chunk_status in (200, 201):
                data = _parse_json(chunk_body)
                video_id = data.get("id", "")
                pos = end + 1
                break
            if chunk_status == 308:
                range_header = (
                    chunk_headers.get("Range")
                    or chunk_headers.get("range")
                    or ""
                )
                if range_header.startswith("bytes="):
                    try:
                        uploaded = int(range_header.split("-", 1)[1]) + 1
                        pos = uploaded
                        retries = 0
                        continue
                    except (ValueError, IndexError):
                        pass
                # Sin Range: asumir que el chunk fue aceptado.
                pos = end + 1
                retries = 0
                continue
            if chunk_status in (500, 502, 503, 504) and retries < 3:
                retries += 1
                time.sleep(2 * retries)
                continue
            data = _parse_json(chunk_body)
            raise SocialPublishError(_youtube_upload_error(data, chunk_status))

    if not video_id:
        raise SocialPublishError(
            "YouTube no confirmó el ID del video. Revisa la conexión e intenta de nuevo."
        )
    return {
        "success": True,
        "dry_run": False,
        "video_id": video_id,
        "url": f"https://youtu.be/{video_id}",
        "title": title,
        "privacy": privacy,
    }


# ---------------------------------------------------------------------------
# TikTok
# ---------------------------------------------------------------------------


def tiktok_config():
    client_key = _env("TIKTOK_CLIENT_KEY")
    client_secret = _env("TIKTOK_CLIENT_SECRET")
    if not client_key or not client_secret:
        raise SocialAuthError(
            "Faltan TIKTOK_CLIENT_KEY y TIKTOK_CLIENT_SECRET en .env."
        )
    return client_key, client_secret


def _replace_query_param(url, key, value):
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
    query[key] = [value]
    new_query = urllib.parse.urlencode(query, doseq=True)
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, new_query, parts.fragment)
    )


def _make_qr_png(text, path):
    try:
        import qrcode
    except Exception as e:
        raise SocialError(
            "Falta la librería qrcode. Instala con: pip install 'qrcode[pil]'"
        ) from e
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(text)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    image.save(path)
    return path


def tiktok_qr_start():
    """Genera un QR de autorización. Prueba los scopes disponibles en la app."""
    client_key, _ = tiktok_config()
    last_error = None
    for scope in TIKTOK_SCOPE_CANDIDATES:
        data = urllib.parse.urlencode(
            {"client_key": client_key, "scope": scope, "state": secrets.token_urlsafe(12)}
        ).encode()
        status, body, _ = _http_json(
            TIKTOK_QRCODE_URL,
            data,
            {"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
            allow_http_errors=True,
        )
        if status >= 400:
            err = body.get("error")
            code = err.get("code") if isinstance(err, dict) else str(err or "")
            last_error = _tiktok_error_message(
                code, body.get("error_description") or ""
            )
            continue
        if body.get("error"):
            code = body.get("error")
            desc = body.get("error_description") or ""
            if code == "invalid_scope":
                last_error = _tiktok_error_message("invalid_scope", desc)
                continue
            raise SocialAuthError(f"TikTok: {desc or code}")
        scan_url = body.get("scan_qrcode_url")
        token = body.get("token")
        if not scan_url or not token:
            last_error = "TikTok no devolvió el código QR."
            continue

        client_ticket = secrets.token_urlsafe(24)
        scan_url = _replace_query_param(scan_url, "client_ticket", client_ticket)
        qr_path = os.path.join(
            tempfile.gettempdir(), f"tiktok_qr_{token[:10]}.png"
        )
        _make_qr_png(scan_url, qr_path)
        return {
            "scan_url": scan_url,
            "token": token,
            "client_ticket": client_ticket,
            "qr_path": qr_path,
            "scope": scope,
        }
    raise SocialAuthError(
        last_error
        or "No se pudo generar el QR de TikTok. Revisa los permisos de la app."
    )


def _extract_code(value):
    if not value:
        return ""
    if "://" in value or value.startswith("http"):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(value).query)
        return (query.get("code") or [""])[0]
    return value.strip()


def _tiktok_exchange_code(code):
    client_key, client_secret = tiktok_config()
    base_params = {
        "client_key": client_key,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
    }
    status, body, _ = _http_json(
        TIKTOK_TOKEN_URL,
        urllib.parse.urlencode(base_params).encode(),
        {"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
        allow_http_errors=True,
    )
    if status < 400 and body.get("access_token"):
        return body

    # Algunas apps de TikTok exigen redirect_uri también en el flujo QR.
    # Solo se usa si está configurado explícitamente para QR; el flujo normal
    # por QR no necesita redirect URI.
    redirect_uri = _env("TIKTOK_QR_REDIRECT_URI")
    desc = (body.get("error_description") or "") if isinstance(body, dict) else ""
    if redirect_uri and "redirect_uri" in desc.lower():
        retry_params = dict(base_params)
        retry_params["redirect_uri"] = redirect_uri
        status, body, _ = _http_json(
            TIKTOK_TOKEN_URL,
            urllib.parse.urlencode(retry_params).encode(),
            {"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
            allow_http_errors=True,
        )
        if status < 400 and body.get("access_token"):
            return body

    if isinstance(body, dict) and body.get("error"):
        desc = body.get("error_description") or body.get("error")
        raise SocialAuthError(f"TikTok no entregó el token: {desc}")
    raise SocialAuthError(f"TikTok no entregó el token (HTTP {status}).")


def tiktok_qr_poll(token, client_ticket, interval=3, timeout=600):
    """Espera el escaneo del QR y guarda los tokens de TikTok."""
    client_key, client_secret = tiktok_config()
    deadline = time.time() + max(60, int(timeout or 600))
    wait = max(2, int(interval or 3))
    while time.time() < deadline:
        time.sleep(wait)
        data = urllib.parse.urlencode(
            {
                "client_key": client_key,
                "client_secret": client_secret,
                "token": token,
            }
        ).encode()
        status, body, _ = _http_json(
            TIKTOK_CHECK_QRCODE_URL,
            data,
            {"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
            allow_http_errors=True,
        )
        if status >= 400:
            raise SocialAuthError(f"TikTok respondió HTTP {status} al revisar el QR.")
        if body.get("error"):
            raise SocialAuthError(
                f"TikTok: {body.get('error_description') or body.get('error')}"
            )
        returned_ticket = body.get("client_ticket") or ""
        if returned_ticket and returned_ticket != client_ticket:
            raise SocialAuthError(
                "El QR de TikTok no coincide con la solicitud. Genera uno nuevo."
            )
        qr_status = body.get("status")
        if qr_status == "confirmed":
            code = _extract_code(body.get("redirect_uri") or body.get("code"))
            if not code:
                raise SocialAuthError("TikTok confirmó el QR pero no envió el código.")
            token_body = _tiktok_exchange_code(code)
            expires_in = int(token_body.get("expires_in") or 86400)
            refresh_expires_in = int(token_body.get("refresh_expires_in") or 0)
            update_tokens(
                "tiktok",
                {
                    "access_token": token_body.get("access_token", ""),
                    "refresh_token": token_body.get("refresh_token", ""),
                    "expires_at": time.time() + expires_in,
                    "refresh_expires_at": (
                        time.time() + refresh_expires_in
                        if refresh_expires_in
                        else 0
                    ),
                    "open_id": token_body.get("open_id", ""),
                    "scope": token_body.get("scope", ""),
                    "connected_at": int(time.time()),
                },
            )
            user_info = {}
            try:
                user_info = tiktok_user_info(token_body.get("access_token"))
            except Exception as e:
                logger.warning("No se pudo consultar el perfil de TikTok: %s", e)

            info = {}
            try:
                info = tiktok_creator_info(token_body.get("access_token"))
            except Exception as e:
                logger.warning("No se pudo consultar creator_info de TikTok: %s", e)

            update_tokens(
                "tiktok",
                {
                    "username": info.get("creator_username", "")
                    or user_info.get("username", ""),
                    "nickname": info.get("creator_nickname", "")
                    or user_info.get("display_name", ""),
                    "avatar_url": user_info.get("avatar_url", ""),
                    "privacy_options": info.get("privacy_level_options", []),
                    "max_video_duration": info.get(
                        "max_video_post_duration_sec", 0
                    ),
                },
            )
            return {"tokens": token_body, "creator": info, "user": user_info}
        if qr_status == "expired":
            raise SocialAuthError("El QR de TikTok expiró. Genera uno nuevo.")
        if qr_status == "utilised":
            raise SocialAuthError(
                "El QR de TikTok ya fue utilizado. Genera uno nuevo."
            )
        # new / scanned: seguir esperando
    raise SocialAuthError("El QR de TikTok expiró. Genera uno nuevo.")


def tiktok_access_token(force=False):
    with _refresh_locks["tiktok"]:
        return _tiktok_access_token_locked(force)


def _tiktok_access_token_locked(force=False):
    token = load_tokens().get("tiktok", {})
    if not force and _token_is_fresh("tiktok"):
        return token["access_token"]
    refresh_token = token.get("refresh_token") or _env("TIKTOK_REFRESH_TOKEN")
    if not refresh_token:
        # Modo de compatibilidad: access token corto sin refresh token.
        access_only = _env("TIKTOK_ACCESS_TOKEN")
        if access_only:
            if (
                token.get("access_token") == access_only
                and float(token.get("expires_at", 0) or 0) <= time.time()
            ):
                raise SocialAuthError(
                    "El access token de TikTok expiró. Actualízalo en .env o "
                    "conecta la cuenta con /conectar_tiktok."
                )
            update_tokens(
                "tiktok",
                {
                    "access_token": access_only,
                    "refresh_token": "",
                    "expires_at": time.time() + 86400,
                    "scope": token.get("scope")
                    or _env("TIKTOK_ACCESS_SCOPE", ""),
                },
            )
            return access_only
        raise SocialAuthError(
            "TikTok no está conectado. Usa /conectar_tiktok para vincular la cuenta."
        )
    client_key, client_secret = tiktok_config()
    data = urllib.parse.urlencode(
        {
            "client_key": client_key,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
    ).encode()
    status, body, _ = _http_json(
        TIKTOK_TOKEN_URL,
        data,
        {"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
        allow_http_errors=True,
    )
    if status >= 400 or not body.get("access_token"):
        desc = body.get("error_description") or body.get("error") or f"HTTP {status}"
        raise SocialAuthError(
            f"El permiso de TikTok venció o fue revocado ({desc}). "
            "Vuelve a conectar la cuenta con /conectar_tiktok."
        )
    expires_in = int(body.get("expires_in") or 86400)
    refresh_expires_in = int(body.get("refresh_expires_in") or 0)
    update_tokens(
        "tiktok",
        {
            "access_token": body["access_token"],
            "refresh_token": body.get("refresh_token") or refresh_token,
            "expires_at": time.time() + expires_in,
            "refresh_expires_at": (
                time.time() + refresh_expires_in if refresh_expires_in else 0
            ),
            "open_id": body.get("open_id", token.get("open_id", "")),
            "scope": body.get("scope", token.get("scope", "")),
        },
    )
    return body["access_token"]


def _tiktok_api_post(url, payload, access_token, timeout=60):
    status, body, _ = _http_json(
        url,
        json.dumps(payload).encode("utf-8"),
        {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
        method="POST",
        timeout=timeout,
        allow_http_errors=True,
    )
    _raise_tiktok_body(body, status)
    if status >= 400:
        raise SocialError(f"TikTok respondió HTTP {status}")
    return body.get("data", body)


def tiktok_user_info(access_token=None):
    """Perfil básico de la cuenta (sirve para validar el token y mostrar el nombre)."""
    token = access_token or tiktok_access_token()
    url = TIKTOK_USER_INFO_URL + "?" + urllib.parse.urlencode(
        {"fields": "open_id,display_name,avatar_url"}
    )
    status, body, _ = _http_json(
        url,
        headers={"Authorization": f"Bearer {token}"},
        allow_http_errors=True,
    )
    _raise_tiktok_body(body, status)
    if status >= 400:
        raise SocialError(f"TikTok respondió HTTP {status} al consultar el perfil.")
    return (body.get("data") or {}).get("user") or {}


def tiktok_creator_info(access_token=None):
    token = access_token or tiktok_access_token()
    data = _tiktok_api_post(TIKTOK_CREATOR_INFO_URL, {}, token)
    return data


def tiktok_scopes():
    token = load_tokens().get("tiktok", {})
    raw = str(token.get("scope") or _env("TIKTOK_ACCESS_SCOPE", ""))
    return {s.strip() for s in raw.split(",") if s.strip()}


def tiktok_mode():
    scopes = tiktok_scopes()
    if "video.publish" in scopes:
        return "direct"
    if "video.upload" in scopes:
        return "inbox"
    return "none"


def _tiktok_upload_file(upload_url, local_path, size, chunk_size, total_chunks,
                        mime_type="video/mp4"):
    with open(local_path, "rb") as f:
        for index in range(total_chunks):
            start = index * chunk_size
            f.seek(start)
            chunk = f.read(chunk_size)
            end = start + len(chunk) - 1
            req = urllib.request.Request(
                upload_url,
                data=chunk,
                method="PUT",
                headers={
                    "Content-Type": mime_type,
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {start}-{end}/{size}",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=600) as resp:
                    if resp.status not in (200, 201, 206):
                        raise SocialPublishError(
                            f"TikTok rechazó el chunk {index + 1} (HTTP {resp.status})."
                        )
            except urllib.error.HTTPError as e:
                body = e.read()
                raise SocialPublishError(
                    f"TikTok rechazó el chunk {index + 1} (HTTP {e.code}): "
                    f"{body.decode('utf-8', 'replace')[:300]}"
                )


def _tiktok_wait_status(publish_id, access_token, mode, timeout=600):
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        time.sleep(5)
        last = _tiktok_api_post(
            TIKTOK_STATUS_URL, {"publish_id": publish_id}, access_token
        )
        status = last.get("status")
        if status == "PUBLISH_COMPLETE":
            return last
        if mode == "inbox" and status == "SEND_TO_USER_INBOX":
            return last
        if status == "FAILED":
            reason = last.get("fail_reason") or "sin detalle"
            friendly = {
                "file_format_check_failed": (
                    "formato no soportado (usa MP4/MOV/WebM con H.264)"
                ),
                "duration_check_failed": "duración no permitida por TikTok",
                "frame_rate_check_failed": "FPS no permitido (usa 23 a 60 FPS)",
                "picture_size_check_failed": (
                    "resolución no permitida (usa entre 360 y 4096 px)"
                ),
                "spam_risk_text": (
                    "TikTok marcó el texto como riesgoso o spam"
                ),
                "spam_risk": "TikTok marcó la publicación como riesgosa",
                "auth_removed": "el cliente retiró el permiso de la app",
                "publish_cancelled": "la publicación fue cancelada",
            }.get(reason, reason)
            raise SocialPublishError(
                f"TikTok no pudo procesar el video: {friendly}"
            )
    # No lo tratamos como error: TikTok puede tardar en moderar. Devolvemos
    # "pendiente" para no provocar reintentos que dupliquen el video.
    result = dict(last) if isinstance(last, dict) else {}
    result["pending"] = True
    result.setdefault("status", "PROCESSING")
    return result


def publish_video_to_tiktok(local_path, caption="", privacy_level=None,
                            dry_run=False):
    """Publica en TikTok (directo) o envía a la bandeja de entrada."""
    if not local_path or not os.path.exists(local_path):
        raise SocialPublishError("No se encontró el archivo de video para TikTok.")
    size = os.path.getsize(local_path)
    if size <= 0:
        raise SocialPublishError("El archivo de video está vacío.")
    if size > 4 * 1024 * 1024 * 1024:
        raise SocialPublishError("TikTok acepta videos de máximo 4 GB.")

    ext = os.path.splitext(local_path)[1].lower()
    if ext not in (".mp4", ".mov", ".webm"):
        raise SocialPublishError(
            "TikTok solo acepta MP4, MOV o WebM. Convierte el video antes de subirlo."
        )

    info = _probe_video(local_path)
    if info:
        duration = info.get("duration", 0)
        if duration and duration > 600:
            raise SocialPublishError(
                f"TikTok acepta máximo 10 minutos por API y el video dura "
                f"{duration / 60:.1f} minutos."
            )
        width = info.get("width", 0)
        height = info.get("height", 0)
        if width and height and not (360 <= width <= 4096 and 360 <= height <= 4096):
            raise SocialPublishError(
                f"TikTok exige entre 360 y 4096 píxeles por lado. "
                f"El video es {width}x{height}."
            )
        fps = info.get("fps", 0)
        if fps and not (23 <= fps <= 60):
            raise SocialPublishError(
                f"TikTok exige entre 23 y 60 FPS. El video tiene {fps:.1f} FPS."
            )

    access_token = tiktok_access_token()
    scopes = tiktok_scopes()
    mode = "direct" if "video.publish" in scopes else (
        "inbox" if "video.upload" in scopes else "none"
    )
    if mode == "none":
        raise SocialAuthError(
            "La cuenta de TikTok no tiene permiso para publicar ni subir videos. "
            "Reconecta la cuenta y aprueba video.publish o video.upload."
        )

    creator = {}
    privacy = (privacy_level or _env("TIKTOK_PRIVACY_LEVEL", "SELF_ONLY")).strip()
    warning = ""
    if mode == "direct":
        creator = tiktok_creator_info(access_token)
        options = creator.get("privacy_level_options") or []
        if options:
            if privacy not in options:
                fallback = "SELF_ONLY" if "SELF_ONLY" in options else options[0]
                warning = (
                    f"La privacidad {privacy} no está disponible; se usó "
                    f"{fallback}."
                )
                privacy = fallback
        max_duration = int(creator.get("max_video_post_duration_sec") or 0)
        if max_duration and info.get("duration", 0) > max_duration:
            raise SocialPublishError(
                f"Esta cuenta de TikTok permite hasta {max_duration} segundos "
                f"por video y el archivo dura {info.get('duration', 0):.0f} segundos."
            )
    else:
        privacy = "SELF_ONLY"
        warning = (
            "La app solo tiene permiso video.upload: el video se envió a la "
            "bandeja de entrada de TikTok. El cliente debe abrir TikTok, "
            "escribir el texto, elegir la privacidad y confirmar la publicación. "
            "Si la app no está auditada, TikTok puede limitar la visibilidad a privado."
        )

    if dry_run:
        return {
            "success": True,
            "dry_run": True,
            "mode": mode,
            "privacy": privacy,
            "username": creator.get("creator_username", ""),
            "warning": warning,
        }

    # TikTok permite un solo chunk hasta 64 MB (y obliga a que los videos
    # menores de 5 MB vayan completos). Para archivos grandes, chunks de 10 MB.
    if size <= 64 * 1024 * 1024:
        chunk_size = size
        total_chunks = 1
    else:
        chunk_size = 10 * 1024 * 1024
        # La documentación de TikTok pide floor(video_size / chunk_size) y
        # permite que el último chunk sea más grande para absorber el resto.
        total_chunks = max(1, size // chunk_size)
        if total_chunks > 1000:
            chunk_size = math.ceil(size / 1000)
            total_chunks = max(1, size // chunk_size)

    source_info = {
        "source": "FILE_UPLOAD",
        "video_size": size,
        "chunk_size": chunk_size,
        "total_chunk_count": total_chunks,
    }

    if mode == "direct":
        payload = {
            "post_info": {
                "title": (caption or "")[:2200],
                "privacy_level": privacy,
                "disable_duet": False,
                "disable_comment": False,
                "disable_stitch": False,
                "video_cover_timestamp_ms": 1000,
                "brand_content_toggle": False,
                "brand_organic_toggle": False,
            },
            "source_info": source_info,
        }
        init_url = TIKTOK_DIRECT_INIT_URL
    else:
        payload = {"source_info": source_info}
        init_url = TIKTOK_INBOX_INIT_URL

    data = _tiktok_api_post(init_url, payload, access_token)
    publish_id = data.get("publish_id")
    upload_url = data.get("upload_url")
    if not publish_id or not upload_url:
        raise SocialPublishError(
            "TikTok no devolvió la URL de subida. Revisa los permisos de la app."
        )

    mime_type = {
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".webm": "video/webm",
    }.get(ext, "video/mp4")
    _tiktok_upload_file(
        upload_url, local_path, size, chunk_size, total_chunks, mime_type
    )
    final = _tiktok_wait_status(publish_id, access_token, mode)
    pending = bool(final.get("pending"))

    post_ids = (
        final.get("publicaly_available_post_id")
        or final.get("publicly_available_post_id")
        or []
    )
    url = ""
    username = creator.get("creator_username") or load_tokens().get(
        "tiktok", {}
    ).get("username", "")
    if post_ids and username:
        url = f"https://www.tiktok.com/@{username}/video/{post_ids[0]}"

    if pending:
        extra = (
            "TikTok todavía está procesando el video (moderación). "
            "Revisa la app o el perfil en unos minutos; no lo envíes de nuevo "
            "para evitar duplicados."
        )
        warning = f"{warning} {extra}".strip() if warning else extra

    return {
        "success": True,
        "dry_run": False,
        "mode": mode,
        "publish_id": publish_id,
        "url": url,
        "privacy": privacy,
        "username": username,
        "warning": warning,
        "status": final.get("status", ""),
        "pending": pending,
    }


# ---------------------------------------------------------------------------
# Estado de las conexiones
# ---------------------------------------------------------------------------


def local_status():
    data = load_tokens()
    yt = data.get("youtube", {}) or {}
    tt = data.get("tiktok", {}) or {}
    yt_connected = bool(
        yt.get("refresh_token")
        or yt.get("access_token")
        or _env("YOUTUBE_ACCESS_TOKEN")
    )
    tt_connected = bool(
        tt.get("refresh_token")
        or tt.get("access_token")
        or _env("TIKTOK_ACCESS_TOKEN")
    )
    return {
        "youtube": {
            "connected": yt_connected,
            "channel_title": yt.get("channel_title", ""),
            "channel_id": yt.get("channel_id", ""),
            "expires_at": yt.get("expires_at", 0),
            "scope": yt.get("scope", ""),
        },
        "tiktok": {
            "connected": tt_connected,
            "username": tt.get("username", ""),
            "nickname": tt.get("nickname", ""),
            "open_id": tt.get("open_id", ""),
            "expires_at": tt.get("expires_at", 0),
            "scope": tt.get("scope", ""),
            "mode": tiktok_mode() if tt_connected else "none",
        },
    }


def check_connections():
    """Valida los tokens contra las APIs. Devuelve estado y errores."""
    result = local_status()
    try:
        channel = youtube_channel_info()
        result["youtube"].update(channel)
        result["youtube"]["valid"] = True
    except Exception as e:
        result["youtube"]["valid"] = False
        result["youtube"]["error"] = str(e)

    try:
        token = tiktok_access_token()
        user = tiktok_user_info(token)
        result["tiktok"].update(
            {
                "nickname": user.get("display_name", "")
                or result["tiktok"].get("nickname", ""),
                "avatar_url": user.get("avatar_url", ""),
            }
        )
        if "video.publish" in tiktok_scopes():
            creator = tiktok_creator_info(token)
            result["tiktok"].update(
                {
                    "username": creator.get("creator_username", "")
                    or result["tiktok"].get("username", ""),
                    "nickname": creator.get("creator_nickname", "")
                    or result["tiktok"].get("nickname", ""),
                }
            )
        result["tiktok"]["valid"] = True
    except Exception as e:
        result["tiktok"]["valid"] = False
        result["tiktok"]["error"] = str(e)
    return result
