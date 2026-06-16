# listennotes-rss-extractor-v1

Script Python che scarica automaticamente i nuovi episodi di un podcast da un feed RSS, li converte in formato WAV e li trascrive in testo tramite **whisper.cpp**.

---

## Cosa fa

1. **Legge il feed RSS** configurato in `RSS_URL`.
2. **Filtra gli episodi** già processati (tracciati in `seen_items.json`) e quelli antecedenti alla data di cutoff (`CUTOFF_DATE`, di default `2026-04-01`).
3. Per ogni episodio nuovo:
   - Crea una cartella dedicata in `OUTPUT_DIR` (nome: `YYYY-MM-DD-titolo-episodio/`)
   - Scarica il file **MP3**
   - Converte in **WAV** mono 16 kHz con `ffmpeg`
   - Trascrive l'audio con **whisper.cpp** e salva il risultato in `transcript.txt`
4. Aggiorna `seen_items.json` dopo ogni episodio completato con successo, in modo che le esecuzioni successive saltino gli episodi già trattati.

---

## Requisiti

| Requisito | Note |
|-----------|------|
| **Python 3.13+** | Gestito automaticamente da `uv` |
| **uv** | Package manager / runner ([installazione](https://docs.astral.sh/uv/getting-started/installation/)) |
| **ffmpeg** | Deve essere disponibile nel `PATH` (`brew install ffmpeg`) |
| **whisper.cpp** | Già compilato in `/Users/simone/dev_ai/whisper.cpp` con il modello `ggml-large-v3-turbo.bin` |

Le dipendenze Python (`feedparser`, `requests`) vengono installate automaticamente da `uv`.

---

## Configurazione

Apri `main.py` e modifica le variabili in cima al file:

```python
RSS_URL     = "https://feeds.megaphone.fm/BCS7205693947"   # feed da processare
OUTPUT_DIR  = Path("/tmp/podcast-transcripts")              # cartella di output
WHISPER_DIR = Path("/Users/simone/dev_ai/whisper.cpp")     # percorso di whisper.cpp
CUTOFF_DATE = datetime(2026, 4, 1, tzinfo=timezone.utc)    # ignora episodi precedenti
```

---

## Utilizzo

```bash
# Prima esecuzione: installa le dipendenze e avvia
uv run main.py

# Esecuzioni successive (le dipendenze sono già nel .venv)
uv run main.py
```

Lo script stampa un log in tempo reale con timestamp:

```
[21:03:01] Fetching feed: https://feeds.megaphone.fm/...
[21:03:02] Feed: Il Podcast  (142 total entries)
[21:03:02] New episodes to process: 3

[21:03:02] ── Episode: Episodio 42 [2026-04-10]
[21:03:02]   ↓ Downloading audio from https://...
[21:03:18]   ✓ Saved to audio.mp3 (48320 KB)
[21:03:18]   ⚙ Converting to WAV …
[21:03:22]   ✓ WAV ready: audio.wav
[21:03:22]   🎙 Transcribing with whisper.cpp (this may take a while) …
[21:07:41]   ✓ Transcript saved: transcript.txt (12430 chars)
[21:07:41]   ✓ Episode complete → /tmp/podcast-transcripts/2026-04-10-episodio-42
```

---

## Struttura dell'output

```
OUTPUT_DIR/
└── 2026-04-10-titolo-episodio/
    ├── audio.mp3        ← file originale scaricato
    ├── audio.wav        ← convertito per whisper.cpp
    └── transcript.txt   ← trascrizione completa
```

---

## File di stato

`seen_items.json` (nella stessa cartella dello script) traccia i GUID degli episodi già processati. Eliminarlo forza la rielaborazione di tutti gli episodi che rientrano nel periodo configurato.
