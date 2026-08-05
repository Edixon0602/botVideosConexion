import os
import json
import logging
from ftplib import FTP
import asyncio
import threading

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
from flask import Flask, render_template, request, redirect, url_for, flash, Response

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
        return []
    try:
        with open(USERS_FILE, 'r') as f:
            return json.load(f)
    except:
        return []

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

def upload_to_ftp(local_file_path: str, remote_filename: str):
    """Sube un archivo al servidor FTP"""
    logger.info(f"Conectando a FTP: {FTP_HOST}...")
    ftp = FTP(FTP_HOST)
    ftp.login(user=FTP_USER, passwd=FTP_PASS)
    
    # Cambiar al directorio destino
    try:
        ftp.cwd(FTP_UPLOAD_PATH)
    except Exception as e:
        logger.warning(f"No se pudo cambiar al directorio {FTP_UPLOAD_PATH}, intentando crear o usar raíz: {e}")
    
    logger.info(f"Subiendo archivo {remote_filename}...")
    with open(local_file_path, 'rb') as file:
        ftp.storbinary(f'STOR {remote_filename}', file)
        
    ftp.quit()
    logger.info("Subida FTP completada.")

@app.on_message(filters.command("start"))
async def start(client: Client, message: Message):
    await message.reply_text("¡Hola! Soy tu bot de subida de videos.\nEnvíame un video y lo subiré automáticamente al dashboard.")

@app.on_message(filters.video | filters.document)
async def handle_video(client: Client, message: Message):
    user_id = str(message.from_user.id)
    username = message.from_user.username
    
    # Verificación de seguridad
    allowed_users = load_users()
    
    is_allowed = False
    if user_id in allowed_users:
        is_allowed = True
    if username:
        if username in allowed_users or f"@{username}" in allowed_users:
            is_allowed = True
            
    if not is_allowed:
        logger.warning(f"Acceso denegado al usuario: {user_id} (@{username})")
        await message.reply_text(f"❌ No estás autorizado para subir videos a este bot. Pide acceso al administrador indicando tu ID numérico: `{user_id}` o tu usuario: `@{username or 'Sin usuario'}`")
        return

    # Extraer información del archivo (ya sea video o documento como MP4)
    file = message.video or message.document
    if not file:
        return
        
    file_size_mb = file.file_size / (1024 * 1024)
    file_name = file.file_name if getattr(file, 'file_name', None) else f"video_{message.id}.mp4"
    
    # Mensaje de estado
    status_msg = await message.reply_text(f"⏳ Descargando `{file_name}` ({file_size_mb:.2f} MB)... esto puede tardar dependiendo del tamaño.")
    
    try:
        # 1. Descargar archivo
        logger.info(f"Iniciando descarga de {file_name} ({file_size_mb:.2f} MB)")
        local_path = await message.download()
        logger.info(f"Descarga completada: {local_path}")
        
        await status_msg.edit_text("⏳ Subiendo video al servidor FTP...")
        
        # 2. Subir por FTP
        upload_to_ftp(local_path, file_name)
        
        # 3. Eliminar archivo temporal local
        if os.path.exists(local_path):
            os.remove(local_path)
        
        await status_msg.edit_text(f"✅ ¡Video subido exitosamente!\nArchivo: `{file_name}`")
        
        # Notificar al administrador si está configurado
        if NOTIFICATION_CHAT_ID and NOTIFICATION_CHAT_ID != "tu_chat_id_aqui":
            user_name = message.from_user.first_name or "Usuario"
            try:
                await client.send_message(
                    chat_id=int(NOTIFICATION_CHAT_ID),
                    text=f"🔔 **NUEVO VIDEO SUBIDO**\n\n👤 **Usuario:** {user_name} (ID: `{user_id}`)\n📁 **Archivo:** `{file_name}`"
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

def check_auth(username, password):
    return username == ADMIN_USER and password == ADMIN_PASSWORD

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return Response(
                'Acceso denegado al Panel de Control.', 401,
                {'WWW-Authenticate': 'Basic realm="Login Requerido"'}
            )
        return f(*args, **kwargs)
    return decorated

@flask_app.route('/')
@requires_auth
def index():
    users = load_users()
    return render_template('index.html', users=users)

@flask_app.route('/add', methods=['POST'])
@requires_auth
def add_user():
    user_id = request.form.get('user_id')
    if user_id:
        user_id = str(user_id).strip()
        users = load_users()
        users_str = [str(u) for u in users]
        if user_id not in users_str:
            users.append(user_id)
            save_users(users)
            flash(f"Usuario {user_id} autorizado correctamente.", "success")
        else:
            flash("El usuario ya estaba autorizado.", "error")
    else:
        flash("Usuario inválido.", "error")
    return redirect(url_for('index'))

@flask_app.route('/delete', methods=['POST'])
@requires_auth
def delete_user():
    user_id = request.form.get('user_id')
    if user_id:
        user_id = str(user_id).strip()
        users = load_users()
        users_str = [str(u) for u in users]
        if user_id in users_str:
            # Encontrar y eliminar el valor original (sea int o str)
            for u in users:
                if str(u) == user_id:
                    users.remove(u)
                    break
            save_users(users)
            flash(f"Acceso revocado para el usuario {user_id}.", "success")
    return redirect(url_for('index'))

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    print(f"Panel Web iniciado en el puerto {port}")
    # Importante: debug=False y use_reloader=False para no interferir con Pyrogram
    flask_app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    # Iniciar el panel web en segundo plano
    threading.Thread(target=run_flask, daemon=True).start()
    
    print("Iniciando bot con Pyrogram... (Soporta descargas de hasta 2GB)")
    app.run()
