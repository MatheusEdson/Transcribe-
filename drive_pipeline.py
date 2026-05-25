"""
drive_pipeline.py — Ingestão Massiva do Google Drive com Shared Drive Support

Fluxo:
    1. Autentica via Service Account (GCP).
    2. Acessa a pasta compartilhada da Blips Educa garantindo visibilidade em Shared Drives.
    3. Coleta nome, ID e o webViewLink de cada arquivo (com paginação completa).
    4. Baixa 1 vídeo por vez para temp/.
    5. Aciona o transcribe.py injetando o webViewLink e o BD.
    6. Deleta o vídeo local para preservar armazenamento.

Configuração via variáveis de ambiente ou argumentos CLI:
    DRIVE_FOLDER_ID       ID da pasta no Google Drive
    DRIVE_CREDENTIALS     Caminho para credentials.json da Service Account
    DRIVE_TEMP_DIR        Pasta temporária para downloads (default: dados_drive_sim)
    DRIVE_DB_PATH         Caminho do SQLite de metadados
"""

import os
import sys
import subprocess
from pathlib import Path
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

SCOPES = ['https://www.googleapis.com/auth/drive.readonly']


def get_config(args):
    """
    FIX #7: configurações via env vars (com fallback para args CLI).
    Nenhum valor sensível hardcoded no código.
    """
    return {
        "folder_id": args.folder_id or os.environ.get("DRIVE_FOLDER_ID", ""),
        "credentials": args.credentials or os.environ.get("DRIVE_CREDENTIALS", "config/credentials.json"),
        "temp_dir": args.temp_dir or os.environ.get("DRIVE_TEMP_DIR", "dados_drive_sim"),
        "db_path": args.db or os.environ.get("DRIVE_DB_PATH", ""),
    }


def authenticate_google_drive(credentials_file: str):
    if not Path(credentials_file).exists():
        raise FileNotFoundError(
            f"Credentials não encontrado: {credentials_file}\n"
            "Configure via --credentials ou variável DRIVE_CREDENTIALS"
        )
    print("[Nuvem] Autenticando com o Google Drive via Service Account...")
    creds = service_account.Credentials.from_service_account_file(
        credentials_file, scopes=SCOPES
    )
    return build('drive', 'v3', credentials=creds)


def list_mp4_files(service, folder_id: str) -> list:
    """
    FIX #8: implementa paginação completa — garante buscar TODOS os arquivos,
    não apenas os primeiros 1000.
    """
    print(f"[Nuvem] Mapeando Shared Drives na pasta ID: {folder_id}...")
    query = f"'{folder_id}' in parents and mimeType contains 'video/mp4' and trashed = false"

    all_items = []
    page_token = None

    while True:
        kwargs = dict(
            q=query,
            spaces='drive',
            fields="nextPageToken, files(id, name, size, webViewLink)",
            pageSize=1000,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        if page_token:
            kwargs["pageToken"] = page_token

        results = service.files().list(**kwargs).execute()
        all_items.extend(results.get('files', []))

        page_token = results.get('nextPageToken')
        if not page_token:
            break

    print(f"[Nuvem] {len(all_items)} vídeos encontrados.")
    return all_items


def download_file(service, file_id: str, file_name: str, download_path: Path):
    print(f"\n[Download] Baixando: {file_name}...")
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)

    with open(download_path, 'wb') as fd:
        downloader = MediaIoBaseDownload(fd, request, chunksize=1024 * 1024 * 10)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status:
                print(f"   Progresso: {int(status.progress() * 100)}%", end='\r')
    print("   Progresso: 100% - Concluído!")


def run_transcription_pipeline(
    video_path: Path,
    web_view_link: str,
    db_path: str,
) -> bool:
    """
    FIX #9: usa sys.executable em vez de 'python3' hardcoded.
             Funciona em Windows, macOS e Linux, dentro de venvs.
    FIX #10: retorno bool usado pelo caller para logar falhas.
    """
    print(f"[Pipeline] Acionando transcrição para: {video_path.name}")

    command = [
        sys.executable, "transcribe.py",
        str(video_path),          # arquivo específico, não o diretório inteiro
        "--drive-url", web_view_link,
    ]
    if db_path:
        command += ["--db", db_path]

    process = subprocess.run(command, capture_output=False, text=True)

    if process.returncode != 0:
        print(f"[ERRO] Falha ao processar: {video_path.name}")
        return False
    return True


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Ingestão de MP4s do Google Drive → transcrição local"
    )
    parser.add_argument("--folder-id", default=None,
                        help="ID da pasta no Drive (ou env DRIVE_FOLDER_ID)")
    parser.add_argument("--credentials", default=None,
                        help="Caminho do credentials.json (ou env DRIVE_CREDENTIALS)")
    parser.add_argument("--temp-dir", default=None,
                        help="Pasta temporária para downloads (ou env DRIVE_TEMP_DIR)")
    parser.add_argument("--db", default=None,
                        help="Caminho do SQLite de metadados (ou env DRIVE_DB_PATH)")
    parser.add_argument("--fail-fast", action="store_true",
                        help="Interrompe ao primeiro erro de transcrição")
    args = parser.parse_args()

    config = get_config(args)

    if not config["folder_id"]:
        print("Erro: DRIVE_FOLDER_ID não definido. Use --folder-id ou variável de ambiente.")
        sys.exit(1)

    print("=== INICIANDO INGESTÃO DE NUVEM (BLIPS EDUCA) ===")
    Path(config["temp_dir"]).mkdir(parents=True, exist_ok=True)

    failed = []

    try:
        drive_service = authenticate_google_drive(config["credentials"])
        videos = list_mp4_files(drive_service, config["folder_id"])

        if not videos:
            print("Nenhum vídeo MP4 disponível na pasta.")
            return

        for video in videos:
            file_id = video['id']
            file_name = video['name']
            web_view_link = video.get('webViewLink', '')
            local_path = Path(config["temp_dir"]) / file_name

            download_file(drive_service, file_id, file_name, local_path)

            # FIX #10: trata falha explicitamente
            ok = run_transcription_pipeline(local_path, web_view_link, config["db_path"])

            # QA #6: só apaga o MP4 local se a transcrição tiver OK
            if ok and local_path.exists():
                local_path.unlink()
                print("[Cleanup] MP4 temporário removido.")
            elif not ok and local_path.exists():
                print(f"[Aviso] MP4 mantido para retry manual: {local_path}")

            if not ok:
                failed.append(file_name)
                if args.fail_fast:
                    print("[ABORTANDO] --fail-fast ativado.")
                    break

    except Exception as err:
        print(f"[ERRO FATAL] {err}")
        sys.exit(1)

    if failed:
        print(f"\n⚠ {len(failed)} arquivo(s) falharam na transcrição:")
        for f in failed:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print("\n✓ Ingestão concluída com sucesso.")


if __name__ == '__main__':
    main()
