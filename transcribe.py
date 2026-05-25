"""
transcribe — MP4 → MP3 → TXT (Escalado para Pipeline de Segmentos)

Fluxo:
    1. Varre pastas e subpastas (segmentos) buscando MP4
    2. Extrai audio do video (MP4 → MP3) via ffmpeg
    3. Transcreve o audio com Whisper (local, offline)
    4. Salva transcricao em .txt (com Header de URL) nas pastas de segmento
    5. Registra origem em manifest.json central

Uso:
    python transcribe.py <arquivo.mp4> [opcoes]
    python transcribe.py pasta_principal 

Opcoes:
    --output, -o     Pasta de saida (default: ./output)
    --model, -m      Modelo Whisper: tiny|base|small|medium|large (default: small)
    --lang           Idioma (default: pt). Use None para auto-detectar
    --mp3-only       Apenas extrai o MP3, sem transcrever
    --skip-extract   Usa MP3 ja existente na pasta de saida
"""

import argparse
import json
import subprocess
import sys
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
    segment_name = mp4_path.parent.name if mp4_path.parent != input_root else "geral"
    segment_output_dir = base_output_dir / segment_name
    segment_output_dir.mkdir(parents=True, exist_ok=True)
    
    entry = {
        "source_file": mp4_path.name,
        "source_path": str(mp4_path.resolve()),
        "segment": segment_name,
        "processed_at": datetime.now(timezone.utc).isoformat(),
    }


    if not args.skip_extract:
        mp3_path = extract_audio(mp4_path, segment_output_dir)
    else:
        mp3_path = segment_output_dir / (mp4_path.stem + ".mp3")
        if not mp3_path.exists():
            print(f"  MP3 nao encontrado em {mp3_path}, extraindo...")
            mp3_path = extract_audio(mp4_path, segment_output_dir)

    entry["mp3_file"] = mp3_path.name
    entry["mp3_size_mb"] = round(mp3_path.stat().st_size / 1024 / 1024, 2)

    if args.mp3_only:
        return entry

    txt_path = segment_output_dir / (mp4_path.stem + ".txt")
    if txt_path.exists():
        print(f"  TXT existente: {txt_path.name}")
        text_content = txt_path.read_text(encoding="utf-8")
        word_count = len(text_content.split())
    else:
        raw_text = transcribe_audio(mp3_path, args.model, args.lang)
        
        mock_url = f"https://drive.google.com/open?id={mp4_path.stem}_mock_id"
        formatted_text = f"URL DE ORIGEM NO DRIVE: {mock_url}\n"
        formatted_text += "="*60 + "\n\n"
        formatted_text += raw_text
        
        txt_path.write_text(formatted_text, encoding="utf-8")
        word_count = len(raw_text.split())
        print(f"  TXT salvo: {txt_path.name} ({word_count:,} palavras) no segmento [{segment_name}]")

    entry["txt_file"] = txt_path.name
    entry["word_count"] = word_count
    entry["preview"] = "Preview omitido para otimizar log."

    return entry


def main():
    parser = argparse.ArgumentParser(
        description="MP4 → MP3 → TXT com rastreio de origem e segmentacao automatica"
    )
    parser.add_argument("input", help="Arquivo MP4 ou pasta base com MP4s")
    parser.add_argument("--output", "-o", default="output", help="Pasta de saida principal")
    parser.add_argument("--model", "-m", default="small",
                        choices=["tiny", "base", "small", "medium", "large"],
                        help="Modelo Whisper (default: small)")
    parser.add_argument("--lang", default="pt",
                        help="Idioma para Whisper (default: pt). Use 'None' para auto")
    parser.add_argument("--mp3-only", action="store_true",
                        help="Apenas extrai MP3, sem transcrever")
    parser.add_argument("--skip-extract", action="store_true",
                        help="Pula extracao, usa MP3 existente")
    args = parser.parse_args()


    if not check_ffmpeg():
        print("Erro: ffmpeg nao encontrado.")
        print("Instale: winget install Gyan.FFmpeg")
        sys.exit(1)

    if not args.mp3_only and not check_whisper():
        print("Erro: whisper nao instalado.")
        print("Instale: pip install openai-whisper")
        sys.exit(1)

    input_path = Path(args.input)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    
    if input_path.is_dir():
        
        mp4_files = sorted(input_path.rglob("*.mp4"))
        if not mp4_files:
            print(f"Nenhum MP4 encontrado em {input_path} ou subpastas.")
            sys.exit(1)
    elif input_path.is_file():
        mp4_files = [input_path]
    else:
        print(f"Arquivo/pasta nao encontrado: {input_path}")
        sys.exit(1)

    print(f"Iniciando pipeline... Processando {len(mp4_files)} arquivo(s) para {output_dir}/")

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

    print(f"\nOperação Concluida. Manifest central: {output_dir / MANIFEST_FILE}")


if __name__ == "__main__":
    main()