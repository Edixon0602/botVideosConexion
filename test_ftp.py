import os
from ftplib import FTP
from dotenv import load_dotenv

load_dotenv()

FTP_HOST = os.getenv("FTP_HOST")
FTP_USER = os.getenv("FTP_USER")
FTP_PASS = os.getenv("FTP_PASS")

try:
    print(f"Conectando a {FTP_HOST} con el usuario {FTP_USER}...")
    ftp = FTP(FTP_HOST)
    ftp.login(user=FTP_USER, passwd=FTP_PASS)
    
    current_dir = ftp.pwd()
    print(f"\nDirectorio actual al iniciar sesión: {current_dir}")
    
    print("\nCarpetas y archivos disponibles en la raíz del FTP:")
    for item in ftp.nlst():
        print(f" - {item}")
        
    ftp.quit()
except Exception as e:
    print(f"Error: {e}")
