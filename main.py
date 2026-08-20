import os
import json
import logging
from ftplib import FTP
import asyncio
import threading
import urllib.request
import time
import aiohttp
from bs4 import BeautifulSoup

# Parche para Render y Python 3.10+ (corrige el error de 'no current event loop')
try:
    loop = asyncio.get_running_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

from dotenv import load_dotenv
from pyrogram import Client, filters
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

DELETIONS_FILE = 'deletions.json'
admin_states = {}

def load_deletions():
    if not os.path.exists(DELETIONS_FILE):
        return {}
    try:
        with open(DELETIONS_FILE, 'r') as f:
            return json.load(f)
    except:
        return {}

def save_deletions(data):
    with open(DELETIONS_FILE, 'w') as f:
        json.dump(data, f)

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
    
    name, ext = os.path.splitext(remote_filename)
    
    for i in range(10):
        if i == 0:
            current_filename = remote_filename
        else:
            current_filename = f"{name}-{i}{ext}"
            
        logger.info(f"Subiendo copia {i+1}/10: {current_filename}...")
        with open(local_file_path, 'rb') as file:
            ftp.storbinary(f'STOR {current_filename}', file)
        
    ftp.quit()
    logger.info("Subida de 10 copias al FTP completada.")

async def control_vdo_panel(action_url: str) -> bool:
    vdo_user = os.getenv("VDOPANEL_USER")
    vdo_pass = os.getenv("VDOPANEL_PASS")
    
    if not vdo_user or not vdo_pass or vdo_user == "tu_usuario_panel_aqui":
        logger.error("Credenciales de VDO Panel no configuradas.")
        return False
        
    login_url = "https://stream.conexion.com.ve/broadcaster/login"
    
    try:
        # Usamos una sesión para guardar las cookies automáticamente
        async with aiohttp.ClientSession() as session:
            # 1. Obtener la página de login para sacar el CSRF token y cookies iniciales
            async with session.get(login_url) as resp:
                html = await resp.text()
                
            soup = BeautifulSoup(html, 'html.parser')
            
            payload = {
                "name": vdo_user,
                "password": vdo_pass
            }
            
            # Extraer campos ocultos (como _token de Laravel)
            for hidden in soup.find_all("input", type="hidden"):
                name = hidden.get("name")
                value = hidden.get("value")
                if name:
                    payload[name] = value
                    
            # 2. Hacer POST al login
            async with session.post(login_url, data=payload) as resp:
                # Laravel suele redirigir (302) si el login fue exitoso. 
                # aiohttp sigue las redirecciones por defecto, por lo que debería darnos un 200 en el dashboard
                if resp.status not in [200, 302] or str(resp.url).endswith("/login"):
                    logger.error(f"Fallo al loguear en VDO Panel: {resp.status} - {resp.url}")
                    return False
                
            # 3. Ejecutar la acción solicitada (start o stop)
            async with session.get(action_url) as resp:
                if resp.status in [200, 302, 301]:
                    return True
                else:
                    logger.error(f"Fallo al ejecutar la acción en VDO Panel: HTTP {resp.status}")
                    return False
    except Exception as e:
        logger.error(f"Excepción en control_vdo_panel: {e}")
        return False

@app.on_message(filters.command("start"))
async def start(client: Client, message: Message):
    await message.reply_text("¡Hola! Envíame un video y lo subiré automáticamente a tu carpeta en el dashboard.")

@app.on_message(filters.command("iniciar"))
async def cmd_start_stream(client: Client, message: Message):
    user_id = str(message.from_user.id)
    allowed_users = load_users()
    
    if user_id not in allowed_users:
        await message.reply_text("❌ No estás autorizado para controlar el stream.")
        return
        
    msg = await message.reply_text("⏳ Intentando **INICIAR** el stream en VDO Panel...")
    success = await control_vdo_panel("https://stream.conexion.com.ve/broadcaster/start-webtv")
    
    if success:
        await msg.edit_text("✅ Stream **INICIADO** exitosamente.")
    else:
        await msg.edit_text("❌ Error al iniciar el stream. Revisa las credenciales de VDO Panel en las variables de entorno.")

@app.on_message(filters.command("detener"))
async def cmd_stop_stream(client: Client, message: Message):
    user_id = str(message.from_user.id)
    allowed_users = load_users()
    
    if user_id not in allowed_users:
        await message.reply_text("❌ No estás autorizado para controlar el stream.")
        return
        
    msg = await message.reply_text("⏳ Intentando **DETENER** el stream en VDO Panel...")
    success = await control_vdo_panel("https://stream.conexion.com.ve/broadcaster/stop-webtv")
    
    if success:
        await msg.edit_text("✅ Stream **DETENIDO** exitosamente.")
    else:
        await msg.edit_text("❌ Error al detener el stream. Revisa las credenciales de VDO Panel en las variables de entorno.")

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
        local_path = await message.download(file_name=file_name)
        file_name = os.path.basename(local_path)
        logger.info(f"Descarga completada: {local_path} (Nombre final: {file_name})")
        
        await status_msg.edit_text(f"⏳ Subiendo video a la carpeta `/{target_folder}` del FTP...")
        
        # 2. Subir por FTP
        upload_to_ftp(local_path, file_name, target_folder)
        
        # 3. Eliminar archivo temporal local
        if os.path.exists(local_path):
            os.remove(local_path)
        
        await status_msg.edit_text(f"✅ ¡Video subido exitosamente a la carpeta `/{target_folder}`!\nArchivo: `{file_name}`")
        
        # Notificar al administrador si está configurado
        if NOTIFICATION_CHAT_ID and NOTIFICATION_CHAT_ID != "tu_chat_id_aqui":
            user_name = message.from_user.first_name or "Usuario"
            file_id = str(uuid.uuid4())[:8]
            
            deletions = load_deletions()
            deletions[file_id] = {
                "ftp_path": target_folder,
                "file_name": file_name,
                "delete_after": None
            }
            save_deletions(deletions)
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("2 Minutos (Test)", callback_data=f"exp_2m_{file_id}")],
                [InlineKeyboardButton("24 Horas", callback_data=f"exp_24h_{file_id}"),
                 InlineKeyboardButton("3 Días", callback_data=f"exp_3d_{file_id}")],
                [InlineKeyboardButton("1 Semana", callback_data=f"exp_1w_{file_id}"),
                 InlineKeyboardButton("Personalizada", callback_data=f"exp_cust_{file_id}")]
            ])
            
            try:
                await client.send_message(
                    chat_id=int(NOTIFICATION_CHAT_ID),
                    text=f"🔔 **NUEVO VIDEO SUBIDO**\n\n👤 **Usuario:** {user_name} (ID: `{user_id}`)\n📁 **Archivo:** `{file_name}`\n📂 **Destino:** `/{target_folder}`\n\n¿Cuándo deseas que se elimine automáticamente?",
                    reply_markup=keyboard
                )
            except Exception as e:
                logger.error(f"No se pudo enviar notificación: {e}")
        
    except Exception as e:
        logger.error(f"Error procesando video: {e}")
        await status_msg.edit_text(f"❌ Ocurrió un error al procesar el video:\n`{str(e)}`")

@app.on_callback_query(filters.regex(r"^exp_"))
async def handle_expiration_callback(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    _, action, file_id = data.split("_", 2)
    
    deletions = load_deletions()
    if file_id not in deletions:
        await callback_query.answer("Este archivo ya no está en el registro.", show_alert=True)
        return
        
    admin_id = callback_query.from_user.id
    
    if action == "cust":
        admin_states[admin_id] = {"action": "awaiting_custom_date", "file_id": file_id}
        await callback_query.message.edit_text(
            callback_query.message.text + "\n\n✍️ **Por favor, escribe en el chat la cantidad de días** que debe durar el archivo (ej: `15`) o una fecha en formato `YYYY-MM-DD`."
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
    
    await callback_query.message.edit_text(
        callback_query.message.text + f"\n\n✅ **Autodestrucción programada:**\nEl archivo se eliminará en {text_confirm} ({date_str} - Hora Vzla)."
    )
    await callback_query.answer("Programado exitosamente.")

@app.on_message(filters.text & filters.private)
async def handle_admin_text(client: Client, message: Message):
    if message.text and message.text.startswith("/"):
        return
        
    admin_id = message.from_user.id
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
            if isinstance(info, dict):
                users[str(u_id)] = {
                    "ftp_path": info.get("ftp_path", "").strip("/"),
                    "name": info.get("name", "Usuario")
                }
                imported_count += 1
            elif isinstance(info, str):
                users[str(u_id)] = {
                    "ftp_path": info.strip("/"),
                    "name": "Usuario"
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
        base_names = {name, name.replace("_", "-"), name.replace("-", "_")}
        
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
                    
                    if success:
                        if NOTIFICATION_CHAT_ID and NOTIFICATION_CHAT_ID != "tu_chat_id_aqui":
                            try:
                                asyncio.run_coroutine_threadsafe(
                                    app.send_message(
                                        chat_id=int(NOTIFICATION_CHAT_ID),
                                        text=f"🗑️ **Autodestrucción Ejecutada**\nEl archivo `{file_name}` ha sido eliminado del FTP por haber cumplido su fecha de caducidad."
                                    ),
                                    loop
                                )
                            except Exception as e:
                                logger.error(f"Error enviando notif de borrado: {e}")
                                
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
