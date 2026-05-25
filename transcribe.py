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

Uso:
    python transcribe.py <pasta_ou_arquivo.mp4> [opcoes]
    python transcribe.py videos/ --db database/blips.sqlite --drive-url https://...
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
TRANSCRIPT_MARKER = "TRANSCRIÇÃO OFICIAL:\n"


def check_ffmpeg():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def check_whisper():
    try:
        import whisper  # noqa: F401
        return True
    except ImportError:
        return False


def normalize_string(text: str) -> str:
    """Normaliza strings removendo caracteres especiais para cruzamento de dados."""
    return re.sub(r'[^a-zA-Z0-9]', '', str(text)).lower()


def sanitize_segment_name(name: str) -> str:
    """
    Sanitiza o nome do segmento para uso seguro como componente de path.
    Remove path traversal, separadores e caracteres não permitidos.
    FIX #3: previne path traversal de valores vindos do SQLite.
    """
    # Remove separadores de caminho e pontos sequenciais
    name = re.sub(r'[/\\]', '_', name)
    name = re.sub(r'\.{2,}', '.', name)
    # Mantém apenas chars seguros: alfanumérico, espaço, hífen, underscore, ponto
    name = re.sub(r'[^\w\s\-.]', '', name, flags=re.UNICODE)
    name = name.strip('. ')
    return name or "geral"


def build_db_cache(db_path: Path) -> list[dict] | None:
    """
    Carrega todas as linhas do SQLite em memória uma única vez.
    FIX #5: evita full scan por vídeo — O(1) lookup em vez de O(n*m).
    Retorna lista de dicts ou None se o arquivo não existir.
    """
    if not db_path.exists():
        print(f"  [Aviso] Banco de dados não encontrado em: {db_path}")
        return None

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [t["name"] for t in cursor.fetchall()]

        all_rows = []
        for t_name in tables:
            cursor.execute(f"SELECT * FROM `{t_name}`")
            for row in cursor.fetchall():
                all_rows.append(dict(row))

        print(f"  [DB] Cache carregado: {len(all_rows)} registros de {len(tables)} tabela(s)")
        return all_rows
    except Exception as e:
        print(f"  [Erro] Falha ao carregar o banco de dados SQLite: {e}")
        return None
    finally:
        conn.close()


def lookup_metadata(db_cache: list[dict], video_filename: str) -> dict | None:
    """Busca metadados no cache em memória pelo nome do vídeo."""
    if not db_cache:
        return None

    video_stem = normalize_string(Path(video_filename).stem)
    for row in db_cache:
        for value in row.values():
            if value and isinstance(value, str):
                if video_stem in normalize_string(value):
                    return row
    return None


def extract_audio(mp4_path: Path, output_dir: Path) -> Path:
    mp3_path = output_dir / (mp4_path.stem + ".mp3")
    if mp3_path.exists():
        print(f"  MP3 existente: {mp3_path.name}")
        return mp3_path

    print(f"  Extraindo audio: {mp4_path.name} → {mp3_path.name}")
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", str(mp4_path)] + FFMPEG_ARGS + [str(mp3_path)],
        capture_output=True, text=True,
        timeout=7200,  # 2h max por arquivo
    )
    if result.returncode != 0:
        # QA #2: lança exceção em vez de sys.exit — permite o loop continuar
        raise RuntimeError(f"ffmpeg falhou em {mp4_path.name}:\n{result.stderr[-500:]}")

    return mp3_path


def load_whisper_model(model_name: str):
    """
    QA #3: carrega o modelo Whisper uma única vez e retorna.
    Chamar whisper.load_model() dentro do loop recarrega ~450 MB por vídeo.
    """
    import whisper
    print(f"  [Whisper] Carregando modelo '{model_name}'...")
    return whisper.load_model(model_name)


def transcribe_audio(mp3_path: Path, model, lang: str) -> str:
    """Transcreve usando um modelo já carregado (não recarrega por vídeo)."""
    print(f"  Transcrevendo: {mp3_path.name} (lang={lang})")
    options = {}
    if lang and lang.lower() != "none":
        options["language"] = lang
    result = model.transcribe(str(mp3_path), **options)
    return result["text"].strip()


def extract_transcript_from_txt(txt_content: str) -> str:
    """
    Extrai apenas a transcrição de um TXT existente (ignora o cabeçalho).
    FIX #4: garante word_count consistente entre TXT novo e existente.
    """
    if TRANSCRIPT_MARKER in txt_content:
        return txt_content.split(TRANSCRIPT_MARKER, 1)[1]
    return txt_content


def load_manifest(output_dir: Path) -> dict:
    path = output_dir / MANIFEST_FILE
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"entries": []}


def save_manifest(manifest: dict, output_dir: Path):
    path = output_dir / MANIFEST_FILE
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def process_file(
    mp4_path: Path,
    base_output_dir: Path,
    input_root: Path,
    args,
    db_cache: list[dict] | None,
    whisper_model=None,
) -> dict:
    # 1. Consulta metadados no cache SQLite (O(1), sem I/O extra)
    metadata = lookup_metadata(db_cache, mp4_path.name) if db_cache else None

    # 2. Definição do segmento com sanitização de path traversal
    segment_name = None
    if metadata:
        for k, v in metadata.items():
            if "segmento" in k.lower() and v:
                segment_name = str(v).strip()
                break  # primeiro match é suficiente

    if not segment_name:
        # FIX #2: input_root é sempre um diretório (garantido em main())
        segment_name = mp4_path.parent.name if mp4_path.parent != input_root else "geral"

    # FIX #3: sanitiza segment_name antes de montar o path
    segment_name = sanitize_segment_name(segment_name)

    segment_output_dir = base_output_dir / segment_name
    segment_output_dir.mkdir(parents=True, exist_ok=True)

    # 3. Copia o vídeo para a estrutura final
    final_mp4_path = segment_output_dir / mp4_path.name
    if not final_mp4_path.exists():
        print(f"  Copiando vídeo para estrutura final: {final_mp4_path.name}")
        shutil.copy2(mp4_path, final_mp4_path)

    # 4. Entrada do manifest — FIX #1: source_path como chave única de dedup
    entry = {
        "source_file": mp4_path.name,
        "source_path": str(mp4_path.resolve()),
        "segment": segment_name,
        "drive_url": args.drive_url if args.drive_url else "LOCAL",
        "database_match": bool(metadata),
        "processed_at": datetime.now(timezone.utc).isoformat(),
    }

    # 5. Extração de áudio
    if not args.skip_extract:
        mp3_path = extract_audio(final_mp4_path, segment_output_dir)
    else:
        mp3_path = segment_output_dir / (mp4_path.stem + ".mp3")
        if not mp3_path.exists():
            mp3_path = extract_audio(final_mp4_path, segment_output_dir)

    entry["mp3_file"] = mp3_path.name
    entry["mp3_size_mb"] = round(mp3_path.stat().st_size / 1024 / 1024, 2)

    if args.mp3_only:
        return entry

    # 6. Transcrição
    txt_path = segment_output_dir / (mp4_path.stem + ".txt")
    if txt_path.exists():
        print(f"  TXT existente: {txt_path.name}")
        # FIX #4: conta apenas a transcrição, não o cabeçalho
        content = txt_path.read_text(encoding="utf-8")
        word_count = len(extract_transcript_from_txt(content).split())
    else:
        raw_text = transcribe_audio(mp3_path, whisper_model, args.lang)

        header_lines = [
            f"LINK OFICIAL DO GOOGLE DRIVE: {args.drive_url or 'Não fornecido'}",
            "-" * 60,
        ]

        if metadata:
            header_lines.append("METADADOS (BLIPS EDUCA - SQLITE):")
            for k, v in metadata.items():
                if v and str(v).strip():
                    header_lines.append(f"{str(k).upper()}: {v}")
            header_lines.append("-" * 60)
        else:
            header_lines.append("ALERTA: Vídeo não localizado no Banco de Dados SQLite.")
            header_lines.append("-" * 60)

        formatted_text = "\n".join(header_lines) + "\n\n"
        formatted_text += TRANSCRIPT_MARKER
        formatted_text += raw_text

        txt_path.write_text(formatted_text, encoding="utf-8")
        word_count = len(raw_text.split())
        print(f"  TXT salvo: {txt_path.name} ({word_count:,} palavras) [{segment_name}]")

    entry["txt_file"] = txt_path.name
    entry["word_count"] = word_count

    return entry


def main():
    parser = argparse.ArgumentParser(
        description="MP4 → MP3 → TXT integrado com SQLite e Google Drive API"
    )
    parser.add_argument("input", help="Arquivo MP4 ou pasta base com MP4s")
    parser.add_argument("--output", "-o", default="output", help="Pasta de saida principal")
    parser.add_argument("--db", default=None, help="Caminho para o SQLite (.sqlite)")
    parser.add_argument("--drive-url", default=None, help="webViewLink do Google Drive")
    parser.add_argument("--model", "-m", default="small",
                        choices=["tiny", "base", "small", "medium", "large"])
    parser.add_argument("--lang", default="pt",
                        help="Idioma Whisper (default: pt). Use 'None' para auto-detectar")
    parser.add_argument("--mp3-only", action="store_true",
                        help="Apenas extrai MP3, sem transcrever")
    parser.add_argument("--skip-extract", action="store_true",
                        help="Pula extração, usa MP3 existente")
    args = parser.parse_args()

    if not check_ffmpeg():
        print("Erro: ffmpeg nao encontrado. Instale: winget install Gyan.FFmpeg")
        sys.exit(1)

    if not args.mp3_only and not check_whisper():
        print("Erro: whisper nao instalado. Instale: pip install openai-whisper")
        sys.exit(1)

    input_path = Path(args.input)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    if input_path.is_dir():
        mp4_files = sorted([f for f in input_path.rglob("*") if f.suffix.lower() == ".mp4"])
        # FIX #2: input_root sempre diretório
        input_root = input_path
        if not mp4_files:
            print(f"Nenhum MP4 encontrado em {input_path} ou subpastas.")
            sys.exit(1)
    elif input_path.is_file():
        mp4_files = [input_path]
        # FIX #2: arquivo único → root é o diretório pai
        input_root = input_path.parent
    else:
        print(f"Arquivo/pasta nao encontrado: {input_path}")
        sys.exit(1)

    print(f"Iniciando pipeline... {len(mp4_files)} arquivo(s) → {output_dir}/")

    # FIX #5: carrega SQLite uma vez, reutiliza em todos os vídeos
    db_cache = build_db_cache(Path(args.db)) if args.db else None

    # QA #3: carrega Whisper uma única vez antes do loop
    whisper_model = None
    if not args.mp3_only:
        whisper_model = load_whisper_model(args.model)

    manifest = load_manifest(output_dir)
    # FIX #1: dedup por source_path (path completo), não só pelo nome
    existing = {e.get("source_path", "") for e in manifest["entries"]}

    failed = []
    for mp4 in mp4_files:
        print(f"\n[Mídia: {mp4.name}]")
        source_path = str(mp4.resolve())
        if source_path in existing and not args.mp3_only:
            print("  Ja processado (manifest.json). Pulando.")
            continue

        try:
            # QA #2: captura RuntimeError do ffmpeg — continua o batch
            entry = process_file(mp4, output_dir, input_root, args, db_cache, whisper_model)
            manifest["entries"].append(entry)
            save_manifest(manifest, output_dir)
        except RuntimeError as e:
            print(f"  [ERRO] {e}")
            failed.append(mp4.name)

    if failed:
        print(f"\n⚠ {len(failed)} arquivo(s) falharam: {', '.join(failed)}")

    print(f"\nOperação Concluída. Manifest: {output_dir / MANIFEST_FILE}")


if __name__ == "__main__":
    main()
