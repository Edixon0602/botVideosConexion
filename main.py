import os
import shutil
import subprocess
import json
import logging
from ftplib import FTP
import asyncio
import threading
import urllib.request
import urllib.parse
import time
import re
import html
import tempfile
import unicodedata
import aiohttp
from bs4 import BeautifulSoup

# Parche para Render y Python 3.10+ (corrige el error de 'no current event loop')
try:
    loop = asyncio.get_running_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

from dotenv import load_dotenv
from pyrogram import Client, filters, enums
from pyrogram.errors import MessageNotModified
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
import uuid
import datetime
try:
    from zoneinfo import ZoneInfo
    CARACAS_TZ = ZoneInfo("America/Caracas")
except Exception:
    CARACAS_TZ = datetime.timezone(datetime.timedelta(hours=-4))

def format_caracas_time(ts):
    dt = datetime.datetime.fromtimestamp(ts, tz=CARACAS_TZ)
    return dt.strftime('%Y-%m-%d %I:%M:%S %p')
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, flash, session

# Configurar logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Cargar variables de entorno
load_dotenv()

import social_media

BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")

FTP_HOST = os.getenv("FTP_HOST")
FTP_USER = os.getenv("FTP_USER")
FTP_PASS = os.getenv("FTP_PASS")
# FTP_UPLOAD_PATH ya no lo usamos globalmente porque es dinámico por usuario, 
# pero podemos dejarlo para retrocompatibilidad
FTP_UPLOAD_PATH = os.getenv("FTP_UPLOAD_PATH")

ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")
NOTIFICATION_CHAT_ID = os.getenv("NOTIFICATION_CHAT_ID")

# Lista de administradores autorizados (soporta múltiples IDs)
ADMIN_CHAT_IDS = [1094100980]  # ID admin Edixon
if NOTIFICATION_CHAT_ID and NOTIFICATION_CHAT_ID != "tu_chat_id_aqui":
    for cid in str(NOTIFICATION_CHAT_ID).split(","):
        cid = cid.strip()
        if cid.lstrip("-").isdigit():
            val = int(cid)
            if val not in ADMIN_CHAT_IDS:
                ADMIN_CHAT_IDS.append(val)

def is_admin(chat_id) -> bool:
    try:
        return int(chat_id) in ADMIN_CHAT_IDS
    except (ValueError, TypeError):
        return False

if not BOT_TOKEN or BOT_TOKEN == "tu_token_aqui":
    print("⚠️ ERROR: POR FAVOR, COLOCA TU TOKEN EN EL ARCHIVO .env")
    exit(1)

if not API_ID or API_ID == "tu_api_id_aqui":
    print("⚠️ ERROR: Faltan API_ID y API_HASH en .env")
    exit(1)

# Archivo JSON para almacenar usuarios permitidos
USERS_FILE = 'users.json'

def load_users():
    if not os.path.exists(USERS_FILE):
        return {}
    try:
        with open(USERS_FILE, 'r') as f:
            data = json.load(f)
            # Migración desde lista antigua
            if isinstance(data, list):
                new_dict = {str(item): {"ftp_path": FTP_UPLOAD_PATH or "avances-informativos", "name": "Usuario"} for item in data}
                save_users(new_dict)
                return new_dict
            
            # Migración desde diccionario antiguo (solo strings) a diccionario con nombre
            if isinstance(data, dict):
                needs_migration = False
                for k, v in data.items():
                    if isinstance(v, str):
                        data[k] = {"ftp_path": v, "name": "Usuario"}
                        needs_migration = True
                if needs_migration:
                    save_users(data)
                return data
            return data
    except:
        return {}

def save_users(users):
    with open(USERS_FILE, 'w') as f:
        json.dump(users, f)

DELETIONS_FILE = 'deletions.json'
deletions_lock = threading.Lock()
admin_states = {}
youtube_connect_state = {"active": False, "started_at": 0, "flow_id": 0, "task": None}
tiktok_connect_state = {"active": False, "started_at": 0, "flow_id": 0, "task": None}
social_publish_in_progress = set()

def load_deletions():
    with deletions_lock:
        if not os.path.exists(DELETIONS_FILE):
            return {}
        try:
            with open(DELETIONS_FILE, 'r') as f:
                return json.load(f)
        except:
            return {}

def save_deletions(data):
    with deletions_lock:
        with open(DELETIONS_FILE, 'w') as f:
            json.dump(data, f)

# Inicializar cliente de Pyrogram
app = Client(
    "mi_bot_videos",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

def sanitize_filename(filename: str) -> str:
    """
    Sanitiza un nombre de archivo para compatibilidad con FTP, Linux y VDO Panel:
    - Normaliza unicode para remover acentos y tildes.
    - Reemplaza espacios, paréntesis y caracteres especiales por guiones.
    - Colapsa múltiples guiones consecutivos.
    """
    if not filename:
        return "video.mp4"
    base, ext = os.path.splitext(filename)
    base = unicodedata.normalize('NFKD', base).encode('ascii', 'ignore').decode('ascii')
    base = re.sub(r'[^a-zA-Z0-9_-]', '-', base)
    base = re.sub(r'-+', '-', base).strip('-')
    if not base:
        base = "video"
    ext = ext.lower() if ext else ".mp4"
    return f"{base}{ext}"

def upload_to_ftp(local_file_path: str, remote_filename: str, target_folder: str, copies: int = 10):
    """Sube un archivo al servidor FTP en la carpeta específica (1 copia o N copias)"""
    remote_filename = sanitize_filename(remote_filename)
    logger.info(f"Conectando a FTP: {FTP_HOST}...")
    ftp = FTP(FTP_HOST)
    ftp.login(user=FTP_USER, passwd=FTP_PASS)
    
    # Limpiar el nombre de la carpeta destino
    if target_folder:
        target_folder = target_folder.strip("/")
    
    # Navegar a la carpeta destino
    if target_folder:
        try:
            ftp.cwd(target_folder)
        except Exception:
            logger.info(f"La carpeta '{target_folder}' no existe, intentando crearla...")
            try:
                ftp.mkd(target_folder)
                ftp.cwd(target_folder)
                logger.info(f"Carpeta '{target_folder}' creada exitosamente.")
            except Exception as e:
                logger.error(f"Error creando la carpeta '{target_folder}': {e}")
                ftp.quit()
                raise Exception(f"No se pudo crear ni acceder a la carpeta destino: {target_folder}")
    
    if copies <= 1:
        logger.info(f"Subiendo archivo único al FTP: {remote_filename}...")
        with open(local_file_path, 'rb') as file:
            ftp.storbinary(f'STOR {remote_filename}', file)
        ftp.quit()
        logger.info("Subida de archivo único al FTP completada.")
        return

    name, ext = os.path.splitext(remote_filename)
    
    for i in range(copies):
        if i == 0:
            current_filename = remote_filename
        else:
            current_filename = f"{name}-{i}{ext}"
            
        logger.info(f"Subiendo copia {i+1}/{copies}: {current_filename}...")
        with open(local_file_path, 'rb') as file:
            ftp.storbinary(f'STOR {current_filename}', file)
        
    ftp.quit()
    logger.info(f"Subida de {copies} copias al FTP completada.")

def extract_audio_from_video(video_path: str, output_audio_path: str) -> bool:
    """Extrae el audio de un video en formato MP3 usando ffmpeg"""
    try:
        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-vn",
            "-acodec", "libmp3lame",
            "-b:a", "192k",
            output_audio_path
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if res.returncode == 0 and os.path.exists(output_audio_path):
            logger.info(f"Audio extraído exitosamente con ffmpeg: {output_audio_path}")
            return True
        else:
            logger.error(f"Error extrayendo audio con ffmpeg: {res.stderr.decode('utf-8', errors='ignore')}")
            return False
    except Exception as e:
        logger.error(f"Excepción al ejecutar ffmpeg: {e}")
        return False

def upload_audio_to_dropbox(local_audio_path: str, dropbox_folder: str = None) -> bool:
    """Sube un archivo de audio a la carpeta de Dropbox usando refresh token"""
    app_key = os.getenv("DROPBOX_APP_KEY")
    app_secret = os.getenv("DROPBOX_APP_SECRET")
    refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
    
    if not (app_key and app_secret and refresh_token):
        logger.warning("Credenciales de Dropbox no configuradas en .env. Se omite la subida a Dropbox.")
        return False
        
    try:
        import dropbox
        dbx = dropbox.Dropbox(
            app_key=app_key,
            app_secret=app_secret,
            oauth2_refresh_token=refresh_token
        )
        folder = dropbox_folder or os.getenv("DROPBOX_FOLDER", "/CNX-INFORMATIVA")
        folder_clean = "/" + folder.strip("/") if folder.strip("/") else ""
        filename = os.path.basename(local_audio_path)
        dest_path = f"{folder_clean}/{filename}"
        
        logger.info(f"Subiendo audio a Dropbox en {dest_path}...")
        with open(local_audio_path, "rb") as f:
            dbx.files_upload(f.read(), dest_path, mode=dropbox.files.WriteMode.overwrite)
            
        logger.info(f"Audio subido exitosamente a Dropbox: {dest_path}")
        return True
    except Exception as e:
        logger.error(f"Error al subir audio a Dropbox: {e}")
        return False

def dropbox_delete_file(dropbox_file_path: str) -> bool:
    """Elimina un archivo de Dropbox dada su ruta remota."""
    app_key = os.getenv("DROPBOX_APP_KEY")
    app_secret = os.getenv("DROPBOX_APP_SECRET")
    refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
    
    if not (app_key and app_secret and refresh_token):
        return False
        
    try:
        import dropbox
        dbx = dropbox.Dropbox(
            app_key=app_key,
            app_secret=app_secret,
            oauth2_refresh_token=refresh_token
        )
        # Asegurar que la ruta comience con '/'
        path_clean = "/" + dropbox_file_path.strip("/") if dropbox_file_path.strip("/") else dropbox_file_path
        dbx.files_delete_v2(path_clean)
        logger.info(f"Archivo de audio eliminado exitosamente de Dropbox: {path_clean}")
        return True
    except Exception as e:
        logger.warning(f"No se pudo eliminar el archivo de audio de Dropbox ({dropbox_file_path}): {e}")
        return False

def download_via_external_api(url: str, output_dir: str = "downloads") -> tuple[bool, str, str, str]:
    """
    Descarga videos de YouTube en 1080p usando un endpoint externo de procesamiento.
    Esto elude 100% los bloqueos de IP y 'Sign in to confirm you are not a bot' de los centros de datos (VPS).
    """
    try:
        os.makedirs(output_dir, exist_ok=True)
        encoded_yt_url = urllib.parse.quote(url, safe='')
        api_url = f"https://loader.to/ajax/download.php?button=1&start=1&end=1&format=1080&url={encoded_yt_url}"
        
        req = urllib.request.Request(api_url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
            
        progress_url = data.get("progress_url")
        video_title = data.get("title") or "video_youtube"
        
        if not progress_url:
            return False, "", video_title, "No se recibió URL de procesamiento del servicio externo."
            
        logger.info(f"[YouTube API] Procesando video en 1080p: {video_title} ({progress_url})...")
        
        # Sondeo de finalización (hasta 60 segundos)
        direct_download_url = None
        for attempt in range(25):
            time.sleep(2.5)
            p_req = urllib.request.Request(progress_url, headers={'User-Agent': 'Mozilla/5.0'})
            try:
                with urllib.request.urlopen(p_req, timeout=15) as p_resp:
                    p_data = json.loads(p_resp.read().decode())
                    if p_data.get("download_url"):
                        direct_download_url = p_data.get("download_url")
                        break
                    elif p_data.get("success") == 1 and p_data.get("progress") == 1000:
                        direct_download_url = p_data.get("download_url")
                        break
            except Exception as e_poll:
                logger.warning(f"[YouTube API] Polling warning: {e_poll}")
                
        if not direct_download_url:
            return False, "", video_title, "Tiempo de espera agotado generando el enlace de descarga 1080p."
            
        logger.info(f"[YouTube API] Enlace 1080p obtenido: {direct_download_url}. Descargando stream...")
        safe_name = sanitize_filename(f"{video_title}.mp4")
        if not safe_name.endswith(".mp4"):
            safe_name += ".mp4"
            
        local_dest = os.path.join(output_dir, f"{uuid.uuid4().hex[:6]}_{safe_name}")
        
        dl_req = urllib.request.Request(direct_download_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(dl_req, timeout=60) as dl_resp:
            with open(local_dest, "wb") as f_out:
                while chunk := dl_resp.read(65536):
                    f_out.write(chunk)
                    
        if os.path.exists(local_dest) and os.path.getsize(local_dest) > 1024:
            logger.info(f"[YouTube API] Descarga exitosa: {local_dest} ({os.path.getsize(local_dest)} bytes)")
            return True, local_dest, video_title, ""
        else:
            return False, "", video_title, "El archivo descargado está vacío o corrupto."
            
    except Exception as e:
        logger.error(f"[YouTube API] Excepción en servicio externo: {e}")
        return False, "", "", str(e)

def _youtube_cookies_file():
    candidates = []
    explicit = os.getenv("YOUTUBE_COOKIES_FILE")
    if explicit:
        candidates.append(explicit)
    candidates.extend([
        os.path.join(os.getcwd(), "cookies.txt"),
        "/app/cookies.txt",
        "cookies.txt",
    ])
    seen = set()
    for path in candidates:
        if not path:
            continue
        abs_path = os.path.abspath(path)
        if abs_path in seen:
            continue
        seen.add(abs_path)
        if os.path.isfile(abs_path):
            return abs_path
    return None


def _youtube_deno_path():
    explicit = os.getenv("YOUTUBE_DENO_PATH")
    if explicit and os.path.exists(explicit):
        return explicit
    return shutil.which("deno")


def _youtube_ydl_opts(output_dir: str, run_id: str, cookie_file: str = None,
                      clients=None):
    out_template = os.path.join(
        output_dir, f"{run_id}_%(id)s_%(title).80s.%(ext)s"
    )
    opts = {
        # Preferir H.264 (avc1) para que el video sea compatible con el VDO
        # Panel y con TikTok. Si no hay, se acepta cualquier códec dentro de mp4.
        "format": (
            "bestvideo[height<=1080][ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/"
            "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/"
            "bestvideo[height<=1080]+bestaudio/"
            "best[height<=1080]/best"
        ),
        "merge_output_format": "mp4",
        "outtmpl": out_template,
        "noplaylist": True,
        "quiet": False,
        "no_warnings": False,
        "windowsfilenames": True,
        "prefer_ffmpeg": True,
        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 3,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 4,
        "extractor_args": {
            "youtube": {
                # Se prueba un cliente por intento: si se pasan varios juntos,
                # un cliente con "Sign in to confirm" puede abortar la extracción
                # completa y desperdiciar los clientes que sí funcionan.
                "player_client": clients or ["web_embedded"],
            }
        },
        "postprocessors": [{
            "key": "FFmpegVideoConvertor",
            "preferedformat": "mp4",
        }],
    }
    if cookie_file:
        opts["cookiefile"] = cookie_file
    deno_path = _youtube_deno_path()
    if deno_path:
        opts["js_runtimes"] = {"deno": {"path": deno_path}}
    proxy = os.getenv("YOUTUBE_PROXY")
    if proxy:
        opts["proxy"] = proxy
    try:
        __import__("yt_dlp_ejs")
    except Exception:
        # Si el paquete yt-dlp-ejs no está instalado, permitir que yt-dlp
        # descargue los scripts EJS desde GitHub.
        opts["remote_components"] = {"ejs:github"}
    return opts


def _youtube_attempt(url: str, output_dir: str, run_id: str,
                     cookie_file: str = None, clients=None):
    import yt_dlp
    # Se usa una copia temporal para que yt-dlp no reescriba cookies.txt.
    cookie_copy = None
    if cookie_file:
        fd, cookie_copy = tempfile.mkstemp(
            prefix="yt_cookies_", suffix=".txt"
        )
        os.close(fd)
        try:
            shutil.copyfile(cookie_file, cookie_copy)
            try:
                os.chmod(cookie_copy, 0o600)
            except Exception:
                pass
        except Exception:
            try:
                os.remove(cookie_copy)
            except Exception:
                pass
            raise
    try:
        opts = _youtube_ydl_opts(output_dir, run_id, cookie_copy, clients)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    finally:
        if cookie_copy:
            try:
                os.remove(cookie_copy)
            except Exception:
                pass
    if not info:
        raise Exception("No se pudo extraer información del video.")

    title = info.get("title", "video_youtube")
    candidates = []
    for item in info.get("requested_downloads") or []:
        path = item.get("filepath")
        if path:
            candidates.append(path)
    for key in ("filepath", "_filename"):
        path = info.get(key)
        if path:
            candidates.append(path)
    for path in candidates:
        if path and os.path.exists(path):
            return path, title

    # Buscar por el prefijo único de esta ejecución, evitando archivos
    # temporales y prefiriendo el MP4 final.
    matches = []
    for name in os.listdir(output_dir):
        if not name.startswith(run_id):
            continue
        if ".part" in name or name.endswith((".ytdl", ".temp")):
            continue
        if re.search(r"\.f\d+\.", name):
            continue
        matches.append(os.path.join(output_dir, name))
    if matches:
        matches.sort(
            key=lambda p: (p.lower().endswith(".mp4"), os.path.getmtime(p)),
            reverse=True,
        )
        return matches[0], title
    raise Exception("No se encontró el archivo descargado.")


def download_youtube_video(url: str, output_dir: str = "downloads") -> tuple[bool, str, str, str]:
    """
    Descarga un video de YouTube en 1080p:
    1. yt-dlp con cookies (si existen), Deno/EJS y clientes actuales.
    2. yt-dlp sin cookies, por si las cookies están vencidas.
    3. API externa solo si YOUTUBE_EXTERNAL_API_ENABLED=True.
    """
    os.makedirs(output_dir, exist_ok=True)
    errors = []

    cookie_file = _youtube_cookies_file()
    deno_path = _youtube_deno_path()
    if deno_path:
        logger.info(f"[YouTube] Deno disponible: {deno_path}")
    else:
        logger.warning(
            "[YouTube] Deno no está instalado. yt-dlp puede fallar al resolver "
            "los desafíos de YouTube o descargar en baja calidad."
        )

    # Se intenta un cliente por vez. Si se pasan varios en la misma lista,
    # un cliente bloqueado puede abortar la extracción completa.
    clients_env = os.getenv(
        "YOUTUBE_PLAYER_CLIENTS",
        "web_embedded,mweb,tv,web,android_vr",
    )
    client_names = [c.strip() for c in clients_env.split(",") if c.strip()]
    if not client_names:
        client_names = ["web_embedded"]

    attempts = []
    for client in client_names:
        if cookie_file:
            attempts.append((f"con cookies/{client}", cookie_file, [client]))
        attempts.append((f"sin cookies/{client}", None, [client]))

    for label, cookiefile, clients in attempts:
        run_id = f"yt_{uuid.uuid4().hex[:10]}"
        try:
            logger.info(f"[YouTube] Intentando descarga {label}: {url}")
            path, title = _youtube_attempt(
                url, output_dir, run_id, cookiefile, clients
            )
            if path and os.path.exists(path):
                logger.info(f"[YouTube] Descarga exitosa ({label}): {path}")
                return True, path, title, ""
            errors.append(f"{label}: no se encontró el archivo")
        except Exception as e:
            logger.error(f"[YouTube yt-dlp] Error ({label}): {e}")
            errors.append(f"{label}: {e}")

    if os.getenv("YOUTUBE_EXTERNAL_API_ENABLED", "False").lower() in (
        "1", "true", "yes", "si", "sí", "on"
    ):
        logger.info(f"[YouTube] Intentando API externa de respaldo: {url}")
        api_ok, api_path, api_title, api_err = download_via_external_api(
            url, output_dir
        )
        if api_ok and os.path.exists(api_path):
            return True, api_path, api_title, ""
        errors.append(f"API externa: {api_err}")

    detail = " | ".join(errors) or "No se pudo descargar el video."
    lowered = detail.lower()
    if (
        "this video is unavailable" in lowered
        or "video unavailable" in lowered
        or "private video" in lowered
        or "age-restricted" in lowered
        or "age restricted" in lowered
        or "not available in your country" in lowered
        or "blocked it in your country" in lowered
    ):
        friendly = (
            "El video no está disponible: puede ser privado, eliminado, tener "
            "restricción de edad o estar bloqueado en la región del servidor. "
            f"Detalle: {detail}"
        )
    elif (
        "sign in to confirm" in lowered
        or "confirm you're not a bot" in lowered
        or "confirm you are not a bot" in lowered
        or "cookies are no longer valid" in lowered
        or "login required" in lowered
    ):
        friendly = (
            "YouTube bloqueó la descarga desde el servidor. "
            "Si estás usando Tor, reinicia el servicio para cambiar de nodo. "
            "Si no, actualiza cookies.txt o configura YOUTUBE_PROXY. "
            f"Detalle: {detail}"
        )
    elif (
        "javascript runtime" in lowered
        or "ejs" in lowered
        or "deno" in lowered
        or "signature solving failed" in lowered
        or "challenge solving failed" in lowered
    ):
        friendly = (
            "Falta Deno/EJS para que yt-dlp resuelva los desafíos de YouTube. "
            "Reconstruye la imagen Docker o instala Deno en el servidor. "
            f"Detalle: {detail}"
        )
    elif "requested format is not available" in lowered:
        friendly = (
            "YouTube no ofreció un formato compatible para ese video. "
            "Actualiza yt-dlp/Deno o prueba con otro video. "
            f"Detalle: {detail}"
        )
    else:
        friendly = detail
    return False, "", "", friendly

def publish_video_to_meta(local_video_path: str, caption: str = "") -> dict:
    """
    Sube un video de forma simultánea a Instagram Reels y a la Página de Facebook usando Meta Graph API.
    Flujo:
    1. Sube video temporalmente a Dropbox para obtener un enlace público HTTPS directo.
    2. En Instagram:
       - Crea contenedor en Instagram Graph API (POST /{ig_user_id}/media con media_type=REELS).
       - Espera que Meta codifique el video (polling status_code == FINISHED).
       - Publica (POST /{ig_user_id}/media_publish) y obtiene permalink.
    3. En Facebook:
       - Obtiene el Page Access Token (PAT) del Fanpage mediante GET /{page_id}?fields=access_token.
       - Sube el video directamente a la Página (POST /{page_id}/videos) con file_url, description y title.
       - Obtiene el permalink o id del post de Facebook.
    4. Limpia el archivo temporal de Dropbox.
    Retorna un diccionario estructurado:
    {
        "instagram": {"success": bool, "permalink": str, "error": str},
        "facebook": {"success": bool, "permalink": str, "error": str}
    }
    """
    results = {
        "instagram": {"success": False, "permalink": "", "error": ""},
        "facebook": {"success": False, "permalink": "", "error": ""}
    }

    ig_account_id = os.getenv("INSTAGRAM_ACCOUNT_ID")
    fb_page_id = os.getenv("FB_PAGE_ID", "116061275124689")
    meta_token = os.getenv("META_ACCESS_TOKEN") or os.getenv("INSTAGRAM_ACCESS_TOKEN")
    dry_run = os.getenv("INSTAGRAM_DRY_RUN", "False").lower() in ("true", "1", "yes")

    if not meta_token:
        err = "Token de Meta (META_ACCESS_TOKEN) no configurado en .env"
        results["instagram"]["error"] = err
        results["facebook"]["error"] = err
        return results

    app_key = os.getenv("DROPBOX_APP_KEY")
    app_secret = os.getenv("DROPBOX_APP_SECRET")
    refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")

    if not (app_key and app_secret and refresh_token):
        err = "Credenciales de Dropbox requeridas para generar enlace público temporal"
        results["instagram"]["error"] = err
        results["facebook"]["error"] = err
        return results

    temp_dbx_path = ""
    dbx = None
    try:
        import dropbox
        dbx = dropbox.Dropbox(
            app_key=app_key,
            app_secret=app_secret,
            oauth2_refresh_token=refresh_token
        )

        folder = os.getenv("DROPBOX_FOLDER", "/CNX-INFORMATIVA")
        folder_clean = "/" + folder.strip("/") if folder.strip("/") else ""
        unique_name = f"temp_meta_{uuid.uuid4().hex[:8]}_{os.path.basename(local_video_path)}"
        temp_dbx_path = f"{folder_clean}/{unique_name}"

        logger.info(f"[Meta] Subiendo video temporal a Dropbox: {temp_dbx_path}")
        with open(local_video_path, "rb") as f:
            dbx.files_upload(f.read(), temp_dbx_path, mode=dropbox.files.WriteMode.overwrite)

        link_res = dbx.files_get_temporary_link(temp_dbx_path)
        video_url = link_res.link
        logger.info("[Meta] Enlace temporal HTTPS de Dropbox generado correctamente")

        # --- 1. PUBLICACIÓN EN INSTAGRAM REELS ---
        if ig_account_id:
            try:
                logger.info(f"[Instagram] Iniciando publicación en cuenta {ig_account_id}...")
                create_url = f"https://graph.facebook.com/v21.0/{ig_account_id}/media"
                ig_params = {
                    "media_type": "REELS",
                    "video_url": video_url,
                    "access_token": meta_token
                }
                if caption:
                    ig_params["caption"] = caption

                encoded_ig = urllib.parse.urlencode(ig_params).encode("utf-8")
                ig_req = urllib.request.Request(create_url, data=encoded_ig, method="POST")

                with urllib.request.urlopen(ig_req, timeout=60) as resp:
                    ig_data = json.loads(resp.read().decode())
                    container_id = ig_data.get("id")

                if not container_id:
                    results["instagram"]["error"] = "No se recibió creation_id del contenedor de Instagram"
                else:
                    logger.info(f"[Instagram] Contenedor creado: {container_id}. Verificando procesamiento...")
                    status_url = f"https://graph.facebook.com/v21.0/{container_id}?fields=status_code,status&access_token={meta_token}"
                    ready = False
                    for i in range(24):
                        time.sleep(5)
                        with urllib.request.urlopen(status_url, timeout=30) as s_resp:
                            s_data = json.loads(s_resp.read().decode())
                            code = s_data.get("status_code")
                            logger.info(f"[Instagram] Estado del contenedor (intento {i+1}): {code}")
                            if code == "FINISHED":
                                ready = True
                                break
                            elif code == "ERROR":
                                results["instagram"]["error"] = f"Error codificación Meta: {s_data.get('status', 'Error desconocido')}"
                                break

                    if not ready and not results["instagram"]["error"]:
                        results["instagram"]["error"] = "Tiempo de espera agotado procesando Reel en Instagram"
                    elif ready:
                        if dry_run:
                            logger.info("[Instagram] [DRY RUN] Reel listo. Omitiendo publicación en vivo.")
                            results["instagram"]["success"] = True
                            results["instagram"]["permalink"] = "dry_run"
                        else:
                            pub_url = f"https://graph.facebook.com/v21.0/{ig_account_id}/media_publish"
                            pub_params = {
                                "creation_id": container_id,
                                "access_token": meta_token
                            }
                            pub_data = urllib.parse.urlencode(pub_params).encode("utf-8")
                            pub_req = urllib.request.Request(pub_url, data=pub_data, method="POST")
                            with urllib.request.urlopen(pub_req, timeout=60) as pub_resp:
                                pub_res = json.loads(pub_resp.read().decode())
                                media_id = pub_res.get("id")

                            if not media_id:
                                results["instagram"]["error"] = "No se recibió media_id al publicar en Instagram"
                            else:
                                permalink = ""
                                try:
                                    info_url = f"https://graph.facebook.com/v21.0/{media_id}?fields=permalink&access_token={meta_token}"
                                    with urllib.request.urlopen(info_url, timeout=30) as info_resp:
                                        info_data = json.loads(info_resp.read().decode())
                                        permalink = info_data.get("permalink", "")
                                except Exception:
                                    pass
                                results["instagram"]["success"] = True
                                results["instagram"]["permalink"] = permalink or f"https://instagram.com/p/{media_id}"
                                logger.info(f"[Instagram] Reel publicado con éxito: {results['instagram']['permalink']}")
            except Exception as e_ig:
                logger.error(f"[Instagram] Error publicando Reel: {e_ig}")
                results["instagram"]["error"] = str(e_ig)
        else:
            results["instagram"]["error"] = "INSTAGRAM_ACCOUNT_ID no configurado"

        # --- 2. PUBLICACIÓN EN PÁGINA DE FACEBOOK ---
        if fb_page_id:
            try:
                logger.info(f"[Facebook] Obteniendo Page Access Token para página {fb_page_id}...")
                page_token_url = f"https://graph.facebook.com/v21.0/{fb_page_id}?fields=access_token&access_token={meta_token}"
                page_access_token = meta_token
                try:
                    with urllib.request.urlopen(page_token_url, timeout=30) as pt_resp:
                        pt_data = json.loads(pt_resp.read().decode())
                        page_access_token = pt_data.get("access_token", meta_token)
                except Exception as e_pat:
                    logger.warning(f"[Facebook] No se pudo obtener PAT dedicado ({e_pat}), usando token principal.")

                logger.info(f"[Facebook] Publicando video en Página {fb_page_id}...")
                fb_video_url = f"https://graph.facebook.com/v21.0/{fb_page_id}/videos"
                
                # Derivar título breve si hay caption
                fb_title = ""
                if caption:
                    first_line = caption.strip().split("\n")[0]
                    fb_title = first_line[:90]

                fb_params = {
                    "file_url": video_url,
                    "description": caption or "",
                    "access_token": page_access_token
                }
                if fb_title:
                    fb_params["title"] = fb_title

                if dry_run:
                    logger.info("[Facebook] [DRY RUN] Video validado. Omitiendo publicación en página pública.")
                    results["facebook"]["success"] = True
                    results["facebook"]["permalink"] = "dry_run"
                else:
                    fb_data_encoded = urllib.parse.urlencode(fb_params).encode("utf-8")
                    fb_req = urllib.request.Request(fb_video_url, data=fb_data_encoded, method="POST")
                    with urllib.request.urlopen(fb_req, timeout=60) as fb_resp:
                        fb_res = json.loads(fb_resp.read().decode())
                        fb_video_id = fb_res.get("id")

                    if fb_video_id:
                        results["facebook"]["success"] = True
                        results["facebook"]["permalink"] = f"https://www.facebook.com/{fb_video_id}"
                        logger.info(f"[Facebook] Video publicado exitosamente en Facebook: {results['facebook']['permalink']}")
                    else:
                        results["facebook"]["error"] = "No se recibió id de video de Facebook"
            except Exception as e_fb:
                logger.error(f"[Facebook] Error publicando video en Facebook: {e_fb}")
                results["facebook"]["error"] = str(e_fb)
        else:
            results["facebook"]["error"] = "FB_PAGE_ID no configurado"

    except Exception as e_gen:
        logger.error(f"[Meta] Excepción general en publish_video_to_meta: {e_gen}")
        if not results["instagram"]["error"]:
            results["instagram"]["error"] = str(e_gen)
        if not results["facebook"]["error"]:
            results["facebook"]["error"] = str(e_gen)
    finally:
        # Limpieza del archivo temporal en Dropbox
        if temp_dbx_path and dbx:
            try:
                dbx.files_delete_v2(temp_dbx_path)
                logger.info("[Meta] Video temporal eliminado de Dropbox")
            except Exception as e_del:
                logger.warning(f"[Meta] No se pudo eliminar archivo temporal de Dropbox: {e_del}")

    return results

def publish_video_to_instagram(local_video_path: str, caption: str = "") -> tuple[bool, str, str]:
    """Retrocompatibilidad: llama a publish_video_to_meta y retorna resultado de Instagram."""
    meta_res = publish_video_to_meta(local_video_path, caption)
    ig = meta_res["instagram"]
    if ig["success"]:
        return True, ig["permalink"], "Publicado exitosamente en Instagram"
    return False, "", ig["error"]

async def set_vdo_schedule_status(session: aiohttp.ClientSession, run_status: int = 1, sched_id: int = 17) -> bool:
    """Configura el estado del programador de la playlist (run_status: 1 = ON, 0 = OFF) en VDO Panel."""
    try:
        sched_url = "https://stream.conexion.com.ve/broadcaster/scheduler"
        async with session.get(sched_url) as resp:
            if resp.status != 200:
                return False
            html = await resp.text()
            
        soup = BeautifulSoup(html, 'html.parser')
        token = None
        for inp in soup.find_all('input', type='hidden'):
            if inp.get('name') == '_token':
                token = inp.get('value')
                break
                
        input_sched = soup.find('input', {'class': 'changeSchedStatus'})
        if input_sched and input_sched.get('data-sched-id'):
            try:
                sched_id = int(input_sched.get('data-sched-id'))
            except Exception:
                pass
                
        if not token:
            for sc in soup.find_all('script'):
                if sc.string and '_token' in sc.string:
                    m = re.search(r"_token:\s*'([^']+)'", sc.string)
                    if m:
                        token = m.group(1)
                        break
                        
        if not token:
            return False
            
        post_data = {
            'sched_id': str(sched_id),
            'run_status': str(run_status),
            '_token': token
        }
        headers = {
            'X-Requested-With': 'XMLHttpRequest',
            'Referer': sched_url,
            'Origin': 'https://stream.conexion.com.ve'
        }
        async with session.post("https://stream.conexion.com.ve/broadcaster/change-schedule-run-status", data=post_data, headers=headers) as resp:
            logger.info(f"Configuración de scheduler {sched_id} (run_status={run_status}): HTTP {resp.status}")
            return resp.status == 200
    except Exception as e:
        logger.error(f"Error configurando scheduler run status: {e}")
        return False

async def activate_vdo_scheduler(session: aiohttp.ClientSession, sched_id: int = 17) -> bool:
    """Retrocompatibilidad: activa el programador continuo."""
    return await set_vdo_schedule_status(session, run_status=1, sched_id=sched_id)

async def control_vdo_panel(action_url: str) -> tuple[bool, str]:
    """
    Controla coordinadamente el streaming de VDO Panel:
    - stop: Desactiva schedule (run_status=0), detiene WebTV (stop-webtv) y detiene el servidor RTMP (stop-serv-confirm).
    - start: Activa schedule (run_status=1), inicia servidor RTMP (start-serv-confirm), inicia WebTV (start-webtv) y verifica playout.
    - restart: Ejecuta secuencia de stop y luego secuencia de start.
    """
    vdo_user = os.getenv("VDOPANEL_USER")
    vdo_pass = os.getenv("VDOPANEL_PASS")
    
    if not vdo_user or not vdo_pass or vdo_user == "tu_usuario_panel_aqui":
        logger.error("Credenciales de VDO Panel no configuradas.")
        return False, "❌ Credenciales de VDO Panel no configuradas en el entorno."
        
    action_type = "start"
    if "stop" in action_url.lower():
        action_type = "stop"
    elif "restart" in action_url.lower():
        action_type = "restart"
        
    login_url = "https://stream.conexion.com.ve/broadcaster/login"
    ajax_url = "https://stream.conexion.com.ve/broadcaster/ajax-get-current-running"
    headers_common = {
        "Referer": "https://stream.conexion.com.ve/broadcaster",
        "User-Agent": "Mozilla/5.0"
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            # 1. Login
            async with session.get(login_url) as resp:
                html = await resp.text()
            soup = BeautifulSoup(html, 'html.parser')
            payload = {"name": vdo_user, "password": vdo_pass}
            for hidden in soup.find_all("input", type="hidden"):
                if hidden.get("name"):
                    payload[hidden.get("name")] = hidden.get("value")
                    
            async with session.post(login_url, data=payload) as resp:
                if resp.status not in [200, 302] or str(resp.url).endswith("/login"):
                    logger.error(f"Fallo al loguear en VDO Panel: {resp.status} - {resp.url}")
                    return False, f"❌ Fallo al autenticar en VDO Panel (HTTP {resp.status})."
                    
            # 2. Funciones auxiliares para acciones coordinadas
            async def do_stop():
                logger.info("[VDO Control] 1/3 Desactivando estado de programación (run_status = 0)...")
                await set_vdo_schedule_status(session, run_status=0)
                logger.info("[VDO Control] 2/3 Deteniendo WebTV scheduler...")
                async with session.get("https://stream.conexion.com.ve/broadcaster/stop-webtv", headers=headers_common) as r:
                    logger.info(f"[VDO Control] stop-webtv: HTTP {r.status}")
                logger.info("[VDO Control] 3/3 Deteniendo servicio RTMP...")
                async with session.get("https://stream.conexion.com.ve/broadcaster/stop-serv-confirm", headers=headers_common) as r:
                    logger.info(f"[VDO Control] stop-serv-confirm: HTTP {r.status}")

            async def do_start():
                logger.info("[VDO Control] 1/3 Activando estado de programación (run_status = 1)...")
                await set_vdo_schedule_status(session, run_status=1)
                logger.info("[VDO Control] 2/3 Iniciando servicio RTMP...")
                async with session.get("https://stream.conexion.com.ve/broadcaster/start-serv-confirm", headers=headers_common) as r:
                    logger.info(f"[VDO Control] start-serv-confirm: HTTP {r.status}")
                await asyncio.sleep(2)
                logger.info("[VDO Control] 3/3 Iniciando WebTV scheduler...")
                async with session.get("https://stream.conexion.com.ve/broadcaster/start-webtv", headers=headers_common) as r:
                    logger.info(f"[VDO Control] start-webtv: HTTP {r.status}")

            # 3. Ejecución coordinada
            if action_type == "stop":
                await do_stop()
                
                # Polling para verificar Offline
                for _ in range(5):
                    await asyncio.sleep(2)
                    async with session.get(ajax_url, headers={"X-Requested-With": "XMLHttpRequest", "User-Agent": "Mozilla/5.0"}) as resp:
                        if resp.status == 200:
                            data = await resp.json(content_type=None)
                            if isinstance(data, dict):
                                st = data.get("options", {}).get("status_name", "")
                                if str(st).lower() == "offline":
                                    return True, "✅ Stream **DETENIDO** exitosamente.\n\n🔴 **Estado:** `Offline`\n📅 **Programador:** `Desactivado (Off)`\n📺 **WebTV:** `Detenido`"
                return True, "✅ Orden de detención coordinada enviada exitosamente a VDO Panel."

            elif action_type == "restart":
                await do_stop()
                await asyncio.sleep(3)
                await do_start()

            elif action_type == "start":
                await do_start()

            # 4. Sondeo de verificación de emisión en vivo (hasta 24 segundos, chequeo cada 2s)
            is_live = False
            curr_opts = {}
            for attempt in range(12):
                await asyncio.sleep(2)
                async with session.get(ajax_url, headers={"X-Requested-With": "XMLHttpRequest", "User-Agent": "Mozilla/5.0"}) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        if isinstance(data, dict):
                            curr_opts = data.get("options", {})
                            if isinstance(curr_opts, dict):
                                curr_status = curr_opts.get("status_name", "Offline")
                                if str(curr_status).lower() == "live":
                                    is_live = True
                                    break
                                    
            act_word = "INICIADO" if action_type == "start" else "REINICIADO"
            if is_live:
                pl_name = curr_opts.get("playli_stname", "MUSICALNEW")
                vid_name = curr_opts.get("video_name", "En cola")
                return True, (
                    f"✅ Stream **{act_word}** coordinadamente y emitiendo en vivo.\n\n"
                    f"🟢 **Estado:** `Live`\n"
                    f"📅 **Programador:** `Activo (On)`\n"
                    f"📋 **Playlist:** `{pl_name}`\n"
                    f"🎬 **Video actual:** `{vid_name}`"
                )
            else:
                curr_status = curr_opts.get("status_name", "Offline") if curr_opts else "Offline"
                return False, (
                    f"⚠️ Se enviaron las órdenes coordinadas de inicio ({act_word}), pero VDO Panel reporta estado: `{curr_status}`.\n"
                    f"Por favor verifica si la lista o el codificador tienen algún problema en el panel."
                )
    except Exception as e:
        logger.error(f"Excepción en control_vdo_panel: {e}")
        return False, f"❌ Excepción técnica al comunicar con VDO Panel: {e}"

async def get_vdo_stream_status() -> dict | None:
    vdo_user = os.getenv("VDOPANEL_USER")
    vdo_pass = os.getenv("VDOPANEL_PASS")
    
    if not vdo_user or not vdo_pass or vdo_user == "tu_usuario_panel_aqui":
        return None
        
    login_url = "https://stream.conexion.com.ve/broadcaster/login"
    ajax_url = "https://stream.conexion.com.ve/broadcaster/ajax-get-current-running"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(login_url) as resp:
                html = await resp.text()
            soup = BeautifulSoup(html, 'html.parser')
            payload = {"name": vdo_user, "password": vdo_pass}
            for hidden in soup.find_all("input", type="hidden"):
                if hidden.get("name"):
                    payload[hidden.get("name")] = hidden.get("value")
            async with session.post(login_url, data=payload) as resp:
                if resp.status not in [200, 302] or str(resp.url).endswith("/login"):
                    return None
                    
            async with session.get(ajax_url, headers={"X-Requested-With": "XMLHttpRequest", "User-Agent": "Mozilla/5.0"}) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    return data.get("options", {})
    except Exception as e:
        logger.error(f"Error consultando estado del stream: {e}")
    return None

async def add_files_to_vdo_playlist(target_folder: str, file_name: str, playlist_id: int = None, copies: int = 10) -> tuple[bool, int]:
    vdo_user = os.getenv("VDOPANEL_USER")
    vdo_pass = os.getenv("VDOPANEL_PASS")
    
    if playlist_id is None:
        try:
            playlist_id = int(os.getenv("VDOPANEL_PLAYLIST_ID", "12"))
        except ValueError:
            playlist_id = 12
            
    if not vdo_user or not vdo_pass or vdo_user == "tu_usuario_panel_aqui":
        logger.error("Credenciales de VDO Panel no configuradas.")
        return False, 0
        
    login_url = "https://stream.conexion.com.ve/broadcaster/login"
    folder_clean = target_folder.strip("/") if target_folder else ""
    fm_url = f"https://stream.conexion.com.ve/broadcaster/filemanager?p={folder_clean}"
    batch_url = "https://stream.conexion.com.ve/broadcaster/filemanager/batch"
    add_url = "https://stream.conexion.com.ve/broadcaster/addtoplaylistpost"
    
    name, ext = os.path.splitext(file_name)
    
    try:
        async with aiohttp.ClientSession() as session:
            # 1. Login a VDO Panel
            async with session.get(login_url) as resp:
                html = await resp.text()
                
            soup = BeautifulSoup(html, 'html.parser')
            payload = {
                "name": vdo_user,
                "password": vdo_pass
            }
            
            for hidden in soup.find_all("input", type="hidden"):
                name_attr = hidden.get("name")
                val_attr = hidden.get("value")
                if name_attr:
                    payload[name_attr] = val_attr
                    
            async with session.post(login_url, data=payload) as resp:
                if resp.status not in [200, 302] or str(resp.url).endswith("/login"):
                    logger.error(f"Fallo al loguear en VDO Panel: {resp.status} - {resp.url}")
                    return False, 0
                    
            # 2. GET filemanager para obtener el token CSRF y los archivos reales del servidor
            fm_token = None
            existing_fm_files = set()
            async with session.get(fm_url) as resp:
                if resp.status == 200:
                    s = BeautifulSoup(await resp.text(), 'html.parser')
                    inp = s.find('input', {'name': '_token'})
                    if inp:
                        fm_token = inp.get('value')
                    for f_inp in s.find_all('input', {'name': 'file[]'}):
                        val = f_inp.get('value')
                        if val:
                            existing_fm_files.add(val)
                        
            if not fm_token:
                for cookie in session.cookie_jar:
                    if cookie.key == "XSRF-TOKEN":
                        fm_token = urllib.parse.unquote(cookie.value)
                        break
                        
            if not fm_token:
                logger.error("No se pudo obtener el CSRF token de Filemanager.")
                return False, 0
                
            # Resolver inteligentemente los nombres exactos presentes en el FileManager
            name, ext = os.path.splitext(file_name)
            sanitized_name = sanitize_filename(file_name)
            s_name, s_ext = os.path.splitext(sanitized_name)
            
            if copies <= 1:
                expected_copies = [s_name + s_ext]
            else:
                expected_copies = [s_name + s_ext] + [f"{s_name}-{i}{s_ext}" for i in range(1, copies)]
            resolved_files = []
            
            for i, exp in enumerate(expected_copies):
                if exp in existing_fm_files:
                    resolved_files.append(exp)
                else:
                    cand = None
                    raw_target = file_name if i == 0 else f"{name}-{i}{ext}"
                    for alt in [
                        raw_target,
                        raw_target.replace(" ", "-"),
                        raw_target.replace(" ", "_"),
                        raw_target.replace("_", "-"),
                        raw_target.replace("-", "_")
                    ]:
                        if alt in existing_fm_files:
                            cand = alt
                            break
                            
                    if not cand:
                        for ef in existing_fm_files:
                            if i == 0 and (ef == s_name + s_ext or ef.startswith(s_name)):
                                cand = ef
                                break
                            elif i > 0 and (ef == f"{s_name}-{i}{s_ext}" or ef.endswith(f"-{i}{s_ext}")):
                                cand = ef
                                break
                                
                    resolved_files.append(cand if cand else exp)
                
            # 3. POST a filemanager/batch (Paso 1 del batch)
            root_path = f"/home/{vdo_user}/uploads/{folder_clean}" if folder_clean else f"/home/{vdo_user}/uploads"
            batch_post_data = [
                ('_token', fm_token),
                ('p', folder_clean),
                ('batch_action', 'playlist'),
                ('FM_ROOT_PATH', root_path)
            ]
            for f in resolved_files:
                batch_post_data.append(('file[]', f))
                
            headers_step1 = {
                "Referer": fm_url,
                "Origin": "https://stream.conexion.com.ve",
                "Content-Type": "application/x-www-form-urlencoded"
            }
            
            step2_token = fm_token
            step2_files = []
            
            logger.info(f"Enviando {len(resolved_files)} archivo(s) a {batch_url} (Archivos: {resolved_files})...")
            async with session.post(batch_url, data=batch_post_data, headers=headers_step1) as resp:
                if resp.status in [200, 302]:
                    batch_html = await resp.text()
                    batch_soup = BeautifulSoup(batch_html, 'html.parser')
                    form_add = batch_soup.find('form')
                    if form_add:
                        for inp in form_add.find_all('input'):
                            if inp.get('name') == '_token':
                                step2_token = inp.get('value')
                            elif inp.get('name') == 'file[]':
                                step2_files.append(inp.get('value'))
                                
            if not step2_files:
                step2_files = [f"{folder_clean}/{f}" if folder_clean else f for f in resolved_files]
                
            # 4. POST a addtoplaylistpost (Paso 2 del batch)
            post_add_data = [
                ('_token', step2_token)
            ]
            for sf in step2_files:
                post_add_data.append(('file[]', sf))
            post_add_data.append(('playlist', str(playlist_id)))
            
            headers_step2 = {
                "Referer": batch_url,
                "Origin": "https://stream.conexion.com.ve",
                "Content-Type": "application/x-www-form-urlencoded"
            }
            
            logger.info(f"Confirmando adición a playlist {playlist_id} en {add_url}...")
            async with session.post(add_url, data=post_add_data, headers=headers_step2, allow_redirects=False) as resp:
                logger.info(f"Respuesta final addtoplaylistpost: HTTP {resp.status}")
                if resp.status in [200, 302]:
                    logger.info(f"{len(step2_files)} archivo(s) agregado(s) exitosamente a la playlist {playlist_id}.")
                    return True, len(step2_files)
                else:
                    logger.error(f"Fallo al confirmar playlist en VDO Panel: HTTP {resp.status}")
                    return False, 0
    except Exception as e:
        logger.error(f"Excepción en add_files_to_vdo_playlist: {e}")
        return False, 0

@app.on_message(filters.incoming & ~filters.me & filters.command("start") & filters.private)
async def start(client: Client, message: Message):
    if not message.from_user or message.from_user.is_bot:
        return
    user_id = str(message.from_user.id)
    allowed_users = load_users()
    if user_id in allowed_users or is_admin(user_id):
        admin_extra = ""
        if is_admin(user_id):
            admin_extra = (
                "\n\n🔗 **Redes Sociales (solo admin):**\n"
                "📺 `/conectar_youtube` - Vincular el canal de YouTube\n"
                "🎵 `/conectar_tiktok` - Vincular la cuenta de TikTok\n"
                "📡 `/redes` - Ver el estado de las cuentas"
            )
        await message.reply_text(
            "👋 ¡Hola! Envíame un video y lo subiré automáticamente a tu carpeta en el dashboard.\n\n"
            "📺 **Comandos de Transmisión (VDO Panel):**\n"
            "▶️ `/iniciar` - Iniciar streaming\n"
            "⏹️ `/detener` - Detener streaming\n"
            "🔄 `/reiniciar` - Reiniciar streaming\n"
            "📡 `/estado` - Consultar estado actual del streaming"
            + admin_extra
        )
    else:
        await message.reply_text(
            f"👋 ¡Hola! No estás autorizado aún.\n"
            f"Pide acceso al administrador indicando tu ID numérico: `{user_id}`"
        )

@app.on_message(filters.incoming & ~filters.me & filters.command("iniciar") & filters.private)
async def cmd_start_stream(client: Client, message: Message):
    if not message.from_user or message.from_user.is_bot:
        return
    user_id = str(message.from_user.id)
    allowed_users = load_users()
    
    if user_id not in allowed_users and not is_admin(user_id):
        await message.reply_text("❌ No estás autorizado para controlar el stream.")
        return
        
    msg = await message.reply_text("⏳ Intentando **INICIAR** el stream en VDO Panel y sincronizar la programación...")
    success, res_text = await control_vdo_panel("https://stream.conexion.com.ve/broadcaster/start-serv-confirm")
    await msg.edit_text(res_text)

@app.on_message(filters.incoming & ~filters.me & filters.command("detener") & filters.private)
async def cmd_stop_stream(client: Client, message: Message):
    if not message.from_user or message.from_user.is_bot:
        return
    user_id = str(message.from_user.id)
    allowed_users = load_users()
    
    if user_id not in allowed_users and not is_admin(user_id):
        await message.reply_text("❌ No estás autorizado para controlar el stream.")
        return
        
    msg = await message.reply_text("⏳ Intentando **DETENER** el stream en VDO Panel...")
    success, res_text = await control_vdo_panel("https://stream.conexion.com.ve/broadcaster/stop-serv-confirm")
    await msg.edit_text(res_text)

@app.on_message(filters.incoming & ~filters.me & filters.command("reiniciar") & filters.private)
async def cmd_restart_stream(client: Client, message: Message):
    if not message.from_user or message.from_user.is_bot:
        return
    user_id = str(message.from_user.id)
    allowed_users = load_users()
    
    if user_id not in allowed_users and not is_admin(user_id):
        await message.reply_text("❌ No estás autorizado para controlar el stream.")
        return
        
    msg = await message.reply_text("⏳ Intentando **REINICIAR** el stream en VDO Panel y sincronizar la programación...")
    success, res_text = await control_vdo_panel("https://stream.conexion.com.ve/broadcaster/restart-serv-confirm")
    await msg.edit_text(res_text)

@app.on_message(filters.incoming & ~filters.me & filters.command("estado") & filters.private)
async def cmd_status_stream(client: Client, message: Message):
    if not message.from_user or message.from_user.is_bot:
        return
    user_id = str(message.from_user.id)
    allowed_users = load_users()
    
    if user_id not in allowed_users and not is_admin(user_id):
        await message.reply_text("❌ No estás autorizado para consultar el estado del stream.")
        return
        
    msg = await message.reply_text("⏳ Consultando estado en VDO Panel...")
    status_info = await get_vdo_stream_status()
    
    if status_info is not None:
        status_name = status_info.get("status_name", "Desconocido")
        playlist = status_info.get("playli_stname", "Ninguna")
        video = status_info.get("video_name", "Ninguno")
        
        status_emoji = "🟢" if str(status_name).lower() == "live" else "🔴"
        text = (
            f"📡 **ESTADO DE LA TRANSMISIÓN**\n\n"
            f"{status_emoji} **Estado:** `{status_name}`\n"
            f"📋 **Playlist:** `{playlist}`\n"
            f"🎬 **Video actual:** `{video}`"
        )
        await msg.edit_text(text)
    else:
        await msg.edit_text("❌ No se pudo obtener el estado de VDO Panel. Revisa las credenciales o conexión.")

async def edit_message_or_caption(message: Message, new_text: str, reply_markup=None):
    try:
        if message.video or message.document or message.photo or message.caption:
            await message.edit_caption(caption=new_text, reply_markup=reply_markup)
        else:
            await message.edit_text(text=new_text, reply_markup=reply_markup)
    except MessageNotModified:
        pass
    except Exception as e:
        logger.error(f"Error editando mensaje/caption: {e}")


# ==========================================
# HELPERS DE PUBLICACIÓN EN YOUTUBE / TIKTOK
# ==========================================

def _safe_html(value) -> str:
    return html.escape(str(value or ""), quote=False)


def _youtube_title_preview(caption: str, file_name: str = "Video") -> str:
    text = (caption or "").strip()
    title = text.splitlines()[0] if text else os.path.splitext(file_name or "Video")[0]
    title = title.replace("<", " ").replace(">", " ").strip() or "Video"
    return title[:100]


def _social_caption(info: dict, platform: str) -> str:
    caption = (info.get(f"{platform}_caption") or info.get("caption") or "").strip()
    if not caption:
        caption = os.path.splitext(info.get("file_name", "video"))[0]
    return caption


def _youtube_privacy(info: dict) -> str:
    value = str(
        info.get("youtube_privacy")
        or os.getenv("YOUTUBE_PRIVACY_STATUS", "public")
    ).strip().lower()
    return value if value in ("public", "unlisted", "private") else "public"


def _youtube_privacy_label(value: str) -> str:
    return {
        "public": "🌍 Público",
        "unlisted": "🔗 No listado",
        "private": "🔒 Privado",
    }.get(value, value)


def _tiktok_privacy(info: dict) -> str:
    value = str(
        info.get("tiktok_privacy")
        or os.getenv("TIKTOK_PRIVACY_LEVEL", "SELF_ONLY")
    ).strip()
    return value or "SELF_ONLY"


def _tiktok_privacy_label(value: str) -> str:
    return social_media.TIKTOK_PRIVACY_LABELS.get(value, value)


def _replace_callback_button(markup, prefixes, new_label, new_callback):
    """Reemplaza el botón que empieza con alguno de los prefijos."""
    if not markup or not getattr(markup, "inline_keyboard", None):
        return None
    rows = []
    changed = False
    for row in markup.inline_keyboard:
        new_row = []
        for btn in row:
            if btn.callback_data and any(
                btn.callback_data.startswith(prefix) for prefix in prefixes
            ):
                new_row.append(
                    InlineKeyboardButton(new_label, callback_data=new_callback)
                )
                changed = True
            else:
                new_row.append(btn)
        rows.append(new_row)
    return InlineKeyboardMarkup(rows) if changed else None


async def _update_video_button(video_msg, prefixes, new_label, new_callback):
    try:
        markup = getattr(video_msg, "reply_markup", None)
        new_markup = _replace_callback_button(
            markup, prefixes, new_label, new_callback
        )
        if new_markup:
            await video_msg.edit_reply_markup(reply_markup=new_markup)
    except Exception as e:
        logger.warning(f"No se pudo actualizar el botón del video: {e}")


def _download_from_ftp(folder: str, filename: str) -> str:
    dest_dir = tempfile.mkdtemp(prefix="social_publish_")
    local_dest = os.path.join(dest_dir, sanitize_filename(filename))
    ftp = None
    try:
        ftp = FTP(FTP_HOST)
        ftp.login(user=FTP_USER, passwd=FTP_PASS)
        if folder:
            ftp.cwd(str(folder).strip("/"))
        with open(local_dest, "wb") as f_out:
            ftp.retrbinary(f"RETR {filename}", f_out.write)
        return local_dest
    finally:
        if ftp:
            try:
                ftp.quit()
            except Exception:
                pass


async def resolve_publish_video(client: Client, target_message: Message, file_id: str):
    """Obtiene el video desde Telegram o, si no existe, desde el FTP."""
    deletions = load_deletions()
    info = deletions.get(file_id, {})
    if not info:
        raise Exception("El video ya no está en el registro.")

    file_name = info.get("file_name", "video.mp4")
    tg_file_id = info.get("telegram_file_id")
    local_path = None
    temp_dir = tempfile.mkdtemp(prefix="social_publish_")
    download_target = os.path.join(temp_dir, sanitize_filename(file_name))

    if tg_file_id:
        logger.info(f"[Redes] Descargando media de Telegram: {tg_file_id}")
        try:
            local_path = await client.download_media(
                tg_file_id, file_name=download_target
            )
        except Exception as e:
            logger.warning(f"[Redes] No se pudo descargar de Telegram: {e}")
            local_path = None

    if not local_path and target_message and (
        target_message.video or target_message.document
    ):
        logger.info("[Redes] Descargando media desde el mensaje del video")
        try:
            local_path = await target_message.download(file_name=download_target)
        except Exception as e:
            logger.warning(f"[Redes] No se pudo descargar el mensaje: {e}")
            local_path = None

    if not local_path:
        try:
            os.rmdir(temp_dir)
        except Exception:
            pass
        ftp_folder = info.get("ftp_path", "")
        logger.info(f"[Redes] Descargando respaldo desde FTP: {ftp_folder}/{file_name}")
        local_path = await asyncio.to_thread(
            _download_from_ftp, ftp_folder, file_name
        )

    if not local_path or not os.path.exists(local_path):
        raise Exception(
            "No se pudo obtener el video desde Telegram ni desde el FTP."
        )
    return local_path, info


def _cleanup_temp_video(path):
    """Borra solo descargas temporales, nunca archivos del proyecto."""
    if not path or not os.path.exists(path):
        return
    abs_path = os.path.abspath(path)
    parent = os.path.dirname(abs_path)
    temp_root = os.path.abspath(tempfile.gettempdir())
    downloads_root = os.path.abspath(os.path.join(os.getcwd(), "downloads"))
    is_temp = (
        abs_path.startswith(temp_root + os.sep)
        or parent.startswith(downloads_root + os.sep)
        or os.path.basename(parent).startswith("social_publish_")
    )
    if not is_temp:
        logger.warning(
            "No se elimina %s: no parece un archivo temporal de publicación.",
            abs_path,
        )
        return
    try:
        os.remove(abs_path)
        if os.path.basename(parent).startswith("social_publish_"):
            try:
                os.rmdir(parent)
            except Exception:
                pass
    except Exception:
        pass


async def send_youtube_panel(client: Client, source_message: Message, file_id: str,
                             admin_id: int, video_message: Message = None,
                             edit: bool = False):
    deletions = load_deletions()
    info = deletions.get(file_id)
    if not info:
        await source_message.reply_text("❌ El video ya no está en el registro.")
        return

    caption = _social_caption(info, "youtube")
    privacy = _youtube_privacy(info)
    if not info.get("youtube_caption") or not info.get("youtube_privacy"):
        info["youtube_caption"] = caption
        info["youtube_privacy"] = privacy
        deletions[file_id] = info
        save_deletions(deletions)

    title = _youtube_title_preview(caption, info.get("file_name", "Video"))
    preview = caption[:600] + ("..." if len(caption) > 600 else "")
    published_url = info.get("youtube_url")
    stored_youtube = social_media.load_tokens().get("youtube", {}) or {}
    channel_title = stored_youtube.get("channel_title", "")
    channel_line = (
        f"📺 <b>Canal:</b> {_safe_html(channel_title)}\n"
        if channel_title
        else ""
    )
    text = (
        "📺 <b>PUBLICAR EN YOUTUBE</b>\n\n"
        f"{channel_line}"
        f"📁 <b>Archivo:</b> <code>{_safe_html(info.get('file_name', ''))}</code>\n"
        f"📝 <b>Título:</b> {_safe_html(title)}\n"
        f"📄 <b>Descripción:</b>\n{_safe_html(preview or '(sin descripción)')}\n\n"
        f"🔒 <b>Privacidad:</b> {_youtube_privacy_label(privacy)}"
    )
    if published_url:
        text += f"\n\n⚠️ Ya se publicó antes: {_safe_html(published_url)}"
    if str(info.get("ftp_path", "")).strip("/").lower() == "youtube":
        text += (
            "\n\n⚠️ Este video se descargó de YouTube. Si lo publicas de nuevo, "
            "se subirá otra copia."
        )
    text += "\n\n¿Publicar este video en YouTube?"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Publicar en YouTube", callback_data=f"yt_go_{file_id}")],
        [InlineKeyboardButton("✏️ Editar texto", callback_data=f"yt_edit_{file_id}")],
        [
            InlineKeyboardButton("🌍 Público", callback_data=f"yt_priv_{file_id}_public"),
            InlineKeyboardButton("🔗 No listado", callback_data=f"yt_priv_{file_id}_unlisted"),
            InlineKeyboardButton("🔒 Privado", callback_data=f"yt_priv_{file_id}_private"),
        ],
        [InlineKeyboardButton("❌ Cancelar", callback_data=f"yt_cancel_{file_id}")],
    ])
    if edit:
        try:
            await source_message.edit_text(
                text, reply_markup=keyboard, parse_mode=enums.ParseMode.HTML
            )
        except MessageNotModified:
            pass
        except Exception:
            await source_message.reply_text(
                text, reply_markup=keyboard, parse_mode=enums.ParseMode.HTML
            )
    else:
        await source_message.reply_text(text, reply_markup=keyboard, parse_mode=enums.ParseMode.HTML)

    if video_message is not None:
        admin_states[admin_id] = {
            "action": "youtube_panel",
            "file_id": file_id,
            "video_message": video_message,
        }


async def send_tiktok_panel(client: Client, source_message: Message, file_id: str,
                            admin_id: int, video_message: Message = None,
                            edit: bool = False):
    deletions = load_deletions()
    info = deletions.get(file_id)
    if not info:
        await source_message.reply_text("❌ El video ya no está en el registro.")
        return

    mode = social_media.tiktok_mode()
    if mode == "none":
        await source_message.reply_text(
            "❌ TikTok no está conectado o la cuenta no tiene permisos para "
            "publicar. Usa /conectar_tiktok primero.",
            parse_mode=enums.ParseMode.HTML,
        )
        return

    caption = _social_caption(info, "tiktok")
    privacy = _tiktok_privacy(info)
    options = []
    warning = ""

    if mode == "direct":
        try:
            creator = await asyncio.to_thread(social_media.tiktok_creator_info)
            options = creator.get("privacy_level_options") or []
            if options and privacy not in options:
                privacy = "SELF_ONLY" if "SELF_ONLY" in options else options[0]
            if "SELF_ONLY" in options:
                warning = (
                    "ℹ️ Si la app no está auditada por TikTok, la única "
                    "privacidad disponible será «Solo yo»."
                )
        except Exception as e:
            warning = f"⚠️ No se pudo consultar la cuenta de TikTok: {_safe_html(e)}"
    else:
        privacy = "SELF_ONLY"
        warning = (
            "ℹ️ La app tiene permiso <b>video.upload</b>: el video se enviará "
            "a la bandeja de entrada de TikTok. El cliente deberá abrirlo, "
            "escribir el texto, elegir la privacidad y confirmar la publicación.\n"
            "⚠️ Si la app no está auditada, TikTok puede limitar la visibilidad "
            "a privado."
        )

    if not info.get("tiktok_caption") or not info.get("tiktok_privacy"):
        info["tiktok_caption"] = caption
        info["tiktok_privacy"] = privacy
        deletions[file_id] = info
        save_deletions(deletions)
    elif privacy != info.get("tiktok_privacy"):
        info["tiktok_privacy"] = privacy
        deletions[file_id] = info
        save_deletions(deletions)

    stored_tiktok = social_media.load_tokens().get("tiktok", {}) or {}
    tt_username = stored_tiktok.get("username", "")
    tt_nickname = stored_tiktok.get("nickname", "")
    tt_account = (
        f"@{tt_username}"
        if tt_username
        else (tt_nickname or "(cuenta conectada)")
    )
    text = (
        "🎵 <b>PUBLICAR EN TIKTOK</b>\n\n"
        f"🎵 <b>Cuenta:</b> {_safe_html(tt_account)}\n"
        f"📁 <b>Archivo:</b> <code>{_safe_html(info.get('file_name', ''))}</code>\n"
        f"📝 <b>Texto:</b>\n{_safe_html(caption[:1200] or '(sin texto)')}\n\n"
    )
    if mode == "direct":
        text += f"🔒 <b>Privacidad:</b> {_tiktok_privacy_label(privacy)}\n"
    else:
        text += "📨 <b>Modo:</b> Bandeja de entrada de TikTok\n"
    if warning:
        text += f"\n{warning}\n"
    if info.get("tiktok_pending"):
        text += (
            "\n⏳ TikTok todavía está procesando el envío anterior. "
            "No lo envíes de nuevo para evitar duplicados.\n"
        )
    elif info.get("tiktok_url"):
        text += f"\n⚠️ Ya se publicó antes: {_safe_html(info['tiktok_url'])}\n"
    elif info.get("tiktok_publish_id"):
        text += (
            "\n⚠️ Este video ya se envió antes a TikTok. Si lo envías de nuevo, "
            "se creará otro borrador.\n"
        )
    text += "\n¿Enviar este video a TikTok?"

    publish_label = (
        "✅ Publicar en TikTok" if mode == "direct" else "📨 Enviar a TikTok"
    )
    rows = [[InlineKeyboardButton(publish_label, callback_data=f"tt_go_{file_id}")],
            [InlineKeyboardButton("✏️ Editar texto", callback_data=f"tt_edit_{file_id}")]]
    if mode == "direct" and options:
        privacy_row = []
        for option in options:
            privacy_row.append(
                InlineKeyboardButton(
                    _tiktok_privacy_label(option),
                    callback_data=f"tt_priv_{file_id}_{option}",
                )
            )
        rows.append(privacy_row)
    rows.append([InlineKeyboardButton("❌ Cancelar", callback_data=f"tt_cancel_{file_id}")])
    keyboard = InlineKeyboardMarkup(rows)

    if edit:
        try:
            await source_message.edit_text(
                text, reply_markup=keyboard, parse_mode=enums.ParseMode.HTML
            )
        except MessageNotModified:
            pass
        except Exception:
            await source_message.reply_text(
                text, reply_markup=keyboard, parse_mode=enums.ParseMode.HTML
            )
    else:
        await source_message.reply_text(text, reply_markup=keyboard, parse_mode=enums.ParseMode.HTML)

    if video_message is not None:
        admin_states[admin_id] = {
            "action": "tiktok_panel",
            "file_id": file_id,
            "video_message": video_message,
        }


async def execute_youtube_publish(client: Client, video_message: Message, file_id: str,
                                  status_message: Message = None):
    deletions = load_deletions()
    info = deletions.get(file_id, {})
    caption = _social_caption(info, "youtube")
    privacy = _youtube_privacy(info)
    dry_run = os.getenv("SOCIAL_DRY_RUN", "False").lower() in ("1", "true", "yes", "si", "sí")

    lock_key = f"youtube:{file_id}"
    if lock_key in social_publish_in_progress:
        if status_message is not None:
            try:
                await status_message.edit_text(
                    "⏳ Ya hay una publicación en curso para este video."
                )
            except Exception:
                pass
        return None

    if status_message is None:
        status_message = await video_message.reply_text(
            "⏳ Preparando el video para YouTube..."
        )
    else:
        try:
            await status_message.edit_text("⏳ Preparando el video para YouTube...")
        except Exception:
            pass

    social_publish_in_progress.add(lock_key)
    temp_path = None
    try:
        temp_path, _ = await resolve_publish_video(client, video_message, file_id)
        try:
            await status_message.edit_text(
                "⏳ Subiendo video a YouTube... Esto puede tardar varios minutos."
            )
        except Exception:
            pass
        result = await asyncio.to_thread(
            social_media.publish_video_to_youtube,
            temp_path,
            caption,
            None,
            None,
            privacy,
            None,
            dry_run,
        )

        deletions = load_deletions()
        if file_id in deletions:
            deletions[file_id]["youtube_caption"] = caption
            deletions[file_id]["youtube_privacy"] = result.get("privacy", privacy)
            deletions[file_id]["youtube_dry_run"] = bool(result.get("dry_run"))
            if not result.get("dry_run"):
                deletions[file_id]["youtube_url"] = result.get("url", "")
                deletions[file_id]["youtube_video_id"] = result.get("video_id", "")
                deletions[file_id]["youtube_published_at"] = int(time.time())
            save_deletions(deletions)

        if result.get("dry_run"):
            # Se deja el botón activo para poder publicar de verdad luego.
            await _update_video_button(
                video_message, ("yt_pub_",),
                "📺 YouTube", f"yt_pub_{file_id}"
            )
            text = (
                "✅ <b>Prueba de YouTube completada</b>\n\n"
                f"📝 Título: {_safe_html(result.get('title', ''))}\n"
                f"🔒 Privacidad: {_youtube_privacy_label(result.get('privacy', privacy))}\n\n"
                "No se publicó nada porque SOCIAL_DRY_RUN está activo."
            )
        else:
            await _update_video_button(
                video_message, ("yt_pub_", "noop_yt_"),
                "✅ YouTube", f"noop_yt_{file_id}"
            )
            text = (
                "✅ <b>Video publicado en YouTube</b>\n\n"
                f"🎬 {_safe_html(result.get('title', ''))}\n"
                f"🔗 {_safe_html(result.get('url', ''))}\n"
                f"🔒 Privacidad: {_youtube_privacy_label(result.get('privacy', privacy))}"
            )
        done_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔁 Publicar de nuevo", callback_data=f"yt_pub_{file_id}")],
            [InlineKeyboardButton("✅ Listo", callback_data=f"noop_yt_{file_id}")],
        ])
        await status_message.edit_text(
            text,
            parse_mode=enums.ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=done_markup,
        )
        return result
    except social_media.SocialAuthError as e:
        text = (
            "❌ <b>YouTube no está conectado o el permiso venció.</b>\n\n"
            f"{_safe_html(e)}\n\n"
            "Usa /conectar_youtube para vincular el canal."
        )
        try:
            await status_message.edit_text(text, parse_mode=enums.ParseMode.HTML)
        except Exception:
            pass
    except social_media.SocialPublishError as e:
        try:
            await status_message.edit_text(
                f"❌ <b>No se pudo publicar en YouTube</b>\n\n{_safe_html(e)}",
                parse_mode=enums.ParseMode.HTML,
            )
        except Exception:
            pass
    except Exception as e:
        logger.error(f"[YouTube] Error publicando: {e}")
        try:
            await status_message.edit_text(
                f"❌ <b>Error inesperado al publicar en YouTube</b>\n\n{_safe_html(e)}",
                parse_mode=enums.ParseMode.HTML,
            )
        except Exception:
            pass
    finally:
        social_publish_in_progress.discard(lock_key)
        _cleanup_temp_video(temp_path)
    return None


async def execute_tiktok_publish(client: Client, video_message: Message, file_id: str,
                                 status_message: Message = None):
    deletions = load_deletions()
    info = deletions.get(file_id, {})
    caption = _social_caption(info, "tiktok")
    mode = social_media.tiktok_mode()
    privacy = _tiktok_privacy(info) if mode == "direct" else "SELF_ONLY"
    dry_run = os.getenv("SOCIAL_DRY_RUN", "False").lower() in ("1", "true", "yes", "si", "sí")

    lock_key = f"tiktok:{file_id}"
    if lock_key in social_publish_in_progress:
        if status_message is not None:
            try:
                await status_message.edit_text(
                    "⏳ Ya hay una publicación en curso para este video."
                )
            except Exception:
                pass
        return None

    if status_message is None:
        status_message = await video_message.reply_text(
            "⏳ Preparando el video para TikTok..."
        )
    else:
        try:
            await status_message.edit_text("⏳ Preparando el video para TikTok...")
        except Exception:
            pass

    social_publish_in_progress.add(lock_key)
    temp_path = None
    try:
        temp_path, _ = await resolve_publish_video(client, video_message, file_id)
        try:
            await status_message.edit_text(
                "⏳ Subiendo video a TikTok... Esto puede tardar varios minutos."
            )
        except Exception:
            pass
        result = await asyncio.to_thread(
            social_media.publish_video_to_tiktok,
            temp_path,
            caption,
            privacy,
            dry_run,
        )

        result_mode = result.get("mode", mode)
        deletions = load_deletions()
        if file_id in deletions:
            deletions[file_id]["tiktok_caption"] = caption
            deletions[file_id]["tiktok_dry_run"] = bool(result.get("dry_run"))
            deletions[file_id]["tiktok_pending"] = bool(result.get("pending"))
            deletions[file_id]["tiktok_mode"] = result_mode
            deletions[file_id]["tiktok_privacy"] = result.get("privacy", privacy)
            if not result.get("dry_run"):
                deletions[file_id]["tiktok_publish_id"] = result.get("publish_id", "")
                deletions[file_id]["tiktok_url"] = result.get("url", "")
                deletions[file_id]["tiktok_published_at"] = int(time.time())
            save_deletions(deletions)

        if result.get("dry_run"):
            # Se deja el botón activo para poder publicar de verdad luego.
            await _update_video_button(
                video_message, ("tt_pub_",),
                "🎵 TikTok", f"tt_pub_{file_id}"
            )
            text = (
                "✅ <b>Prueba de TikTok completada</b>\n\n"
                f"Modo: {'Publicación directa' if result_mode == 'direct' else 'Bandeja de entrada'}\n\n"
                "No se publicó nada porque SOCIAL_DRY_RUN está activo."
            )
        elif result.get("pending"):
            # Se deja el botón para abrir el panel y poder reintentar cuando
            # TikTok termine de moderar.
            await _update_video_button(
                video_message, ("tt_pub_", "noop_tt_"),
                "⏳ TikTok", f"tt_pub_{file_id}"
            )
            text = (
                "⏳ <b>TikTok está procesando el video</b>\n\n"
                "TikTok todavía está moderando el video. Revisa la app o el "
                "perfil en unos minutos. No lo envíes de nuevo para evitar "
                "duplicados."
            )
        elif result_mode == "inbox":
            await _update_video_button(
                video_message, ("tt_pub_", "noop_tt_"),
                "📨 TikTok", f"noop_tt_{file_id}"
            )
            text = (
                "📨 <b>Video enviado a la bandeja de entrada de TikTok</b>\n\n"
                "Pide al cliente que abra TikTok, revise la notificación, "
                "escriba el texto y confirme la publicación.\n\n"
                f"📝 Texto sugerido:\n{_safe_html(caption)}\n\n"
                f"{_safe_html(result.get('warning', ''))}"
            )
        else:
            await _update_video_button(
                video_message, ("tt_pub_", "noop_tt_"),
                "✅ TikTok", f"noop_tt_{file_id}"
            )
            text = (
                "✅ <b>Video publicado en TikTok</b>\n\n"
                f"🔒 Privacidad: {_tiktok_privacy_label(result.get('privacy', privacy))}"
            )
            if result.get("url"):
                text += f"\n🔗 {_safe_html(result['url'])}"
            if result.get("warning"):
                text += f"\n\n⚠️ {_safe_html(result['warning'])}"
        done_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔁 Publicar de nuevo", callback_data=f"tt_pub_{file_id}")],
            [InlineKeyboardButton("✅ Listo", callback_data=f"noop_tt_{file_id}")],
        ])
        await status_message.edit_text(
            text,
            parse_mode=enums.ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=done_markup,
        )
        return result
    except social_media.SocialAuthError as e:
        text = (
            "❌ <b>TikTok no está conectado o el permiso venció.</b>\n\n"
            f"{_safe_html(e)}\n\n"
            "Usa /conectar_tiktok para vincular la cuenta."
        )
        try:
            await status_message.edit_text(text, parse_mode=enums.ParseMode.HTML)
        except Exception:
            pass
    except social_media.SocialPublishError as e:
        try:
            await status_message.edit_text(
                f"❌ <b>No se pudo publicar en TikTok</b>\n\n{_safe_html(e)}",
                parse_mode=enums.ParseMode.HTML,
            )
        except Exception:
            pass
    except Exception as e:
        logger.error(f"[TikTok] Error publicando: {e}")
        try:
            await status_message.edit_text(
                f"❌ <b>Error inesperado al publicar en TikTok</b>\n\n{_safe_html(e)}",
                parse_mode=enums.ParseMode.HTML,
            )
        except Exception:
            pass
    finally:
        social_publish_in_progress.discard(lock_key)
        _cleanup_temp_video(temp_path)
    return None


@app.on_message(filters.incoming & ~filters.me & (filters.video | filters.document))
async def handle_video(client: Client, message: Message):
    if not message.from_user or message.from_user.is_bot:
        return

    # Si es documento, verificar que sea un archivo de video (evitar procesar pdfs, zips, etc.)
    if message.document:
        mime = message.document.mime_type or ""
        doc_name = (message.document.file_name or "").lower()
        if not (mime.startswith("video/") or doc_name.endswith(('.mp4', '.mkv', '.mov', '.avi', '.webm', '.ts', '.m4v'))):
            return

    user_id = str(message.from_user.id)
    username = message.from_user.username
    
    # Verificación de seguridad
    allowed_users = load_users()
    
    is_allowed = False
    target_folder = ""
    user_name = "Usuario"
    
    if user_id in allowed_users:
        is_allowed = True
        target_folder = allowed_users[user_id].get("ftp_path", "")
        user_name = allowed_users[user_id].get("name", "Usuario")
    elif username:
        if username in allowed_users:
            is_allowed = True
            target_folder = allowed_users[username].get("ftp_path", "")
            user_name = allowed_users[username].get("name", "Usuario")
        elif f"@{username}" in allowed_users:
            is_allowed = True
            target_folder = allowed_users[f"@{username}"].get("ftp_path", "")
            user_name = allowed_users[f"@{username}"].get("name", "Usuario")
            
    if not is_allowed:
        logger.warning(f"Acceso denegado al usuario: {user_id} (@{username})")
        await message.reply_text(f"❌ No estás autorizado para subir videos. Pide acceso al administrador indicando tu ID numérico: `{user_id}` o tu usuario: `@{username or 'Sin usuario'}`")
        return

    # Extraer información del archivo (ya sea video o documento como MP4)
    file = message.video or message.document
    if not file:
        return
        
    file_size_mb = file.file_size / (1024 * 1024)
    raw_file_name = file.file_name if getattr(file, 'file_name', None) else f"video_{message.id}.mp4"
    file_name = sanitize_filename(raw_file_name)
    
    # Mensaje de estado
    status_msg = await message.reply_text(f"¡Hola {user_name}! ⏳ Descargando `{file_name}` ({file_size_mb:.2f} MB)...")
    
    try:
        # 1. Descargar archivo
        logger.info(f"Iniciando descarga de {file_name} ({file_size_mb:.2f} MB)")
        local_path = await message.download(file_name=file_name)
        file_name = os.path.basename(local_path)
        logger.info(f"Descarga completada: {local_path} (Nombre final: {file_name})")
        
        folder_display = target_folder if target_folder.startswith("/") else f"/{target_folder}"
        await status_msg.edit_text(f"⏳ Subiendo video a la carpeta `{folder_display}` del FTP...")
        
        # 2. Subir por FTP en un hilo separado para no bloquear el event loop de Telegram
        await asyncio.to_thread(upload_to_ftp, local_path, file_name, target_folder)
        
        # 3. Extraer audio y subir a Dropbox si está configurado
        dropbox_uploaded = False
        dropbox_audio_path = None
        try:
            base_name, _ = os.path.splitext(local_path)
            audio_path = f"{base_name}.mp3"
            if extract_audio_from_video(local_path, audio_path):
                dropbox_folder = os.getenv("DROPBOX_FOLDER", "/CNX-INFORMATIVA")
                dropbox_uploaded = await asyncio.to_thread(upload_audio_to_dropbox, audio_path, dropbox_folder)
                if dropbox_uploaded:
                    folder_clean = "/" + dropbox_folder.strip("/") if dropbox_folder.strip("/") else ""
                    audio_filename = os.path.basename(audio_path)
                    dropbox_audio_path = f"{folder_clean}/{audio_filename}"
                if os.path.exists(audio_path):
                    os.remove(audio_path)
        except Exception as e_dbx:
            logger.error(f"Error procesando audio para Dropbox: {e_dbx}")
        
        # 4. Eliminar archivo temporal local
        if os.path.exists(local_path):
            os.remove(local_path)
        
        try:
            success_text = f"✅ ¡Video subido exitosamente a la carpeta `{folder_display}`!\nArchivo: `{file_name}`"
            await status_msg.edit_text(success_text)
        except MessageNotModified:
            pass
        
        # Notificar a los administradores
        if ADMIN_CHAT_IDS:
            user_first = message.from_user.first_name or "Usuario"
            file_id = str(uuid.uuid4())[:8]
            
            tg_media = message.video or message.document
            tg_file_id = tg_media.file_id if tg_media else None

            deletions = load_deletions()
            deletions[file_id] = {
                "ftp_path": target_folder,
                "file_name": file_name,
                "delete_after": None,
                "caption": message.caption or "",
                "telegram_file_id": tg_file_id,
                "dropbox_audio_path": dropbox_audio_path
            }
            save_deletions(deletions)
            
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("📺 YouTube", callback_data=f"yt_pub_{file_id}"),
                    InlineKeyboardButton("🎵 TikTok", callback_data=f"tt_pub_{file_id}"),
                ],
                [InlineKeyboardButton("📲 Publicar en Redes (IG + FB)", callback_data=f"ig_pub_{file_id}")],
                [InlineKeyboardButton("🎵 Agregar a Lista Musical", callback_data=f"addpl_{file_id}")],
                [InlineKeyboardButton("2 Minutos (Test)", callback_data=f"exp_2m_{file_id}")],
                [InlineKeyboardButton("24 Horas", callback_data=f"exp_24h_{file_id}"),
                 InlineKeyboardButton("3 Días", callback_data=f"exp_3d_{file_id}")],
                [InlineKeyboardButton("1 Semana", callback_data=f"exp_1w_{file_id}"),
                 InlineKeyboardButton("Personalizada", callback_data=f"exp_cust_{file_id}")]
            ])
            
            caption_text = (
                f"🔔 **NUEVO VIDEO SUBIDO**\n\n"
                f"👤 **Usuario:** {user_name} ({user_first} - ID: `{user_id}`)\n"
                f"📁 **Archivo:** `{file_name}`\n"
                f"📂 **Destino:** `{folder_display}`\n\n"
                f"¿Deseas publicarlo en YouTube/TikTok, agregarlo a la lista o "
                f"programar su eliminación?"
            )
            
            for admin_chat_id in ADMIN_CHAT_IDS:
                try:
                    if message.video:
                        await client.send_video(
                            chat_id=admin_chat_id,
                            video=message.video.file_id,
                            caption=caption_text,
                            reply_markup=keyboard
                        )
                    elif message.document:
                        await client.send_document(
                            chat_id=admin_chat_id,
                            document=message.document.file_id,
                            caption=caption_text,
                            reply_markup=keyboard
                        )
                    else:
                        await client.send_message(
                            chat_id=admin_chat_id,
                            text=caption_text,
                            reply_markup=keyboard
                        )
                except Exception as e:
                    logger.error(f"No se pudo enviar notificación multimedia a admin {admin_chat_id}: {e}")
                    try:
                        await client.send_message(
                            chat_id=admin_chat_id,
                            text=caption_text,
                            reply_markup=keyboard
                        )
                    except Exception as e2:
                        logger.error(f"Tampoco se pudo enviar notificación de respaldo a admin {admin_chat_id}: {e2}")
        
    except MessageNotModified:
        pass
    except Exception as e:
        logger.error(f"Error procesando video: {e}")
        try:
            await status_msg.edit_text(f"❌ Ocurrió un error al procesar el video:\n`{str(e)}`")
        except Exception:
            pass

@app.on_callback_query(filters.regex(r"^addpl_"))
async def handle_add_playlist_callback(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    _, file_id = data.split("_", 1)
    
    deletions = load_deletions()
    if file_id not in deletions:
        await callback_query.answer("Este archivo ya no está en el registro.", show_alert=True)
        return
        
    info = deletions[file_id]
    target_folder = info.get("ftp_path", "")
    file_name = info.get("file_name", "")
    
    if not target_folder:
        target_folder = FTP_UPLOAD_PATH or "avances-informativos"
    
    copies = info.get("copies", 1 if str(target_folder).strip("/").lower() == "youtube" else 10)
    
    if copies == 1:
        await callback_query.answer("⏳ Agregando video a la Lista Musical...", show_alert=False)
    else:
        await callback_query.answer("⏳ Agregando las 10 copias a la Lista Musical...", show_alert=False)
    
    success, count = await add_files_to_vdo_playlist(target_folder, file_name, copies=copies)
    
    if success:
        deletions[file_id]["playlist_added"] = True
        save_deletions(deletions)
        
        btn_label = "✅ Agregado a la Lista" if copies == 1 else f"✅ Agregado ({count}/10)"
        current_markup = callback_query.message.reply_markup
        new_rows = []
        if current_markup and current_markup.inline_keyboard:
            for row in current_markup.inline_keyboard:
                new_row = []
                for btn in row:
                    if btn.callback_data and btn.callback_data.startswith("addpl_"):
                        new_row.append(InlineKeyboardButton(btn_label, callback_data=f"noop_{file_id}"))
                    else:
                        new_row.append(btn)
                new_rows.append(new_row)
        else:
            new_rows = [[InlineKeyboardButton(btn_label, callback_data=f"noop_{file_id}")]]
            
        try:
            await callback_query.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(new_rows))
        except Exception:
            pass
            
        if copies == 1:
            await callback_query.message.reply_text(f"🎵 ✅ **`{file_name}` agregado con éxito a la Lista Musical (VDO Panel)**.")
        else:
            await callback_query.message.reply_text(f"🎵 ✅ **{count}/10 copias de `{file_name}` agregadas con éxito a la Lista Musical (VDO Panel)**.")
    else:
        await callback_query.message.reply_text(f"❌ **Error:** No se pudo agregar `{file_name}` a la Lista Musical en VDO Panel. Revisa los logs.")

async def execute_meta_publish(client: Client, target_message: Message, file_id: str, final_caption: str):
    """Ejecuta la descarga del video y publicación simultánea en Instagram Reels y Facebook Page."""
    deletions = load_deletions()
    info = deletions.get(file_id, {})
    file_name = info.get("file_name", "video.mp4")
    tg_file_id = info.get("telegram_file_id")

    # Actualizar botón a estado procesando en el mensaje del video original si tiene botones
    current_markup = target_message.reply_markup
    processing_rows = []
    if current_markup and current_markup.inline_keyboard:
        for row in current_markup.inline_keyboard:
            new_row = []
            for btn in row:
                if btn.callback_data and (btn.callback_data.startswith("ig_") or btn.callback_data.startswith("noop_ig_")):
                    new_row.append(InlineKeyboardButton("⏳ Publicando en Redes (IG+FB)...", callback_data=f"noop_ig_{file_id}"))
                else:
                    new_row.append(btn)
            processing_rows.append(new_row)
    try:
        if processing_rows:
            await target_message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(processing_rows))
    except Exception:
        pass

    temp_video_path = None
    try:
        status_msg = await target_message.reply_text("⏳ Descargando video de Telegram para publicar en Redes...")
        
        # Descarga robusta: primero intentar con telegram_file_id directamente
        if tg_file_id:
            logger.info(f"[Meta] Descargando media mediante file_id: {tg_file_id}")
            temp_video_path = await client.download_media(tg_file_id)
        elif target_message.video or target_message.document:
            logger.info("[Meta] Descargando media desde target_message")
            temp_video_path = await target_message.download()
        else:
            # Fallback: descargar desde el FTP si fue un video importado (ej. YouTube)
            ftp_folder = info.get("ftp_path", "YouTube")
            logger.info(f"[Meta] Archivo sin medio en Telegram, descargando copia de respaldo desde FTP: {ftp_folder}/{file_name}")
            def download_from_ftp(folder: str, filename: str) -> str:
                os.makedirs("temp_ftp", exist_ok=True)
                local_dest = os.path.join("temp_ftp", filename)
                ftp = FTP(FTP_HOST)
                ftp.login(user=FTP_USER, passwd=FTP_PASS)
                if folder:
                    ftp.cwd(folder.strip("/"))
                with open(local_dest, "wb") as f_out:
                    ftp.retrbinary(f"RETR {filename}", f_out.write)
                ftp.quit()
                return local_dest
            temp_video_path = await asyncio.to_thread(download_from_ftp, ftp_folder, file_name)

        if not temp_video_path or not os.path.exists(temp_video_path):
            raise Exception("No se pudo obtener el archivo de video (ni desde Telegram ni desde FTP).")


        await status_msg.edit_text("⏳ Conectando con Meta (Instagram Reels + Facebook Page)...")
        results = await asyncio.to_thread(publish_video_to_meta, temp_video_path, final_caption)

        ig_res = results.get("instagram", {})
        fb_res = results.get("facebook", {})

        ig_success = ig_res.get("success", False)
        fb_success = fb_res.get("success", False)

        alert_parts = ["📲 **RESULTADO DE PUBLICACIÓN EN REDES SOCIALES**\n"]

        if ig_success:
            if ig_res.get("permalink") == "dry_run":
                alert_parts.append("📸 **Instagram:** ✅ Validado con éxito (Modo Simulación / Dry Run)")
            else:
                alert_parts.append(f"📸 **Instagram Reel:** ✅ Publicado\n🔗 {ig_res.get('permalink')}")
        else:
            alert_parts.append(f"📸 **Instagram Reel:** ❌ Error: `{ig_res.get('error', 'Desconocido')}`")

        alert_parts.append("")

        if fb_success:
            if fb_res.get("permalink") == "dry_run":
                alert_parts.append("👤 **Facebook:** ✅ Validado con éxito (Modo Simulación / Dry Run)")
            else:
                alert_parts.append(f"👤 **Facebook Fanpage:** ✅ Publicado\n🔗 {fb_res.get('permalink')}")
        else:
            alert_parts.append(f"👤 **Facebook Fanpage:** ❌ Error: `{fb_res.get('error', 'Desconocido')}`")

        if final_caption:
            alert_parts.append(f"\n📝 **Caption:**\n_{final_caption}_")

        alert_text = "\n".join(alert_parts)

        # Actualizar botón
        btn_label = "✅ Publicado en Redes" if (ig_success and fb_success) else ("⚠️ Publicado Parcial" if (ig_success or fb_success) else "❌ Error en Redes")
        success_rows = []
        if current_markup and current_markup.inline_keyboard:
            for row in current_markup.inline_keyboard:
                new_row = []
                for btn in row:
                    if btn.callback_data and (btn.callback_data.startswith("ig_") or btn.callback_data.startswith("noop_ig_")):
                        new_row.append(InlineKeyboardButton(btn_label, callback_data=f"noop_ig_{file_id}"))
                    else:
                        new_row.append(btn)
                success_rows.append(new_row)
        try:
            if success_rows:
                await target_message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(success_rows))
        except Exception:
            pass

        await status_msg.edit_text(alert_text)

    except Exception as e:
        logger.error(f"Error en publicación Meta: {e}")
        try:
            if current_markup:
                await target_message.edit_reply_markup(reply_markup=current_markup)
        except Exception:
            pass
        await target_message.reply_text(f"❌ Error inesperado: `{e}`")
    finally:
        if temp_video_path and os.path.exists(temp_video_path):
            try:
                os.remove(temp_video_path)
            except Exception:
                pass

async def execute_instagram_publish(client: Client, target_message: Message, file_id: str, final_caption: str):
    """Retrocompatibilidad."""
    await execute_meta_publish(client, target_message, file_id, final_caption)

@app.on_callback_query(filters.regex(r"^ig_pub_"))
async def handle_instagram_publish_callback(client: Client, callback_query: CallbackQuery):
    if not is_admin(callback_query.from_user.id):
        await callback_query.answer("❌ Solo administradores pueden publicar en redes.", show_alert=True)
        return

    data = callback_query.data
    _, _, file_id = data.partition("ig_pub_")

    deletions = load_deletions()
    info = deletions.get(file_id, {})
    file_name = info.get("file_name", "video.mp4")
    caption = info.get("caption", "")

    if not caption:
        msg = callback_query.message
        if msg.caption:
            caption = msg.caption.split("🔔 **NUEVO VIDEO SUBIDO**")[0].strip()
        if not caption:
            caption = os.path.splitext(file_name)[0]

    # Presentar opciones de confirmación/edición del caption
    preview_caption = caption if caption else "(Sin texto / Vacío)"
    text = (
        f"📲 **PUBLICACIÓN EN REDES SOCIALES**\n"
        f"Se publicará simultáneamente en:\n"
        f"• **Instagram Reels**\n"
        f"• **Facebook Fanpage** (Plataforma Conexión)\n\n"
        f"📝 **Caption actual:**\n"
        f"_{preview_caption}_\n\n"
        f"¿Deseas publicar con este texto o editarlo?"
    )

    confirm_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirmar y Publicar en Redes", callback_data=f"ig_go_{file_id}")],
        [InlineKeyboardButton("✏️ Editar Caption", callback_data=f"ig_edit_{file_id}")],
        [InlineKeyboardButton("❌ Cancelar", callback_data=f"ig_cancel_{file_id}")]
    ])

    admin_states[callback_query.from_user.id] = {
        "video_message": callback_query.message,
        "file_id": file_id
    }

    await callback_query.message.reply_text(text, reply_markup=confirm_keyboard)
    await callback_query.answer()

@app.on_callback_query(filters.regex(r"^ig_go_"))
async def handle_ig_go_callback(client: Client, callback_query: CallbackQuery):
    admin_id = callback_query.from_user.id
    if not is_admin(admin_id):
        await callback_query.answer("❌ No autorizado.", show_alert=True)
        return

    _, _, file_id = callback_query.data.partition("ig_go_")
    deletions = load_deletions()
    info = deletions.get(file_id, {})
    caption = info.get("caption", "")

    # Recuperar el mensaje del video original o fallback al mensaje de la consulta
    state = admin_states.pop(admin_id, {})
    video_msg = state.get("video_message", callback_query.message)

    await callback_query.answer("🚀 Iniciando publicación en redes...", show_alert=False)
    await callback_query.message.delete()
    await execute_meta_publish(client, video_msg, file_id, caption)

@app.on_callback_query(filters.regex(r"^ig_edit_"))
async def handle_ig_edit_callback(client: Client, callback_query: CallbackQuery):
    admin_id = callback_query.from_user.id
    if not is_admin(admin_id):
        await callback_query.answer("❌ No autorizado.", show_alert=True)
        return

    _, _, file_id = callback_query.data.partition("ig_edit_")
    prev_state = admin_states.get(admin_id, {})
    video_msg = prev_state.get("video_message", callback_query.message)

    admin_states[admin_id] = {
        "action": "awaiting_instagram_caption",
        "file_id": file_id,
        "video_message": video_msg
    }

    await callback_query.message.edit_text(
        "✏️ **Escribe en el chat el nuevo caption/texto para las Redes (IG + FB).**\n\n"
        "Puedes incluir hashtags (#) y menciones (@). En cuanto lo envíes, comenzará la publicación."
    )
    await callback_query.answer()

@app.on_callback_query(filters.regex(r"^ig_cancel_"))
async def handle_ig_cancel_callback(client: Client, callback_query: CallbackQuery):
    admin_id = callback_query.from_user.id
    if admin_id in admin_states:
        del admin_states[admin_id]
    await callback_query.message.delete()
    await callback_query.answer("Operación cancelada.")


@app.on_callback_query(filters.regex(r"^noop_ig_"))
async def handle_noop_ig_callback(client: Client, callback_query: CallbackQuery):
    await callback_query.answer("Este video ya fue procesado para las redes.", show_alert=True)

@app.on_callback_query(filters.regex(r"^noop_(?!yt_|tt_|ig_)"))
async def handle_noop_callback(client: Client, callback_query: CallbackQuery):
    await callback_query.answer("Este archivo ya fue agregado a la lista musical.", show_alert=True)

@app.on_callback_query(filters.regex(r"^exp_"))
async def handle_expiration_callback(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    _, action, file_id = data.split("_", 2)
    
    deletions = load_deletions()
    if file_id not in deletions:
        await callback_query.answer("Este archivo ya no está en el registro.", show_alert=True)
        return
        
    admin_id = callback_query.from_user.id
    current_content = callback_query.message.caption or callback_query.message.text or ""
    
    if action == "cust":
        admin_states[admin_id] = {"action": "awaiting_custom_date", "file_id": file_id}
        await edit_message_or_caption(
            callback_query.message,
            current_content + "\n\n✍️ **Por favor, escribe en el chat la cantidad de días** que debe durar el archivo (ej: `15`) o una fecha en formato `YYYY-MM-DD`."
        )
        await callback_query.answer()
        return
        
    seconds = 0
    text_confirm = ""
    if action == "2m":
        seconds = 120
        text_confirm = "2 Minutos"
    elif action == "5m":
        seconds = 300
        text_confirm = "5 Minutos"
    elif action == "24h":
        seconds = 86400
        text_confirm = "24 Horas"
    elif action == "3d":
        seconds = 86400 * 3
        text_confirm = "3 Días"
    elif action == "1w":
        seconds = 86400 * 7
        text_confirm = "1 Semana"
        
    delete_timestamp = time.time() + seconds
    deletions[file_id]["delete_after"] = delete_timestamp
    save_deletions(deletions)
    
    date_str = format_caracas_time(delete_timestamp)
    
    is_added = deletions[file_id].get("playlist_added", False)
    pl_label = "✅ Agregado a Lista Musical" if is_added else "🎵 Agregar a Lista Musical"
    pl_callback = f"noop_{file_id}" if is_added else f"addpl_{file_id}"
    pl_btn = InlineKeyboardButton(pl_label, callback_data=pl_callback)

    # Conservar los botones de YouTube, TikTok, IG/FB y autodestrucción.
    current_markup = callback_query.message.reply_markup
    new_rows = []
    playlist_found = False
    if current_markup and current_markup.inline_keyboard:
        for row in current_markup.inline_keyboard:
            new_row = []
            for btn in row:
                cb = btn.callback_data or ""
                is_playlist_btn = cb.startswith("addpl_") or (
                    cb.startswith("noop_")
                    and not cb.startswith(("noop_yt_", "noop_tt_", "noop_ig_"))
                )
                if is_playlist_btn:
                    new_row.append(pl_btn)
                    playlist_found = True
                else:
                    new_row.append(btn)
            new_rows.append(new_row)
    if not playlist_found:
        new_rows.append([pl_btn])

    await edit_message_or_caption(
        callback_query.message,
        current_content + f"\n\n✅ **Autodestrucción programada:**\nEl archivo se eliminará en {text_confirm} ({date_str} - Hora Vzla).",
        reply_markup=InlineKeyboardMarkup(new_rows)
    )
    await callback_query.answer("Programado exitosamente.")

# ==========================================
# CONEXIÓN DE CUENTAS DE YOUTUBE Y TIKTOK
# ==========================================

async def _poll_youtube_connection(client: Client, chat_id: int, flow: dict, flow_id: int):
    try:
        result = await asyncio.to_thread(
            social_media.youtube_device_poll,
            flow["device_code"],
            flow.get("interval", 5),
            flow.get("expires_in", 1800),
        )
        if youtube_connect_state.get("flow_id") != flow_id:
            return
        channel = result.get("channel", {}) or {}
        name = channel.get("channel_title") or "(canal sin nombre)"
        extra = ""
        if result.get("channel_error"):
            extra = (
                "\n\n⚠️ El permiso quedó guardado, pero no se pudo leer el canal: "
                f"{_safe_html(str(result['channel_error'])[:500])}\n"
                "Revisa que YouTube Data API v3 esté habilitada en Google Cloud."
            )
        expected_handle = os.getenv("YOUTUBE_CHANNEL_HANDLE", "").strip().lstrip("@").lower()
        connected_handle = (
            (channel.get("channel_custom_url") or "").strip().lstrip("@").lower()
        )
        if expected_handle and connected_handle and expected_handle != connected_handle:
            # No dejamos guardado un token de un canal equivocado: si el cliente
            # eligió su cuenta personal, no debe poder publicar ahí por error.
            social_media.disconnect("youtube")
            logger.warning(
                "YouTube conectado al canal equivocado: @%s (esperado @%s)",
                connected_handle,
                expected_handle,
            )
            await client.send_message(
                chat_id,
                "❌ <b>No se guardó la conexión de YouTube</b>\n\n"
                f"Se autorizó el canal <b>{_safe_html(name)}</b> "
                f"(@{_safe_html(connected_handle)}), pero se esperaba "
                f"<b>@{_safe_html(expected_handle)}</b>.\n\n"
                "Vuelve a ejecutar /conectar_youtube y elige el canal correcto. "
                "No se publicará nada en el canal equivocado.",
                parse_mode=enums.ParseMode.HTML,
            )
            return
        await client.send_message(
            chat_id,
            "✅ <b>YouTube conectado</b>\n\n"
            f"📺 Canal: {_safe_html(name)}\n"
            "Ya puedes publicar videos desde los botones de cada video."
            f"{extra}",
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception as e:
        if youtube_connect_state.get("flow_id") == flow_id:
            try:
                await client.send_message(
                    chat_id,
                    f"❌ <b>No se pudo conectar YouTube</b>\n\n{_safe_html(e)}\n\n"
                    "Genera un código nuevo con /conectar_youtube.",
                    parse_mode=enums.ParseMode.HTML,
                )
            except Exception as e2:
                logger.error(f"No se pudo notificar el error de YouTube: {e2}")
    finally:
        if youtube_connect_state.get("flow_id") == flow_id:
            youtube_connect_state["active"] = False
            youtube_connect_state["task"] = None


async def _poll_tiktok_connection(client: Client, chat_id: int, flow: dict, flow_id: int):
    try:
        result = await asyncio.to_thread(
            social_media.tiktok_qr_poll,
            flow["token"],
            flow["client_ticket"],
            3,
            600,
        )
        if tiktok_connect_state.get("flow_id") != flow_id:
            return
        mode = social_media.tiktok_mode()
        creator = result.get("creator", {}) or {}
        stored = social_media.load_tokens().get("tiktok", {}) or {}
        username = creator.get("creator_username") or stored.get("username", "")
        nickname = creator.get("creator_nickname") or stored.get("nickname", "")
        account = (
            f"@{username}"
            if username
            else (nickname or "(cuenta conectada)")
        )
        if mode == "direct":
            if creator:
                mode_text = "Publicación directa habilitada."
            else:
                mode_text = (
                    "La cuenta se conectó, pero no se pudo consultar la "
                    "información de publicación. Revisa /redes y los permisos "
                    "de la app en TikTok."
                )
        elif mode == "inbox":
            mode_text = (
                "La app tiene permiso video.upload: los videos se enviarán a la "
                "bandeja de entrada de TikTok y el cliente deberá confirmarlos en la app.\n"
                "Para publicación automática hay que aprobar video.publish y pasar "
                "la auditoría de TikTok."
            )
        else:
            mode_text = (
                "La cuenta se conectó, pero la app no tiene permiso para publicar "
                "videos. Revisa los scopes en el portal de TikTok."
            )
        await client.send_message(
            chat_id,
            "✅ <b>TikTok conectado</b>\n\n"
            f"🎵 Cuenta: {_safe_html(account)}\n"
            f"{_safe_html(mode_text)}",
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception as e:
        if tiktok_connect_state.get("flow_id") == flow_id:
            try:
                await client.send_message(
                    chat_id,
                    f"❌ <b>No se pudo conectar TikTok</b>\n\n{_safe_html(e)}\n\n"
                    "Genera un QR nuevo con /conectar_tiktok.",
                    parse_mode=enums.ParseMode.HTML,
                )
            except Exception as e2:
                logger.error(f"No se pudo notificar el error de TikTok: {e2}")
    finally:
        if tiktok_connect_state.get("flow_id") == flow_id:
            tiktok_connect_state["active"] = False
            tiktok_connect_state["task"] = None
        try:
            os.remove(flow.get("qr_path", ""))
        except Exception:
            pass


@app.on_message(filters.incoming & ~filters.me & filters.command("conectar_youtube") & filters.private)
async def cmd_connect_youtube(client: Client, message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        await message.reply_text("❌ Solo administradores pueden conectar YouTube.")
        return
    if youtube_connect_state.get("active"):
        old_task = youtube_connect_state.get("task")
        if old_task and not old_task.done():
            old_task.cancel()
        youtube_connect_state["active"] = False
        youtube_connect_state["task"] = None

    status_msg = await message.reply_text("⏳ Generando código de Google...")
    try:
        flow = await asyncio.to_thread(social_media.youtube_device_start)
    except Exception as e:
        await status_msg.edit_text(
            "❌ <b>No se pudo generar el código de YouTube</b>\n\n"
            f"{_safe_html(e)}\n\n"
            "Revisa que YOUTUBE_DEVICE_CLIENT_ID y YOUTUBE_DEVICE_CLIENT_SECRET "
            "sean de tipo «TVs and Limited Input devices» en Google Cloud.",
            parse_mode=enums.ParseMode.HTML,
        )
        return

    code = flow.get("user_code", "")
    url = flow.get("verification_url", "https://www.google.com/device")
    expires_min = max(1, int(flow.get("expires_in", 1800) / 60))
    expected_channel = os.getenv("YOUTUBE_CHANNEL_HANDLE", "").strip()
    expected_email = os.getenv("YOUTUBE_ACCOUNT_EMAIL", "").strip()
    account_lines = ""
    if expected_channel:
        account_lines += f"📺 Canal: {_safe_html(expected_channel)}\n"
    if expected_email:
        account_lines += f"📧 Cuenta: {_safe_html(expected_email)}\n"
    channel_hint = (
        f"   ⚠️ Elige {_safe_html(expected_channel)}, no el canal personal.\n"
        if expected_channel
        else ""
    )
    text = (
        "📺 <b>CONECTAR YOUTUBE</b>\n\n"
        + (account_lines + "\n" if account_lines else "")
        + "Reenvía este mensaje al cliente o pídele que haga esto:\n\n"
        f"1️⃣ Abre: {_safe_html(url)}\n"
        f"2️⃣ Escribe este código: <code>{_safe_html(code)}</code>\n"
        "3️⃣ Inicia sesión con la cuenta dueña del canal y acepta el permiso.\n"
        "   Si el canal es una cuenta Brand, selecciónalo cuando Google lo pida.\n"
        + channel_hint
        + "   Si Google muestra un aviso de app no verificada, entra en "
        "«Configuración avanzada» y continúa.\n\n"
        f"⏳ El código vence en {expires_min} minutos.\n"
        "Cuando el cliente termine, te aviso por aquí."
    )
    await status_msg.edit_text(
        text, parse_mode=enums.ParseMode.HTML, disable_web_page_preview=True
    )
    flow_id = int(youtube_connect_state.get("flow_id", 0)) + 1
    youtube_connect_state.update(
        {
            "active": True,
            "started_at": time.time(),
            "admin_id": message.from_user.id,
            "flow_id": flow_id,
        }
    )
    task = asyncio.create_task(
        _poll_youtube_connection(client, message.chat.id, flow, flow_id)
    )
    youtube_connect_state["task"] = task


@app.on_message(filters.incoming & ~filters.me & filters.command("conectar_tiktok") & filters.private)
async def cmd_connect_tiktok(client: Client, message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        await message.reply_text("❌ Solo administradores pueden conectar TikTok.")
        return
    if tiktok_connect_state.get("active"):
        old_task = tiktok_connect_state.get("task")
        if old_task and not old_task.done():
            old_task.cancel()
        tiktok_connect_state["active"] = False
        tiktok_connect_state["task"] = None

    status_msg = await message.reply_text("⏳ Generando código QR de TikTok...")
    try:
        flow = await asyncio.to_thread(social_media.tiktok_qr_start)
    except Exception as e:
        await status_msg.edit_text(
            "❌ <b>No se pudo generar el QR de TikTok</b>\n\n"
            f"{_safe_html(e)}\n\n"
            "Revisa que la app tenga Login Kit y los scopes video.publish o "
            "video.upload aprobados en el portal de TikTok.",
            parse_mode=enums.ParseMode.HTML,
        )
        return

    scope = flow.get("scope", "")
    if "video.publish" in scope:
        mode_text = "Publicación directa habilitada."
    elif "video.upload" in scope:
        mode_text = (
            "Bandeja de entrada: el cliente deberá confirmar la publicación en TikTok."
        )
    else:
        mode_text = (
            "La app solo tiene permiso de perfil; no podrá publicar videos."
        )

    expected_tiktok = os.getenv("TIKTOK_USERNAME", "").strip()
    account_line = (
        f"🎵 Cuenta: {_safe_html(expected_tiktok)}\n\n"
        if expected_tiktok
        else ""
    )
    caption = (
        "🎵 <b>CONECTAR TIKTOK</b>\n\n"
        + account_line
        + "Reenvía este código QR al cliente o pídele que haga esto:\n\n"
        "1️⃣ Abre TikTok en el teléfono.\n"
        "2️⃣ Toca el ícono de escanear QR.\n"
        "3️⃣ Escanea este código y acepta el permiso.\n\n"
        f"ℹ️ {mode_text}\n"
        "Cuando el cliente termine, te aviso por aquí."
    )
    try:
        await status_msg.delete()
    except Exception:
        pass
    # Se envía como documento para que Telegram no comprima el QR.
    await message.reply_document(
        flow["qr_path"], caption=caption, parse_mode=enums.ParseMode.HTML
    )
    flow_id = int(tiktok_connect_state.get("flow_id", 0)) + 1
    tiktok_connect_state.update(
        {
            "active": True,
            "started_at": time.time(),
            "admin_id": message.from_user.id,
            "flow_id": flow_id,
        }
    )
    task = asyncio.create_task(
        _poll_tiktok_connection(client, message.chat.id, flow, flow_id)
    )
    tiktok_connect_state["task"] = task


@app.on_message(filters.incoming & ~filters.me & filters.command("redes") & filters.private)
async def cmd_social_status(client: Client, message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        await message.reply_text("❌ Solo administradores pueden ver el estado de las redes.")
        return
    status_msg = await message.reply_text(
        "⏳ Consultando estado de YouTube y TikTok..."
    )
    try:
        status = await asyncio.to_thread(social_media.check_connections)
    except Exception as e:
        await status_msg.edit_text(
            f"❌ No se pudo consultar el estado: {_safe_html(e)}",
            parse_mode=enums.ParseMode.HTML,
        )
        return

    yt = status.get("youtube", {})
    tt = status.get("tiktok", {})
    expected_yt = os.getenv("YOUTUBE_CHANNEL_HANDLE", "").strip()
    expected_tt = os.getenv("TIKTOK_USERNAME", "").strip()
    lines = ["📡 <b>ESTADO DE REDES SOCIALES</b>", ""]

    if yt.get("connected"):
        icon = "✅" if yt.get("valid") else "⚠️"
        channel = yt.get("channel_title") or "(canal sin nombre)"
        lines.append(f"📺 <b>YouTube:</b> {icon} {_safe_html(channel)}")
        if yt.get("error"):
            lines.append(f"   ⚠️ {_safe_html(str(yt['error'])[:300])}")
    else:
        lines.append("📺 <b>YouTube:</b> ❌ No conectado")
        if expected_yt:
            lines.append(f"   Esperado: {_safe_html(expected_yt)}")
        lines.append("   Usa /conectar_youtube")

    lines.append("")

    if tt.get("connected"):
        icon = "✅" if tt.get("valid") else "⚠️"
        username = tt.get("username") or ""
        nickname = tt.get("nickname") or ""
        account = f"@{username}" if username else (nickname or "(cuenta conectada)")
        mode = tt.get("mode", "none")
        mode_label = {
            "direct": "Publicación directa",
            "inbox": "Bandeja de entrada (el cliente confirma en TikTok)",
            "none": "Sin permiso de publicación",
        }.get(mode, mode)
        lines.append(f"🎵 <b>TikTok:</b> {icon} {_safe_html(account)}")
        lines.append(f"   Modo: {_safe_html(mode_label)}")
        if tt.get("error"):
            lines.append(f"   ⚠️ {_safe_html(str(tt['error'])[:300])}")
    else:
        lines.append("🎵 <b>TikTok:</b> ❌ No conectado")
        if expected_tt:
            lines.append(f"   Esperado: {_safe_html(expected_tt)}")
        lines.append("   Usa /conectar_tiktok")

    lines.append("")
    lines.append(
        "Puedes reconectar cuando quieras con /conectar_youtube o /conectar_tiktok."
    )
    await status_msg.edit_text("\n".join(lines), parse_mode=enums.ParseMode.HTML)


# ==========================================
# CALLBACKS DE PUBLICACIÓN EN YOUTUBE / TIKTOK
# ==========================================

@app.on_callback_query(filters.regex(r"^yt_pub_"))
async def handle_youtube_publish_callback(client: Client, callback_query: CallbackQuery):
    if not is_admin(callback_query.from_user.id):
        await callback_query.answer(
            "❌ Solo administradores pueden publicar en YouTube.", show_alert=True
        )
        return
    if not social_media.local_status().get("youtube", {}).get("connected"):
        await callback_query.answer(
            "❌ YouTube no está conectado. Usa /conectar_youtube.",
            show_alert=True,
        )
        return
    file_id = callback_query.data.partition("yt_pub_")[2]
    await callback_query.answer()
    await send_youtube_panel(
        client,
        callback_query.message,
        file_id,
        callback_query.from_user.id,
        video_message=callback_query.message,
    )


@app.on_callback_query(filters.regex(r"^tt_pub_"))
async def handle_tiktok_publish_callback(client: Client, callback_query: CallbackQuery):
    if not is_admin(callback_query.from_user.id):
        await callback_query.answer(
            "❌ Solo administradores pueden publicar en TikTok.", show_alert=True
        )
        return
    if social_media.tiktok_mode() == "none":
        await callback_query.answer(
            "❌ TikTok no está conectado o no tiene permisos. Usa /conectar_tiktok.",
            show_alert=True,
        )
        return
    file_id = callback_query.data.partition("tt_pub_")[2]
    await callback_query.answer()
    await send_tiktok_panel(
        client,
        callback_query.message,
        file_id,
        callback_query.from_user.id,
        video_message=callback_query.message,
    )


@app.on_callback_query(filters.regex(r"^yt_go_"))
async def handle_youtube_go_callback(client: Client, callback_query: CallbackQuery):
    admin_id = callback_query.from_user.id
    if not is_admin(admin_id):
        await callback_query.answer("❌ No autorizado.", show_alert=True)
        return
    file_id = callback_query.data.partition("yt_go_")[2]
    state = admin_states.pop(admin_id, {})
    video_msg = state.get("video_message") or callback_query.message.reply_to_message or callback_query.message
    await callback_query.answer("🚀 Subiendo a YouTube...")
    await execute_youtube_publish(
        client, video_msg, file_id, status_message=callback_query.message
    )


@app.on_callback_query(filters.regex(r"^tt_go_"))
async def handle_tiktok_go_callback(client: Client, callback_query: CallbackQuery):
    admin_id = callback_query.from_user.id
    if not is_admin(admin_id):
        await callback_query.answer("❌ No autorizado.", show_alert=True)
        return
    file_id = callback_query.data.partition("tt_go_")[2]
    state = admin_states.pop(admin_id, {})
    video_msg = state.get("video_message") or callback_query.message.reply_to_message or callback_query.message
    await callback_query.answer("🚀 Enviando a TikTok...")
    await execute_tiktok_publish(
        client, video_msg, file_id, status_message=callback_query.message
    )


@app.on_callback_query(filters.regex(r"^yt_edit_"))
async def handle_youtube_edit_callback(client: Client, callback_query: CallbackQuery):
    admin_id = callback_query.from_user.id
    if not is_admin(admin_id):
        await callback_query.answer("❌ No autorizado.", show_alert=True)
        return
    file_id = callback_query.data.partition("yt_edit_")[2]
    state = admin_states.get(admin_id, {})
    video_msg = state.get("video_message") or callback_query.message.reply_to_message or callback_query.message
    admin_states[admin_id] = {
        "action": "awaiting_youtube_caption",
        "file_id": file_id,
        "video_message": video_msg,
        "panel_message": callback_query.message,
    }
    await callback_query.message.edit_text(
        "✏️ <b>Escribe el nuevo texto para YouTube.</b>\n\n"
        "La primera línea será el título (máximo 100 caracteres) y el resto "
        "será la descripción.\n\n"
        "Envíalo en el chat y te mostraré la vista previa.",
        parse_mode=enums.ParseMode.HTML,
    )
    await callback_query.answer()


@app.on_callback_query(filters.regex(r"^tt_edit_"))
async def handle_tiktok_edit_callback(client: Client, callback_query: CallbackQuery):
    admin_id = callback_query.from_user.id
    if not is_admin(admin_id):
        await callback_query.answer("❌ No autorizado.", show_alert=True)
        return
    file_id = callback_query.data.partition("tt_edit_")[2]
    state = admin_states.get(admin_id, {})
    video_msg = state.get("video_message") or callback_query.message.reply_to_message or callback_query.message
    admin_states[admin_id] = {
        "action": "awaiting_tiktok_caption",
        "file_id": file_id,
        "video_message": video_msg,
        "panel_message": callback_query.message,
    }
    await callback_query.message.edit_text(
        "✏️ <b>Escribe el nuevo texto para TikTok.</b>\n\n"
        "Puedes incluir hashtags (#) y menciones (@). Máximo 2200 caracteres.\n\n"
        "Envíalo en el chat y te mostraré la vista previa.",
        parse_mode=enums.ParseMode.HTML,
    )
    await callback_query.answer()


@app.on_callback_query(filters.regex(r"^yt_priv_"))
async def handle_youtube_privacy_callback(client: Client, callback_query: CallbackQuery):
    admin_id = callback_query.from_user.id
    if not is_admin(admin_id):
        await callback_query.answer("❌ No autorizado.", show_alert=True)
        return
    rest = callback_query.data.partition("yt_priv_")[2]
    file_id, _, privacy = rest.partition("_")
    if privacy not in ("public", "unlisted", "private"):
        await callback_query.answer("Privacidad inválida.", show_alert=True)
        return
    deletions = load_deletions()
    if file_id not in deletions:
        await callback_query.answer("El video ya no está en el registro.", show_alert=True)
        return
    deletions[file_id]["youtube_privacy"] = privacy
    save_deletions(deletions)
    state = admin_states.get(admin_id, {})
    video_msg = state.get("video_message")
    await send_youtube_panel(
        client,
        callback_query.message,
        file_id,
        admin_id,
        video_message=video_msg,
        edit=True,
    )
    await callback_query.answer(f"Privacidad: {_youtube_privacy_label(privacy)}")


@app.on_callback_query(filters.regex(r"^tt_priv_"))
async def handle_tiktok_privacy_callback(client: Client, callback_query: CallbackQuery):
    admin_id = callback_query.from_user.id
    if not is_admin(admin_id):
        await callback_query.answer("❌ No autorizado.", show_alert=True)
        return
    rest = callback_query.data.partition("tt_priv_")[2]
    file_id, _, privacy = rest.partition("_")
    if privacy not in social_media.TIKTOK_PRIVACY_LABELS:
        await callback_query.answer("Privacidad inválida.", show_alert=True)
        return
    deletions = load_deletions()
    if file_id not in deletions:
        await callback_query.answer("El video ya no está en el registro.", show_alert=True)
        return
    deletions[file_id]["tiktok_privacy"] = privacy
    save_deletions(deletions)
    state = admin_states.get(admin_id, {})
    video_msg = state.get("video_message")
    await send_tiktok_panel(
        client,
        callback_query.message,
        file_id,
        admin_id,
        video_message=video_msg,
        edit=True,
    )
    await callback_query.answer(f"Privacidad: {_tiktok_privacy_label(privacy)}")


@app.on_callback_query(filters.regex(r"^yt_cancel_"))
async def handle_youtube_cancel_callback(client: Client, callback_query: CallbackQuery):
    if not is_admin(callback_query.from_user.id):
        await callback_query.answer("❌ No autorizado.", show_alert=True)
        return
    admin_states.pop(callback_query.from_user.id, None)
    try:
        await callback_query.message.delete()
    except Exception:
        pass
    await callback_query.answer("Operación cancelada.")


@app.on_callback_query(filters.regex(r"^tt_cancel_"))
async def handle_tiktok_cancel_callback(client: Client, callback_query: CallbackQuery):
    if not is_admin(callback_query.from_user.id):
        await callback_query.answer("❌ No autorizado.", show_alert=True)
        return
    admin_states.pop(callback_query.from_user.id, None)
    try:
        await callback_query.message.delete()
    except Exception:
        pass
    await callback_query.answer("Operación cancelada.")


@app.on_callback_query(filters.regex(r"^noop_yt_"))
async def handle_noop_youtube_callback(client: Client, callback_query: CallbackQuery):
    file_id = callback_query.data.partition("noop_yt_")[2]
    info = load_deletions().get(file_id, {})
    url = info.get("youtube_url")
    if url:
        await callback_query.answer(f"Ya publicado en YouTube: {url}", show_alert=True)
    elif info.get("youtube_dry_run"):
        await callback_query.answer("Este video ya fue probado para YouTube.", show_alert=True)
    else:
        await callback_query.answer("Este video ya fue procesado para YouTube.", show_alert=True)


@app.on_callback_query(filters.regex(r"^noop_tt_"))
async def handle_noop_tiktok_callback(client: Client, callback_query: CallbackQuery):
    file_id = callback_query.data.partition("noop_tt_")[2]
    info = load_deletions().get(file_id, {})
    if info.get("tiktok_pending"):
        await callback_query.answer(
            "TikTok todavía está procesando este video.", show_alert=True
        )
    elif info.get("tiktok_dry_run"):
        await callback_query.answer("Este video ya fue probado en TikTok.", show_alert=True)
    elif info.get("tiktok_mode") == "inbox":
        await callback_query.answer(
            "El video ya fue enviado a la bandeja de entrada de TikTok.",
            show_alert=True,
        )
    elif info.get("tiktok_url"):
        await callback_query.answer(
            f"Ya publicado en TikTok: {info['tiktok_url']}", show_alert=True
        )
    else:
        await callback_query.answer("Este video ya fue procesado para TikTok.", show_alert=True)


@app.on_message(filters.incoming & ~filters.me & filters.text & filters.private)
async def handle_admin_text(client: Client, message: Message):
    if not message.from_user or message.from_user.is_bot:
        return
    if message.text and message.text.startswith("/"):
        return
        
    admin_id = message.from_user.id
    if admin_id in admin_states and admin_states[admin_id].get("action") == "awaiting_instagram_caption":
        state = admin_states.pop(admin_id)
        file_id = state["file_id"]
        video_msg = state.get("video_message", message)
        new_caption = message.text.strip()

        # Actualizar caption en deletions para persistencia
        deletions = load_deletions()
        if file_id in deletions:
            deletions[file_id]["caption"] = new_caption
            save_deletions(deletions)

        await message.reply_text(f"📝 **Nuevo caption guardado:**\n_{new_caption}_\n\n🚀 Iniciando subida a Redes (IG + FB)...")
        await execute_meta_publish(client, video_msg, file_id, new_caption)
        return

    if admin_id in admin_states and admin_states[admin_id].get("action") == "awaiting_youtube_caption":
        state = admin_states.pop(admin_id)
        file_id = state["file_id"]
        video_msg = state.get("video_message")
        new_caption = (message.text or "").strip()
        deletions = load_deletions()
        if file_id in deletions:
            deletions[file_id]["youtube_caption"] = new_caption
            save_deletions(deletions)
        panel_message = state.get("panel_message")
        if panel_message:
            try:
                await panel_message.delete()
            except Exception:
                pass
        await send_youtube_panel(
            client, message, file_id, admin_id, video_message=video_msg
        )
        return

    if admin_id in admin_states and admin_states[admin_id].get("action") == "awaiting_tiktok_caption":
        state = admin_states.pop(admin_id)
        file_id = state["file_id"]
        video_msg = state.get("video_message")
        new_caption = (message.text or "").strip()[:2200]
        deletions = load_deletions()
        if file_id in deletions:
            deletions[file_id]["tiktok_caption"] = new_caption
            save_deletions(deletions)
        panel_message = state.get("panel_message")
        if panel_message:
            try:
                await panel_message.delete()
            except Exception:
                pass
        await send_tiktok_panel(
            client, message, file_id, admin_id, video_message=video_msg
        )
        return

    if admin_id in admin_states and admin_states[admin_id].get("action") == "awaiting_custom_date":
        file_id = admin_states[admin_id]["file_id"]
        text = message.text.strip()
        
        deletions = load_deletions()
        if file_id not in deletions:
            await message.reply_text("❌ El archivo ya no está en el registro.")
            del admin_states[admin_id]
            return
            
        delete_timestamp = 0
        try:
            naive_dt = datetime.datetime.strptime(text, "%Y-%m-%d")
            local_dt = naive_dt.replace(tzinfo=CARACAS_TZ)
            delete_timestamp = local_dt.timestamp()
        except ValueError:
            try:
                days = float(text)
                delete_timestamp = time.time() + (days * 86400)
            except ValueError:
                await message.reply_text("❌ Formato inválido. Por favor, escribe un número de días (ej: 14) o una fecha YYYY-MM-DD.")
                return
                
        if delete_timestamp < time.time():
            await message.reply_text("❌ La fecha especificada ya pasó. Intenta con una fecha futura.")
            return
            
        deletions[file_id]["delete_after"] = delete_timestamp
        save_deletions(deletions)
        del admin_states[admin_id]
        
        date_str = format_caracas_time(delete_timestamp)
        await message.reply_text(f"✅ **Autodestrucción programada:**\nEl archivo se eliminará el {date_str} (Hora Vzla).")
        return

    # Detección de enlaces de YouTube
    yt_regex = r'(https?://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?v=|shorts/|live/)|youtu\.be/)[\w-]+[^\s]*)'
    yt_match = re.search(yt_regex, message.text or "")
    if yt_match:
        if not is_admin(admin_id):
            await message.reply_text("❌ Solo los administradores pueden enviar enlaces de YouTube para descargar.")
            return

        yt_url = yt_match.group(1)
        status_msg = await message.reply_text(
            f"🎬 **Enlace de YouTube detectado:**\n`{yt_url}`\n\n"
            f"⏳ Conectando con YouTube y descargando en **hasta 1080p** "
            f"(con audio fusionado).\n"
            f"Esto puede tardar unos minutos..."
        )

        # 1. Descargar en segundo plano
        success, local_path, video_title, err = await asyncio.to_thread(download_youtube_video, yt_url)
        if not success or not os.path.exists(local_path):
            err_text = err or "Fallo desconocido"
            if len(err_text) > 1500:
                err_text = err_text[:1500] + "..."
            await status_msg.edit_text(
                f"❌ **Error al descargar video de YouTube:**\n`{err_text}`"
            )
            return

        file_size_mb = os.path.getsize(local_path) / (1024 * 1024)
        target_folder = "YouTube"
        folder_display = f"/{target_folder}"

        # Sanitizar nombre final para FTP y VDO Panel
        base_name = sanitize_filename(f"{video_title}.mp4")
        if not base_name.endswith(".mp4"):
            base_name += ".mp4"

        try:
            await status_msg.edit_text(
                f"📥 **Descarga completada:** `{base_name}` ({file_size_mb:.2f} MB)\n\n"
                f"⏳ Subiendo video al servidor FTP en la carpeta `{folder_display}`..."
            )

            # 2. Subir 1 sola copia al FTP en la carpeta YouTube
            await asyncio.to_thread(upload_to_ftp, local_path, base_name, target_folder, 1)

            # 3. Registrar en deletions.json para autodestrucción y acciones interactivas (sin Dropbox, 1 copia)
            file_id = str(uuid.uuid4())[:8]
            deletions = load_deletions()
            deletions[file_id] = {
                "ftp_path": target_folder,
                "file_name": base_name,
                "delete_after": None,
                "caption": video_title,
                "telegram_file_id": None,
                "dropbox_audio_path": None,
                "copies": 1
            }
            save_deletions(deletions)

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("📺 YouTube", callback_data=f"yt_pub_{file_id}"),
                    InlineKeyboardButton("🎵 TikTok", callback_data=f"tt_pub_{file_id}"),
                ],
                [InlineKeyboardButton("📲 Publicar en Redes (IG + FB)", callback_data=f"ig_pub_{file_id}")],
                [InlineKeyboardButton("🎵 Agregar a Lista Musical", callback_data=f"addpl_{file_id}")],
                [InlineKeyboardButton("2 Minutos (Test)", callback_data=f"exp_2m_{file_id}")],
                [InlineKeyboardButton("24 Horas", callback_data=f"exp_24h_{file_id}"),
                 InlineKeyboardButton("3 Días", callback_data=f"exp_3d_{file_id}")],
                [InlineKeyboardButton("1 Semana", callback_data=f"exp_1w_{file_id}"),
                 InlineKeyboardButton("Personalizada", callback_data=f"exp_cust_{file_id}")]
            ])

            user_first = message.from_user.first_name or "Admin"
            confirm_text = (
                f"✅ **VIDEO DE YOUTUBE DESCARGADO Y SUBIDO**\n\n"
                f"🎬 **Título:** {video_title}\n"
                f"📁 **Archivo:** `{base_name}` ({file_size_mb:.2f} MB)\n"
                f"📂 **Destino FTP:** `{folder_display}`\n"
                f"👤 **Descargado por:** {user_first} (ID: `{admin_id}`)\n\n"
                f"¿Deseas publicarlo en YouTube/TikTok, agregarlo a la lista o "
                f"programar su eliminación?"
            )

            await status_msg.edit_text(confirm_text, reply_markup=keyboard)

        except Exception as e_up:
            logger.error(f"[YouTube] Error subiendo video a FTP: {e_up}")
            await status_msg.edit_text(f"❌ Error al procesar o subir el video a FTP:\n`{e_up}`")

        finally:
            if os.path.exists(local_path):
                try:
                    os.remove(local_path)
                except Exception:
                    pass
        return


# ==========================================
# SERVIDOR WEB FLASK (PANEL ADMINISTRATIVO)
# ==========================================

flask_app = Flask(__name__)
flask_app.secret_key = "super_secreto_para_flash_messages_123"

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

@flask_app.route('/ping')
def ping():
    # Ruta pública para el servicio de monitoreo (UptimeRobot, cron-job, etc)
    return "OK", 200

@flask_app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if username == ADMIN_USER and password == ADMIN_PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('index'))
        else:
            flash('Usuario o contraseña incorrectos', 'error')
    return render_template('login.html')

@flask_app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

@flask_app.route('/')
def home():
    # Ruta pública raíz para que el servicio de ping no reciba 301/302/401
    return "Bot is running!", 200

@flask_app.route('/admin')
@requires_auth
def index():
    users = load_users()
    return render_template('index.html', users=users)

@flask_app.route('/add', methods=['POST'])
@requires_auth
def add_user():
    user_id = request.form.get('user_id')
    user_name = request.form.get('user_name')
    ftp_path = request.form.get('ftp_path')
    if user_id and ftp_path and user_name:
        user_id = str(user_id).strip()
        user_name = str(user_name).strip()
        ftp_path = str(ftp_path).strip()
        if not ftp_path.startswith("/"):
            ftp_path = f"/{ftp_path}"
            
        users = load_users()
        
        if user_id not in users:
            users[user_id] = {"ftp_path": ftp_path, "name": user_name}
            save_users(users)
            flash(f"Usuario {user_id} ({user_name}) autorizado para usar la carpeta {ftp_path}.", "success")
        else:
            flash("El usuario ya estaba autorizado. Revócalo primero si deseas cambiar sus datos.", "error")
    else:
        flash("Datos inválidos. Completa todos los campos.", "error")
    return redirect(url_for('index'))

@flask_app.route('/delete', methods=['POST'])
@requires_auth
def delete_user():
    user_id = request.form.get('user_id')
    if user_id:
        user_id = str(user_id).strip()
        users = load_users()
        if user_id in users:
            del users[user_id]
            save_users(users)
            flash(f"Acceso revocado para el usuario {user_id}.", "success")
    return redirect(url_for('index'))

@flask_app.route('/import', methods=['POST'])
@requires_auth
def import_users():
    imported_count = 0
    raw_data = None
    
    # 1. Verificar si subieron un archivo
    if 'file' in request.files:
        file = request.files['file']
        if file and file.filename.endswith('.json'):
            try:
                raw_data = json.load(file)
            except Exception as e:
                flash(f"Error al leer archivo JSON: {e}", "error")
                return redirect(url_for('index'))
                
    # 2. Verificar si pegaron JSON en texto
    if not raw_data and request.form.get('json_data'):
        try:
            raw_data = json.loads(request.form.get('json_data'))
        except Exception as e:
            flash(f"Error al procesar texto JSON: {e}", "error")
            return redirect(url_for('index'))
            
    if isinstance(raw_data, dict):
        users = load_users()
        for u_id, info in raw_data.items():
            path_val = ""
            name_val = "Usuario"
            if isinstance(info, dict):
                path_val = str(info.get("ftp_path", "")).strip()
                name_val = info.get("name", "Usuario")
            elif isinstance(info, str):
                path_val = str(info).strip()
                
            if path_val and not path_val.startswith("/"):
                path_val = f"/{path_val}"
                
            users[str(u_id)] = {
                "ftp_path": path_val,
                "name": name_val
            }
            imported_count += 1
            
        save_users(users)
        flash(f"Se importaron {imported_count} usuarios exitosamente.", "success")
    else:
        flash("Formato JSON no válido o no se envió ningún dato.", "error")
        
    return redirect(url_for('index'))

def keep_alive_ping():
    """Ping automático interno para burlar la inactividad de Render"""
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if not url:
        return
    while True:
        try:
            time.sleep(600)
            urllib.request.urlopen(url)
            logger.info(f"Auto-ping interno exitoso a {url}")
        except Exception as e:
            logger.warning(f"Error en auto-ping interno: {e}")

def ftp_delete_file(target_folder, file_name):
    try:
        ftp = FTP(FTP_HOST)
        ftp.login(user=FTP_USER, passwd=FTP_PASS)
        if target_folder:
            target_folder = target_folder.strip("/")
            try:
                ftp.cwd(target_folder)
            except Exception as e:
                logger.error(f"No se pudo acceder a la carpeta /{target_folder}: {e}")
                ftp.quit()
                return False
                
        name, ext = os.path.splitext(file_name)
        s_name, s_ext = os.path.splitext(sanitize_filename(file_name))
        base_names = {
            name,
            name.replace("_", "-"),
            name.replace("-", "_"),
            name.replace(" ", "-"),
            name.replace(" ", "_"),
            s_name,
            s_name.replace("_", "-"),
            s_name.replace("-", "_")
        }
        
        deleted_count = 0
        existing_files = set()
        try:
            existing_files = set(ftp.nlst())
        except Exception:
            pass

        for b_name in base_names:
            # Archivo base
            target_file = f"{b_name}{ext}"
            if not existing_files or target_file in existing_files:
                try:
                    ftp.delete(target_file)
                    deleted_count += 1
                    logger.info(f"Borrado del FTP: {target_file}")
                except Exception as e:
                    logger.warning(f"No se pudo borrar {target_file}: {e}")
                    
            # 9 copias
            for i in range(1, 10):
                target_copy = f"{b_name}-{i}{ext}"
                if not existing_files or target_copy in existing_files:
                    try:
                        ftp.delete(target_copy)
                        deleted_count += 1
                        logger.info(f"Borrado del FTP: {target_copy}")
                    except Exception as e:
                        logger.warning(f"No se pudo borrar {target_copy}: {e}")
                
        ftp.quit()
        return deleted_count > 0
    except Exception as e:
        logger.error(f"Error borrando archivos FTP {file_name}: {e}")
        return False

def auto_delete_worker():
    """Hilo que revisa cada minuto si algún archivo caducó"""
    while True:
        try:
            time.sleep(60)
            deletions = load_deletions()
            now = time.time()
            modified = False
            
            for file_id, info in list(deletions.items()):
                delete_after = info.get("delete_after")
                if delete_after and now >= delete_after:
                    ftp_path = info.get("ftp_path", "")
                    file_name = info.get("file_name", "")
                    
                    logger.info(f"Eliminando archivo caducado: {file_name} de {ftp_path}")
                    success = ftp_delete_file(ftp_path, file_name)
                    
                    # Eliminar también el audio correspondiente en Dropbox (si no es YouTube)
                    dbx_deleted = False
                    if str(ftp_path).strip("/").lower() != "youtube":
                        dbx_audio_path = info.get("dropbox_audio_path")
                        if not dbx_audio_path:
                            # Fallback: deducir ruta si no estaba explícita
                            dbx_folder = os.getenv("DROPBOX_FOLDER", "/CNX-INFORMATIVA").strip("/")
                            base_f, _ = os.path.splitext(file_name)
                            dbx_audio_path = f"/{dbx_folder}/{base_f}.mp3"
                        
                        dbx_deleted = dropbox_delete_file(dbx_audio_path)
                    
                    if success or dbx_deleted:
                        for admin_chat_id in ADMIN_CHAT_IDS:
                            try:
                                msg_text = f"🗑️ **Autodestrucción Ejecutada**\nEl archivo `{file_name}` ha sido eliminado del FTP."
                                if dbx_deleted:
                                    msg_text += f"\n🎵 Audio `{os.path.basename(dbx_audio_path)}` eliminado de Dropbox."
                                asyncio.run_coroutine_threadsafe(
                                    app.send_message(
                                        chat_id=admin_chat_id,
                                        text=msg_text
                                    ),
                                    loop
                                )
                            except Exception as e:
                                logger.error(f"Error enviando notif de borrado a admin {admin_chat_id}: {e}")

                                
                    del deletions[file_id]
                    modified = True
                    
            if modified:
                save_deletions(deletions)
        except Exception as e:
            logger.error(f"Error en auto_delete_worker: {e}")

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    print(f"Panel Web iniciado en el puerto {port}")
    # Importante: debug=False y use_reloader=False para no interferir con Pyrogram
    flask_app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    # Iniciar el panel web en segundo plano
    threading.Thread(target=run_flask, daemon=True).start()
    
    # Iniciar auto-ping interno
    threading.Thread(target=keep_alive_ping, daemon=True).start()
    threading.Thread(target=auto_delete_worker, daemon=True).start()
    
    print("Iniciando bot con Pyrogram... (Soporta descargas de hasta 2GB)")
    app.run()
