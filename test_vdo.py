import os
import asyncio
from dotenv import load_dotenv
import aiohttp
from bs4 import BeautifulSoup

load_dotenv()

async def control_vdo_panel(action_url: str) -> bool:
    vdo_user = os.getenv("VDOPANEL_USER")
    vdo_pass = os.getenv("VDOPANEL_PASS")
    
    if not vdo_user or not vdo_pass or vdo_user == "tu_usuario_panel_aqui":
        print("❌ Credenciales de VDO Panel no configuradas en .env")
        return False
        
    login_url = "https://stream.conexion.com.ve/broadcaster/login"
    
    try:
        async with aiohttp.ClientSession() as session:
            print("1. Obteniendo página de login para extraer CSRF token...")
            async with session.get(login_url) as resp:
                html = await resp.text()
                
            soup = BeautifulSoup(html, 'html.parser')
            
            payload = {
                "name": vdo_user,
                "password": vdo_pass
            }
            
            for hidden in soup.find_all("input", type="hidden"):
                name = hidden.get("name")
                value = hidden.get("value")
                if name:
                    payload[name] = value
                    
            print(f"2. Haciendo POST a login con las credenciales... (Campos: {list(payload.keys())})")
            async with session.post(login_url, data=payload) as resp:
                print(f"   -> URL tras login: {resp.url} (Status: {resp.status})")
                if resp.status not in [200, 302] or str(resp.url).endswith("/login"):
                    print(f"❌ Fallo al loguear en VDO Panel. Credenciales inválidas o token erróneo.")
                    return False
                
            print("3. Login exitoso, ejecutando accion de detener stream...")
            async with session.get(action_url) as resp:
                print(f"   -> URL tras acción: {resp.url} (Status: {resp.status})")
                if resp.status in [200, 302, 301]:
                    print("✅ Éxito absoluto")
                    return True
                else:
                    print(f"❌ Fallo al ejecutar la acción: HTTP {resp.status}")
                    return False
    except Exception as e:
        print(f"❌ Excepción: {e}")
        return False

if __name__ == "__main__":
    asyncio.run(control_vdo_panel("https://stream.conexion.com.ve/broadcaster/stop-webtv"))
