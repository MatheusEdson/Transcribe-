"""
transcribe — MP4 → MP3 → TXT (Escalado para Pipeline de Segmentos + SQLite + Drive URL)

Fluxo:
    1. Varre pastas e subpastas buscando MP4.
    2. Consulta SQLite para resgatar Metadados (Segmento, Dúvidas, etc).
    3. Recebe a URL oficial do Drive via argumento.
    4. Copia o MP4 para a pasta do respectivo segmento.
    5. Extrai audio do video (MP4 → MP3) via ffmpeg.
    6. Transcreve o audio com Whisper (local, offline).
    7. Salva transcricao em .txt com Header completo (URL do Drive + SQLite).
    8. Registra origem e URL no manifest.json central.
"""

import argparse
import json
import subprocess
import sys
import sqlite3
import shutil
import re
from datetime import datetime, timezone
from pathlib import Path

MANIFEST_FILE = "manifest.json"
FFMPEG_ARGS = ["-vn", "-acodec", "libmp3lame", "-q:a", "4"]


def check_ffmpeg():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def check_whisper():
    try:
        import whisper
        return True
    except ImportError:
        return False


def normalize_string(text: str) -> str:
    """Normaliza strings removendo caracteres especiais e espaços para cruzamento de dados."""
    return re.sub(r'[^a-zA-Z0-9]', '', str(text)).lower()


def get_metadata_from_db(db_path: Path, video_filename: str) -> dict:
    if not db_path.exists():
        print(f"  [Aviso] Banco de dados não encontrado em: {db_path}")
        return None
        
    video_stem = normalize_string(Path(video_filename).stem)
    
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [t["name"] for t in cursor.fetchall()]
        
        for t_name in tables:
            cursor.execute(f"SELECT * FROM `{t_name}`")
            rows = cursor.fetchall()
            for row in rows:
                row_dict = dict(row)
                for key, value in row_dict.items():
                    if value and isinstance(value, str):
                        if video_stem in normalize_string(value):
                            return row_dict
        return None
    except Exception as e:
        print(f"  [Erro] Falha ao ler o banco de dados SQLite: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()


def extract_audio(mp4_path: Path, output_dir: Path) -> Path:
    mp3_path = output_dir / (mp4_path.stem + ".mp3")
    if mp3_path.exists():
        print(f"  MP3 existente: {mp3_path.name}")
        return mp3_path

    print(f"  Extraindo audio: {mp4_path.name} → {mp3_path.name}")
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", str(mp4_path)] + FFMPEG_ARGS + [str(mp3_path)],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"  Erro ffmpeg:\n{result.stderr[-500:]}")
        sys.exit(1)

    return mp3_path


def transcribe_audio(mp3_path: Path, model_name: str, lang: str) -> str:
    import whisper

    print(f"  Transcrevendo: {mp3_path.name} (modelo={model_name}, lang={lang})")
    model = whisper.load_model(model_name)

    options = {}
    if lang and lang.lower() != "none":
        options["language"] = lang

    result = model.transcribe(str(mp3_path), **options)
    return result["text"].strip()


def load_manifest(output_dir: Path) -> dict:
    path = output_dir / MANIFEST_FILE
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"entries": []}


def save_manifest(manifest: dict, output_dir: Path):
    path = output_dir / MANIFEST_FILE
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def process_file(mp4_path: Path, base_output_dir: Path, input_root: Path, args) -> dict:
    # 1. Consulta Metadados no Banco SQLite
    metadata = None
    if args.db:
        db_p = Path(args.db)
        metadata = get_metadata_from_db(db_p, mp4_path.name)

    # 2. Definição do Segmento
    segment_name = None
    if metadata:
        for k, v in metadata.items():
            k_lower = k.lower()
            if "segmento" in k_lower and v:
                segment_name = str(v).strip()
    
    if not segment_name:
        segment_name = mp4_path.parent.name if mp4_path.parent != input_root else "geral"

    segment_output_dir = base_output_dir / segment_name
    segment_output_dir.mkdir(parents=True, exist_ok=True)
    
    # 3. Copia o vídeo para a pasta final
    final_mp4_path = segment_output_dir / mp4_path.name
    if not final_mp4_path.exists():
        print(f"  Copiando vídeo para estrutura final: {final_mp4_path.name}")
        shutil.copy2(mp4_path, final_mp4_path)

    # 4. Rastreio no Manifest com o webViewLink do Drive
    entry = {
        "source_file": mp4_path.name,
        "segment": segment_name,
        "drive_url": args.drive_url if args.drive_url else "LOCAL",
        "database_match": bool(metadata),
        "processed_at": datetime.now(timezone.utc).isoformat(),
    }

    if not args.skip_extract:
        mp3_path = extract_audio(final_mp4_path, segment_output_dir)
    else:
        mp3_path = segment_output_dir / (mp4_path.stem + ".mp3")
        if not mp3_path.exists():
            mp3_path = extract_audio(final_mp4_path, segment_output_dir)

    entry["mp3_file"] = mp3_path.name

    if args.mp3_only:
        return entry

    txt_path = segment_output_dir / (mp4_path.stem + ".txt")
    if txt_path.exists():
        print(f"  TXT existente: {txt_path.name}")
        text_content = txt_path.read_text(encoding="utf-8")
        word_count = len(text_content.split())
    else:
        raw_text = transcribe_audio(mp3_path, args.model, args.lang)
        
        # Montagem Estruturada do Cabeçalho com o Link do Drive solicitado pelo Matheus
        header_lines = [
            f"LINK OFICIAL DO GOOGLE DRIVE: {args.drive_url if args.drive_url else 'Não fornecido'}",
            "-" * 60
        ]
        
        if metadata:
            header_lines.append("METADADOS (BLIPS EDUCA - SQLITE):")
            for k, v in metadata.items():
                if v and str(v).strip() != "":
                    header_lines.append(f"{str(k).upper()}: {v}")
            header_lines.append("-" * 60)
        else:
            header_lines.append("ALERTA: Vídeo não localizado no Banco de Dados SQLite.")
            header_lines.append("-" * 60)
            
        formatted_text = "\n".join(header_lines) + "\n\n"
        formatted_text += "TRANSCRIÇÃO OFICIAL:\n"
        formatted_text += raw_text
        
        txt_path.write_text(formatted_text, encoding="utf-8")
        word_count = len(raw_text.split())
        print(f"  TXT salvo: {txt_path.name} ({word_count:,} palavras) no segmento [{segment_name}]")

    entry["txt_file"] = txt_path.name
    entry["word_count"] = word_count

    return entry


def main():
    parser = argparse.ArgumentParser(description="MP4 → MP3 → TXT integrado com SQLite e Google Drive API")
    parser.add_argument("input", help="Arquivo MP4 ou pasta base com MP4s")
    parser.add_argument("--output", "-o", default="output", help="Pasta de saida principal")
    parser.add_argument("--db", default=None, help="Caminho para o arquivo de banco de dados SQLite (.sqlite)")
    parser.add_argument("--drive-url", default=None, help="Link oficial webViewLink vindo da API do Google Drive")
    parser.add_argument("--model", "-m", default="small", choices=["tiny", "base", "small", "medium", "large"])
    parser.add_argument("--lang", default="pt")
    parser.add_argument("--mp3-only", action="store_true")
    parser.add_argument("--skip-extract", action="store_true")
    args = parser.parse_args()

    if not check_ffmpeg():
        print("Erro: ffmpeg nao encontrado.")
        sys.exit(1)

    if not args.mp3_only and not check_whisper():
        print("Erro: whisper nao instalado.")
        sys.exit(1)

    input_path = Path(args.input)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    if input_path.is_dir():
        mp4_files = sorted([f for f in input_path.rglob("*") if f.suffix.lower() == ".mp4"])
        if not mp4_files:
            print(f"Nenhum MP4 encontrado em {input_path} ou subpastas.")
            sys.exit(1)
    elif input_path.is_file():
        mp4_files = [input_path]
    else:
        print(f"Arquivo/pasta nao encontrado: {input_path}")
        sys.exit(1)

    print(f"Iniciando pipeline... Processando {len(mp4_files)} arquivo(s)")

    manifest = load_manifest(output_dir)
    existing = {e["source_file"] for e in manifest["entries"]}

    for mp4 in mp4_files:
        print(f"\n[Mídia: {mp4.name}]")
        if mp4.name in existing and not args.mp3_only:
            print("  Ja processado (manifest.json). Pulando.")
            continue

        entry = process_file(mp4, output_dir, input_path, args)
        manifest["entries"].append(entry)
        save_manifest(manifest, output_dir)

    print(f"\nOperação Concluída. Manifest central: {output_dir / MANIFEST_FILE}")


if __name__ == "__main__":
    main()