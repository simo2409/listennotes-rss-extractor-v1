# listennotes-rss-extractor-v1

Script Python che scarica automaticamente i nuovi episodi di uno o più podcast dai feed RSS, li converte in formato WAV e li trascrive in testo tramite **whisper.cpp**.

---

## Cosa fa

1. **Legge i feed RSS** configurati in `config.json`.
2. **Raccoglie tutti gli episodi candidati** non ancora processati, non esplicitamente esclusi e non antecedenti alla data di cutoff (`cutoff_date`, di default `2026-04-01`).
3. **Mostra la lista completa** all'utente, che può rimuovere gli episodi da non processare. Gli esclusi vengono salvati in `excluded_items.json` e **non verranno più proposti** nelle esecuzioni successive.
4. Dopo conferma, **processa gli episodi in parallelo** con un numero configurabile di worker (`parallel_workers`, default `2`).
5. Per ogni episodio:
   - Crea una cartella dedicata in `output_dir` (nome: `YYYY-MM-DD-titolo-episodio/`)
   - Scarica il file **MP3**
   - Converte in **WAV** mono 16 kHz con `ffmpeg`
   - Trascrive l'audio con **whisper.cpp** e salva il risultato in un file `.txt`
6. Aggiorna `seen_items.json` con i GUID completati con successo, in modo che le esecuzioni successive saltino gli episodi già trattati.

### Ottimizzazioni per non bloccare il Mac

- I worker sono limitati di default a **2** (`parallel_workers`).
- I processi `ffmpeg` e `whisper.cpp` vengono eseguiti con `nice -n 10` su macOS/Linux, riducendo l'impatto sul sistema quando è sotto carico.
- Il numero di thread usati da ogni istanza di whisper.cpp è limitato automaticamente in base ai core disponibili e al numero di worker (ad esempio, su 8 core con 2 worker ogni istanza userà fino a 4 thread). È configurabile con `whisper_threads`.

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

Copia `config.default.json` in `config.json` e personalizzalo:

```json
{
  "whisper_dir": "/Users/simone/dev_ai/whisper.cpp",
  "whisper_model": "models/ggml-large-v3-turbo.bin",
  "cutoff_date": "2026-04-01",
  "parallel_workers": 2,
  "whisper_threads": null,
  "feeds": [
    {
      "url": "https://feeds.megaphone.fm/BCS7205693947",
      "output_dir": "../../raw/seietrenta_podcast",
      "output_txt": true,
      "output_mp3": false,
      "output_wav": false
    }
  ]
}
```

### Campi di configurazione

| Campo | Descrizione |
|-------|-------------|
| `whisper_dir` | Percorso della directory contenente whisper.cpp |
| `whisper_model` | Percorso relativo del modello rispetto a `whisper_dir` |
| `cutoff_date` | Ignora episodi pubblicati prima di questa data (`YYYY-MM-DD`) |
| `parallel_workers` | Numero di episodi processati contemporaneamente (default `2`) |
| `whisper_threads` | Thread per istanza whisper.cpp (`null` = calcolato automaticamente) |
| `feeds` | Lista dei feed RSS da monitorare |

---

## Utilizzo

```bash
# Prima esecuzione: installa le dipendenze e avvia
uv run main.py

# Esecuzioni successive (le dipendenze sono già nel .venv)
uv run main.py
```

Lo script stampa il log in tempo reale con timestamp e, per la modalità parallela, un prefisso `[wN]` che identifica il worker.

### Flusso interattivo

```
[21:03:01] Fetching feed: https://feeds.megaphone.fm/...
[21:03:02] Feed: Il Podcast (142 total entries)
[21:03:02] New episodes to consider: 3

============================================================
EPISODI IN ATTESA DI PROCESSING
============================================================
  [1] Il Podcast — Episodio 42 [2026-04-10]
  [2] Il Podcast — Episodio 43 [2026-04-17]
  [3] Altro Podcast — Intervista [2026-04-18]

Suggerimento: inserisci i numeri degli episodi da saltare
  (es. '1,3,5-7' oppure '2 4 6'). Invio per processarli tutti.

Episodi da escludere: 2
  Escluso: Episodio 43
[21:03:03] 1 episodi esclusi e salvati in excluded_items.json

Pronti a processare 2 episodi con 2 worker
(whisper.cpp userà fino a 4 thread per istanza).
Procedere? [Y/n]
```

### Esempio di processing parallelo

```
[21:03:05] [w0] ── Episode: Episodio 42 [2026-04-10]
[21:03:05] [w1] ── Episode: Intervista [2026-04-18]
[21:03:21] [w0]   ✓ Saved to audio.mp3 (48320 KB)
[21:03:22] [w0]   ⚙ Converting to WAV …
...
[21:07:41] [w0]   ✓ Episode complete → .../2026-04-10-episodio-42
[21:08:12] [w1]   ✓ Episode complete → .../2026-04-18-intervista
[21:08:12] Episodi completati in questo batch: 2/2
[21:08:12]
✓ All feeds processed.
```

---

## Struttura dell'output

```
OUTPUT_DIR/
└── 2026-04-10-titolo-episodio/
    ├── audio.mp3        ← file originale scaricato (se output_mp3=true)
    ├── audio.wav        ← convertito per whisper.cpp (se output_wav=true)
    └── audio.txt        ← trascrizione completa (se output_txt=true)
```

I nomi dei file derivano dal nome originale dell'audio nel feed.

---

## File di stato

- **`seen_items.json`** (nella stessa cartella dello script) traccia i GUID degli episodi già processati con successo. Eliminarlo forza la rielaborazione di tutti gli episodi che rientrano nel periodo configurato.
- **`excluded_items.json`** traccia i GUID degli episodi che hai scelto di saltare durante la fase interattiva. Anche eliminando `seen_items.json`, gli esclusi **non** verranno riproposti finché questo file esiste.

---

## Note sulla parallelizzazione

- Il numero di worker è configurabile con `parallel_workers`. Valori più alti velocizzano il processing ma aumentano il carico sulla CPU e sul disco.
- Se imposti `parallel_workers: 1`, lo script torna alla modalità sequenziale e riattiva lo skip interattivo con il tasto `x`.
- In modalità parallela lo skip con `x` è disabilitato perché non è possibile associare in modo affidabile la pressione del tasto a un singolo worker. Puoi sempre usare `Ctrl+C` per interrompere l'esecuzione.
