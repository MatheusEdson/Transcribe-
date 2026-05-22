# Transcribe

**MP4 → MP3 → TXT** com rastreio de origem.

Extrai o audio de videos, transcreve com Whisper (local, gratuito) e salva um `manifest.json` com o nome original de cada arquivo processado.

## Setup

```bash
# 1. ffmpeg
npm run setup:ffmpeg

# 2. Whisper (Python)
npm run setup
```

## Uso

```bash
# Um arquivo
npm run transcribe -- video.mp4

# Pasta inteira de MP4s
npm run transcribe -- videos/

# Opcoes
npm run transcribe -- video.mp4 --output transcripts/ --model medium --lang pt

# Apenas extrair MP3 (sem transcrever)
npm run transcribe:mp3 -- video.mp4

# Modelo mais rapido (menos preciso)
npm run transcribe:fast -- video.mp4

# Ver manifest
npm run manifest
```

## Modelos Whisper

| Modelo | Velocidade | Precisao | RAM |
|--------|-----------|----------|-----|
| tiny   | muito rapido | baixa | ~1 GB |
| base   | rapido | media | ~1 GB |
| **small** | **bom custo-beneficio** | **boa** | ~2 GB |
| medium | lento | muito boa | ~5 GB |
| large  | muito lento | excelente | ~10 GB |

Default: `small`

## Saida

```
output/
  video.mp3           # audio extraido
  video.txt           # transcricao
  manifest.json       # rastreio de origem
```

`manifest.json` exemplo:
```json
{
  "entries": [
    {
      "source_file": "aula-01.mp4",
      "source_path": "C:/Users/.../aula-01.mp4",
      "processed_at": "2026-05-22T10:00:00+00:00",
      "mp3_file": "aula-01.mp3",
      "mp3_size_mb": 12.4,
      "txt_file": "aula-01.txt",
      "word_count": 3421,
      "preview": "Bem-vindos à aula de hoje..."
    }
  ]
}
```

## Requisitos

- Python 3.10+
- Node.js (scripts npm)
- ffmpeg (`npm run setup:ffmpeg`)
