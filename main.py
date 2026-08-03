import os
import logging
from ftplib import FTP
from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.types import Message

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

if not BOT_TOKEN or BOT_TOKEN == "tu_token_aqui":
    print("⚠️ ERROR: POR FAVOR, COLOCA TU TOKEN EN EL ARCHIVO .env")
    exit(1)

if not API_ID or API_ID == "tu_api_id_aqui":
    print("⚠️ ERROR: Faltan API_ID y API_HASH en .env")
    print("⚠️ Ve a https://my.telegram.org, inicia sesión, ve a 'API development tools' y crea una app para obtenerlos.")
    exit(1)

# Inicializar cliente de Pyrogram
# Esto crea una sesión local llamada "mi_bot_videos.session"
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
        
        # 3. Eliminar archivo temporal local para no llenar el disco duro
        if os.path.exists(local_path):
            os.remove(local_path)
        
        await status_msg.edit_text(f"✅ ¡Video subido exitosamente!\nArchivo: `{file_name}`")
        
    except Exception as e:
        logger.error(f"Error procesando video: {e}")
        await status_msg.edit_text(f"❌ Ocurrió un error al procesar el video:\n`{str(e)}`")

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# Servidor web falso para que Render.com no apague el bot
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Bot is running!")
        
def run_dummy_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), DummyHandler)
    print(f"Servidor web falso escuchando en el puerto {port}")
    server.serve_forever()

if __name__ == "__main__":
    # Iniciar el servidor web falso en segundo plano
    threading.Thread(target=run_dummy_server, daemon=True).start()
    
    print("Iniciando bot con Pyrogram... (Soporta descargas de hasta 2GB)")
    app.run()
