import os
import json
import logging
from ftplib import FTP
import asyncio
import threading
import urllib.request
import time

# Parche para Render y Python 3.10+ (corrige el error de 'no current event loop')
try:
    loop = asyncio.get_running_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.types import Message
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, flash, Response, session

# Configurar logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Cargar variables de entorno
load_dotenv()

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

# Inicializar cliente de Pyrogram
app = Client(
    "mi_bot_videos",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

def upload_to_ftp(local_file_path: str, remote_filename: str, target_folder: str):
    """Sube un archivo al servidor FTP en la carpeta específica"""
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
    
    logger.info(f"Subiendo archivo {remote_filename}...")
    with open(local_file_path, 'rb') as file:
        ftp.storbinary(f'STOR {remote_filename}', file)
        
    ftp.quit()
    logger.info("Subida FTP completada.")

@app.on_message(filters.command("start"))
async def start(client: Client, message: Message):
    await message.reply_text("¡Hola! Envíame un video y lo subiré automáticamente a tu carpeta en el dashboard.")

@app.on_message(filters.video | filters.document)
async def handle_video(client: Client, message: Message):
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
    file_name = file.file_name if getattr(file, 'file_name', None) else f"video_{message.id}.mp4"
    
    # Mensaje de estado
    status_msg = await message.reply_text(f"¡Hola {user_name}! ⏳ Descargando `{file_name}` ({file_size_mb:.2f} MB)...")
    
    try:
        # 1. Descargar archivo
        logger.info(f"Iniciando descarga de {file_name} ({file_size_mb:.2f} MB)")
        local_path = await message.download()
        logger.info(f"Descarga completada: {local_path}")
        
        await status_msg.edit_text(f"⏳ Subiendo video a la carpeta `/ {target_folder}` del FTP...")
        
        # 2. Subir por FTP
        upload_to_ftp(local_path, file_name, target_folder)
        
        # 3. Eliminar archivo temporal local
        if os.path.exists(local_path):
            os.remove(local_path)
        
        await status_msg.edit_text(f"✅ ¡Video subido exitosamente a la carpeta `/{target_folder}`!\nArchivo: `{file_name}`")
        
        # Notificar al administrador si está configurado
        if NOTIFICATION_CHAT_ID and NOTIFICATION_CHAT_ID != "tu_chat_id_aqui":
            user_name = message.from_user.first_name or "Usuario"
            try:
                await client.send_message(
                    chat_id=int(NOTIFICATION_CHAT_ID),
                    text=f"🔔 **NUEVO VIDEO SUBIDO**\n\n👤 **Usuario:** {user_name} (ID: `{user_id}`)\n📁 **Archivo:** `{file_name}`\n📂 **Destino:** `/{target_folder}`"
                )
            except Exception as e:
                logger.error(f"No se pudo enviar notificación: {e}")
        
    except Exception as e:
        logger.error(f"Error procesando video: {e}")
        await status_msg.edit_text(f"❌ Ocurrió un error al procesar el video:\n`{str(e)}`")


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
        users = load_users()
        
        if user_id not in users:
            users[user_id] = {"ftp_path": ftp_path, "name": user_name}
            save_users(users)
            flash(f"Usuario {user_id} ({user_name}) autorizado para usar la carpeta /{ftp_path}.", "success")
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
    
    print("Iniciando bot con Pyrogram... (Soporta descargas de hasta 2GB)")
    app.run()
