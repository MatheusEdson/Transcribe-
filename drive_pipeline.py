"""
drive_pipeline.py — Ingestão Massiva do Google Drive com Shared Drive Support

Fluxo:
    1. Autentica via Service Account (GCP).
    2. Acessa a pasta compartilhada da Blips Educa garantindo visibilidade em Shared Drives.
    3. Coleta nome, ID e o webViewLink de cada arquivo.
    4. Baixa 1 vídeo por vez para temp/.
    5. Aciona o transcribe.py injetando o webViewLink e o BD.
    6. Deleta o vídeo local para preservar armazenamento.
"""

import os
import subprocess
from pathlib import Path
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ================= CONFIGURAÇÕES DE AMBIENTE =================
FOLDER_ID = '1X37PfW66j2doQGqrkqRgckaK4aOIRibf'
CREDENTIALS_FILE = 'config/credentials.json'
SCOPES = ['https://www.googleapis.com/auth/drive.readonly']
TEMP_DOWNLOAD_DIR = 'dados_drive_sim'
DB_PATH = 'database/blips_educa_para_socorro.sqlite'
# =============================================================

def authenticate_google_drive():
    if not os.path.exists(CREDENTIALS_FILE):
        raise FileNotFoundError(f"Arquivo não encontrado: {CREDENTIALS_FILE}.")
    
    print("[Nuvem] Autenticando com o Google Drive via Service Account...")
    creds = service_account.Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
    return build('drive', 'v3', credentials=creds)

def list_mp4_files(service):
    print(f"[Nuvem] Mapeando Shared Drives na pasta ID: {FOLDER_ID}...")
    query = f"'{FOLDER_ID}' in parents and mimeType contains 'video/mp4' and trashed = false"
    
    # Inserção exata das exigências de infraestrutura para Service Accounts
    results = service.files().list(
        q=query,
        spaces='drive',
        fields="nextPageToken, files(id, name, size, webViewLink)",
        pageSize=1000,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute()
    
    items = results.get('files', [])
    print(f"[Nuvem] {len(items)} vídeos encontrados.")
    return items

def download_file(service, file_id: str, file_name: str, download_path: Path):
    print(f"\n[Download] Baixando: {file_name}...")
    # SupportsAllDrives também é obrigatório no get_media
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    
    with open(download_path, 'wb') as fd:
        downloader = MediaIoBaseDownload(fd, request, chunksize=1024*1024*10)
        done = False
        while done is False:
            status, done = downloader.next_chunk()
            if status:
                print(f"   Progresso: {int(status.progress() * 100)}%", end='\r')
    print(f"   Progresso: 100% - Concluído!")

def run_transcription_pipeline(temp_video_path: Path, web_view_link: str):
    print(f"[Pipeline] Acionando inteligência para: {temp_video_path.name}")
    
    command = [
        "python3", "transcribe.py", 
        str(TEMP_DOWNLOAD_DIR), 
        "--db", DB_PATH,
        "--drive-url", web_view_link
    ]
    
    process = subprocess.run(command, capture_output=False, text=True)
    
    if process.returncode != 0:
        print(f"[ERRO CRÍTICO] Falha ao processar {temp_video_path.name}")
        return False
    return True

def main():
    print("=== INICIANDO INGESTÃO DE NUVEM (BLIPS EDUCA) ===")
    Path(TEMP_DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)
    Path('config').mkdir(parents=True, exist_ok=True)
    
    try:
        drive_service = authenticate_google_drive()
        videos_na_nuvem = list_mp4_files(drive_service)
        
        if not videos_na_nuvem:
            print("Nenhum arquivo de vídeo disponível na pasta para processar.")
            return

        for video in videos_na_nuvem:
            file_id = video['id']
            file_name = video['name']
            web_view_link = video.get('webViewLink', 'Link não disponível')
            
            local_video_path = Path(TEMP_DOWNLOAD_DIR) / file_name
            
            # Download, Processamento e Cleanup
            download_file(drive_service, file_id, file_name, local_video_path)
            sucesso = run_transcription_pipeline(local_video_path, web_view_link)
            
            if local_video_path.exists():
                os.remove(local_video_path)
                print("[Cleanup] MP4 temporário apagado para liberar disco.")
                
    except Exception as err:
        print(f"[ERRO] Falha na comunicação com a API: {err}")

if __name__ == '__main__':
    main()