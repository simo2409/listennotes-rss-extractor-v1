"""
RSS podcast extractor: fetches new episodes, downloads audio,
converts to WAV, and transcribes with whisper.cpp.

Interactively presents the list of pending episodes so the user can
exclude items before processing. Excluded GUIDs are persisted and will
not be offered again. Processing runs in parallel (configurable workers)
and subprocesses are niced to keep the Mac responsive.
"""

import json
import os
import re
import atexit
import select
import subprocess
import sys
import termios
import threading
import tty
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import requests

# ── Configuration ──────────────────────────────────────────────────────────────

CONFIG_FILE = Path(__file__).parent / "config.json"
STATE_FILE = Path(__file__).parent / "seen_items.json"
EXCLUDED_FILE = Path(__file__).parent / "excluded_items.json"
IMPORT_LOG_FILE = Path(__file__).parent / "import.log"
OUT_COUNT_FILE = Path(__file__).parent / "out_count.txt"


def load_config() -> dict:
    return json.loads(CONFIG_FILE.read_text())


def _parse_cutoff(date_str: str) -> datetime:
    y, m, d = (int(x) for x in date_str.split("-"))
    return datetime(y, m, d, tzinfo=timezone.utc)


# ── Logging ────────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def log_import(msg: str) -> None:
    """Append a timestamped line to import.log (created/reset at the start of main)."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(IMPORT_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")


# ── State persistence ──────────────────────────────────────────────────────────

_seen_lock = threading.Lock()


def load_seen() -> set[str]:
    """Return the set of GUIDs that have already been processed."""
    if STATE_FILE.exists():
        data = json.loads(STATE_FILE.read_text())
        if isinstance(data, list):
            return set(data)
    return set()


def save_seen(seen: set[str]) -> None:
    with _seen_lock:
        STATE_FILE.write_text(json.dumps(sorted(seen), indent=2))


def load_excluded() -> set[str]:
    """Return the set of GUIDs explicitly skipped by the user."""
    if EXCLUDED_FILE.exists():
        data = json.loads(EXCLUDED_FILE.read_text())
        if isinstance(data, list):
            return set(data)
    return set()


def save_excluded(excluded: set[str]) -> None:
    EXCLUDED_FILE.write_text(json.dumps(sorted(excluded), indent=2))


# ── Skip control ───────────────────────────────────────────────────────────────

class SkipEpisode(Exception):
    pass


class SkipController:
    """Background thread that watches for 'x' keypress to skip the current episode."""

    def __init__(self):
        self.skip_event = threading.Event()
        self._current_proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._running = True
        self._is_tty = sys.stdin.isatty()
        self._restored = False
        if self._is_tty:
            self._orig = termios.tcgetattr(sys.stdin.fileno())
            # Safety net: restore the terminal even if we exit through an
            # unexpected path (exception, sys.exit, interpreter shutdown
            # before the daemon thread's finally runs).
            atexit.register(self._restore)
            self._thread = threading.Thread(target=self._monitor, daemon=True)
            self._thread.start()

    def _restore(self) -> None:
        """Idempotently restore the original terminal settings."""
        if self._is_tty and not self._restored:
            self._restored = True
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._orig)
            except (termios.error, ValueError, OSError):
                pass

    def set_proc(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._current_proc = proc

    def clear_proc(self) -> None:
        with self._lock:
            self._current_proc = None

    def reset(self) -> None:
        """Clear any previous skip before starting a new episode."""
        self.skip_event.clear()

    def stop(self) -> None:
        """Signal the background thread to exit and wait briefly for cleanup."""
        self._running = False
        if self._is_tty:
            self._thread.join(timeout=0.5)
            self._restore()

    def _monitor(self) -> None:
        fd = sys.stdin.fileno()
        try:
            tty.setcbreak(fd)
            while self._running:
                r, _, _ = select.select([sys.stdin], [], [], 0.1)
                if not r:
                    continue
                ch = sys.stdin.read(1)
                if ch.lower() == "x" and not self.skip_event.is_set():
                    self._ask_confirm()
        finally:
            self._restore()

    def _ask_confirm(self) -> None:
        print("\n  [?] Annullare questo episodio? [y/N] ", end="", flush=True)
        r, _, _ = select.select([sys.stdin], [], [], 15.0)
        if r:
            ch = sys.stdin.read(1)
            print(ch, flush=True)
            if ch.lower() == "y":
                print("  ✗ Episodio annullato — passo al prossimo.", flush=True)
                self.skip_event.set()
                with self._lock:
                    if self._current_proc:
                        self._current_proc.kill()
                return
        print("no — continuo.", flush=True)


# ── Helpers ────────────────────────────────────────────────────────────────────

def slugify(text: str) -> str:
    """Turn a title into a safe, filesystem-friendly directory name."""
    text = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[\s_-]+", "-", text).strip("-")[:80]


def entry_date(entry) -> datetime | None:
    """Parse the publication date of a feed entry (UTC-aware)."""
    if getattr(entry, "published_parsed", None):
        return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
    return None


def audio_url(entry) -> str | None:
    """Return the first audio enclosure URL found in a feed entry."""
    for enc in getattr(entry, "enclosures", []):
        if "audio" in enc.get("type", ""):
            return enc.href
    return None


def stem_from_url(url: str) -> str:
    """Extract the bare filename stem (no extension) from a URL path."""
    filename = Path(urlparse(url).path).name
    return Path(filename).stem or "audio"


def nice_cmd(cmd: list[str]) -> list[str]:
    """Prepend nice on Unix to keep the Mac responsive while working."""
    if sys.platform != "win32":
        return ["nice", "-n", "10"] + cmd
    return cmd


# Pattern for whisper timestamped output: [HH:MM:SS.mmm --> HH:MM:SS.mmm]  text
_SEGMENT_RE = re.compile(r"\[(\d{2}:\d{2}:\d{2}\.\d{3}) --> (\d{2}:\d{2}:\d{2}\.\d{3})\]\s*(.*)")


# ── Episode model ──────────────────────────────────────────────────────────────

@dataclass
class Episode:
    feed_cfg: dict
    feed_title: str
    title: str
    guid: str
    pub: datetime | None
    url: str


# ── Processing steps ───────────────────────────────────────────────────────────

def _run_proc(
    cmd: list[str], skip_ctrl: SkipController | None, **kwargs
) -> subprocess.CompletedProcess:
    """Run a subprocess registered with skip_ctrl so it can be killed on skip."""
    proc = subprocess.Popen(cmd, **kwargs)
    if skip_ctrl is not None:
        skip_ctrl.set_proc(proc)
    try:
        stdout, stderr = proc.communicate()
    finally:
        if skip_ctrl is not None:
            skip_ctrl.clear_proc()
    if skip_ctrl is not None and skip_ctrl.skip_event.is_set():
        raise SkipEpisode()
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def download_mp3(url: str, dest: Path, prefix: str) -> None:
    log(f"{prefix}  ↓ Downloading audio from {url}")
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                f.write(chunk)
    log(f"{prefix}  ✓ Saved to {dest.name} ({dest.stat().st_size // 1024} KB)")


def convert_to_wav(mp3: Path, wav: Path, skip_ctrl: SkipController | None, prefix: str) -> None:
    """Convert MP3 to 16 kHz mono PCM WAV as required by whisper.cpp."""
    log(f"{prefix}  ⚙ Converting to WAV …")
    _run_proc(
        nice_cmd(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(mp3),
                "-ar",
                "16000",
                "-ac",
                "1",
                "-c:a",
                "pcm_s16le",
                str(wav),
            ]
        ),
        skip_ctrl,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    log(f"{prefix}  ✓ WAV ready: {wav.name}")


def _ts_to_sec(ts: str) -> float:
    """'HH:MM:SS.mmm' → seconds as float."""
    h, m, s = ts.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def _parse_segments(output: str) -> list[tuple[float, float, str]]:
    """Return list of (start_sec, end_sec, text) from whisper timestamped output."""
    out = []
    for line in output.splitlines():
        m = _SEGMENT_RE.match(line.strip())
        if m:
            out.append((_ts_to_sec(m.group(1)), _ts_to_sec(m.group(2)), m.group(3).strip()))
    return out


def _is_loop(text: str) -> bool:
    """Return True if a whisper segment looks like a hallucination loop
    (one word dominates more than 40 % of the tokens)."""
    words = [w.lower().strip(".,!?;:\"'") for w in text.split()]
    words = [w for w in words if w]
    if len(words) < 6:
        return False
    top = max(set(words), key=words.count)
    return words.count(top) / len(words) >= 0.4


def _run_whisper(
    wav: Path,
    whisper_bin: Path,
    model: Path,
    whisper_dir: Path,
    skip_ctrl: SkipController | None,
    threads: int,
    prefix: str,
) -> str:
    """Run whisper-cli with timestamps and return raw stdout."""
    result = _run_proc(
        nice_cmd(
            [
                str(whisper_bin),
                "-m",
                str(model),
                "-f",
                str(wav.resolve()),
                "--language",
                "it",
                "-t",
                str(threads),
            ]
        ),
        skip_ctrl,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(whisper_dir),
    )
    return result.stdout


def _cut_wav(
    src: Path, dest: Path, start_sec: float, skip_ctrl: SkipController | None, prefix: str
) -> None:
    """Extract audio from start_sec to end, re-encoding as clean 16 kHz mono WAV."""
    _run_proc(
        nice_cmd(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(src),
                "-ss",
                str(start_sec),
                "-ar",
                "16000",
                "-ac",
                "1",
                "-c:a",
                "pcm_s16le",
                str(dest),
            ]
        ),
        skip_ctrl,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def transcribe(
    wav: Path,
    txt: Path,
    whisper_dir: Path,
    whisper_model: str,
    skip_ctrl: SkipController | None,
    threads: int,
    prefix: str,
) -> None:
    """Transcribe wav to txt, restarting whisper whenever a hallucination loop
    is detected so that the remainder of the audio is still recovered."""
    log(f"{prefix}  🎙 Transcribing with whisper.cpp …")
    whisper_bin = whisper_dir / "build/bin/whisper-cli"
    model = whisper_dir / whisper_model

    collected: list[str] = []
    current_wav = wav
    temp_wavs: list[Path] = []
    abs_offset = 0.0  # seconds into the original audio where current_wav starts
    MAX_PASSES = 8

    try:
        for pass_num in range(MAX_PASSES):
            if pass_num > 0:
                log(
                    f"{prefix}  🔄 Pass {pass_num + 1}: restarting from {abs_offset:.1f}s into audio …"
                )

            raw = _run_whisper(current_wav, whisper_bin, model, whisper_dir, skip_ctrl, threads, prefix)
            segments = _parse_segments(raw)

            if not segments:
                break

            loop_start: float | None = None
            for start, end, text in segments:
                if _is_loop(text):
                    loop_start = start
                    log(
                        f"{prefix}  ⚠ Loop at +{start:.1f}s (abs {abs_offset + start:.1f}s) — skipping ahead"
                    )
                    break
                collected.append(text)

            if loop_start is None:
                break  # clean pass — done

            # Skip 0.5 s past the loop start to avoid re-triggering on the same spot
            skip = loop_start + 0.5
            remaining = segments[-1][1] - skip
            if remaining < 2.0:
                log(f"{prefix}  ℹ Less than 2s remaining after loop — stopping.")
                break

            temp_wav = wav.parent / f"_temp_{pass_num}.wav"
            temp_wavs.append(temp_wav)
            _cut_wav(current_wav, temp_wav, skip, skip_ctrl, prefix)
            abs_offset += skip
            current_wav = temp_wav

    finally:
        for f in temp_wavs:
            f.unlink(missing_ok=True)

    final = "\n".join(t for t in collected if t)
    txt.write_text(final)
    log(f"{prefix}  ✓ Transcript saved: {txt.name} ({len(final)} chars)")


# ── Candidate collection / interactive filtering ───────────────────────────────

def collect_candidates(
    feed_cfg: dict, seen: set[str], excluded: set[str], cutoff: datetime
) -> tuple[str, Path, list[Episode]]:
    rss_url = feed_cfg["url"]
    feed = feedparser.parse(rss_url)

    feed_title = feed.feed.get("title", "Unknown feed")
    output_dir = Path(feed_cfg["output_dir"]).expanduser().resolve()

    candidates = []
    for entry in feed.entries:
        guid = entry.get("id", entry.get("link", ""))
        pub = entry_date(entry)
        if guid in seen or guid in excluded:
            continue
        if pub is not None and pub < cutoff:
            continue
        url = audio_url(entry)
        if not url:
            continue
        title = entry.get("title", "untitled")
        candidates.append(
            Episode(feed_cfg=feed_cfg, feed_title=feed_title, title=title, guid=guid, pub=pub, url=url)
        )

    # Most recent first, then apply optional per-feed max_results cap.
    candidates.sort(
        key=lambda ep: ep.pub if ep.pub is not None else datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    max_results = feed_cfg.get("max_results")
    if max_results is not None:
        candidates = candidates[: int(max_results)]

    return feed_title, output_dir, candidates


def _parse_selection(raw: str, total: int) -> set[int]:
    """Parse user input like '1,3,5-7' or '2 4 6' into a set of 1-based indices."""
    selected: set[int] = set()
    for part in re.split(r"[,\s]+", raw.strip()):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, _, end = part.partition("-")
            try:
                a, b = int(start), int(end)
            except ValueError:
                continue
            if a > b:
                a, b = b, a
            selected.update(range(max(1, a), min(total, b) + 1))
        else:
            try:
                idx = int(part)
                if 1 <= idx <= total:
                    selected.add(idx)
            except ValueError:
                continue
    return selected


def present_and_filter(candidates: list[Episode], excluded: set[str]) -> list[Episode]:
    """Show the candidate list and let the user exclude items."""
    print("\n" + "=" * 60)
    print("EPISODI IN ATTESA DI PROCESSING")
    print("=" * 60)
    for i, ep in enumerate(candidates, 1):
        date_str = ep.pub.strftime("%Y-%m-%d") if ep.pub else "undated"
        print(f"  [{i}] {ep.feed_title} — {ep.title} [{date_str}]")

    print("\nSuggerimento: inserisci i numeri degli episodi da saltare")
    print("  (es. '1,3,5-7' oppure '2 4 6'). Invio per processarli tutti.")
    raw = input("\nEpisodi da escludere: ").strip()

    if not raw:
        return candidates

    skip_set = _parse_selection(raw, len(candidates))
    if not skip_set:
        return candidates

    newly_excluded = 0
    for idx in sorted(skip_set):
        ep = candidates[idx - 1]
        excluded.add(ep.guid)
        newly_excluded += 1
        print(f"  Escluso: {ep.title}")

    save_excluded(excluded)
    log(f"{newly_excluded} episodi esclusi e salvati in {EXCLUDED_FILE.name}")

    return [ep for i, ep in enumerate(candidates, 1) if i not in skip_set]


# ── Single-episode processing ──────────────────────────────────────────────────

def process_episode(
    ep: Episode,
    whisper_dir: Path,
    whisper_model: str,
    threads: int,
    skip_ctrl: SkipController | None,
    worker_id: int,
) -> str | None:
    """Download, convert and transcribe one episode. Returns its GUID on success."""
    prefix = f"[w{worker_id}]" if worker_id >= 0 else ""
    date_str = ep.pub.strftime("%Y-%m-%d") if ep.pub else "undated"
    output_dir = Path(ep.feed_cfg["output_dir"])
    want_txt = ep.feed_cfg.get("output_txt", True)
    want_mp3 = ep.feed_cfg.get("output_mp3", True)
    want_wav = ep.feed_cfg.get("output_wav", False)

    output_dir.mkdir(parents=True, exist_ok=True)

    log(f"{prefix} ── Episode: {ep.title} [{date_str}]")

    episode_dir = output_dir / f"{date_str}-{slugify(ep.title)}"
    episode_dir.mkdir(parents=True, exist_ok=True)

    stem = stem_from_url(ep.url)
    mp3_path = episode_dir / f"{stem}.mp3"
    wav_path = episode_dir / f"{stem}.wav"
    txt_path = episode_dir / f"{stem}.txt"

    if skip_ctrl is not None:
        skip_ctrl.reset()

    try:
        download_mp3(ep.url, mp3_path, prefix)

        if want_txt or want_wav:
            convert_to_wav(mp3_path, wav_path, skip_ctrl, prefix)

        if want_txt:
            transcribe(wav_path, txt_path, whisper_dir, whisper_model, skip_ctrl, threads, prefix)

        if not want_wav and wav_path.exists():
            wav_path.unlink()

        if not want_mp3 and mp3_path.exists():
            mp3_path.unlink()

    except SkipEpisode:
        for f in [mp3_path, wav_path, txt_path]:
            f.unlink(missing_ok=True)
        log(f"{prefix}  ⏭ Saltato → {episode_dir} (cartella vuota)")
        return None

    except subprocess.CalledProcessError as e:
        log(f"{prefix}  ✗ Subprocess error: {e.stderr[:200] if e.stderr else e}")
        return None
    except Exception as e:
        log(f"{prefix}  ✗ Unexpected error: {e}")
        return None

    log(f"{prefix}  ✓ Episode complete → {episode_dir}")
    return ep.guid


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    IMPORT_LOG_FILE.unlink(missing_ok=True)
    OUT_COUNT_FILE.unlink(missing_ok=True)
    log_import("Inizio importazione")

    cfg = load_config()
    whisper_dir = Path(cfg["whisper_dir"])
    whisper_model = cfg["whisper_model"]
    cutoff = _parse_cutoff(cfg["cutoff_date"])
    parallel_workers = int(cfg.get("parallel_workers", 2))
    cpu_count = os.cpu_count() or 4
    whisper_threads = int(cfg.get("whisper_threads") or max(1, cpu_count // parallel_workers))

    seen = load_seen()
    excluded = load_excluded()
    cutoff_date_str = cfg["cutoff_date"]

    feed_summaries: list[tuple[str, Path, list[Episode], dict]] = []
    all_candidates: list[Episode] = []
    for feed_cfg in cfg["feeds"]:
        feed_title, output_dir, candidates = collect_candidates(feed_cfg, seen, excluded, cutoff)
        feed_summaries.append((feed_title, output_dir, candidates, feed_cfg))
        all_candidates.extend(candidates)

    to_process: list[Episode] = []
    if all_candidates:
        to_process = present_and_filter(all_candidates, excluded)

    to_process_by_feed = defaultdict(list)
    for ep in to_process:
        to_process_by_feed[ep.feed_title].append(ep)

    for feed_title, output_dir, candidates, feed_cfg in feed_summaries:
        found = len(candidates)
        n_import = len(to_process_by_feed.get(feed_title, []))
        n_skipped = found - n_import
        max_results = feed_cfg.get("max_results")
        limit_line = max_results if max_results is not None else cutoff_date_str
        print("\n─── [PODCAST] ────────────────────────────────────────")
        print(f"Source: {feed_title}")
        print(f"Output: {output_dir}")
        print(f"Max results or limit date: {limit_line}")
        print("──────────────────────────────────────────────────────")
        print(f"Results: {found} found | {n_import} to import | {n_skipped} skipped")
        log_import(f"{feed_title}: {found} trovati, {n_import} da importare, {n_skipped} saltati")

    total_found = len(all_candidates)
    total_to_process = len(to_process)
    log_import(f"Totale candidati: {total_found}; da processare: {total_to_process}")

    if not all_candidates:
        log("Nessun episodio da processare.")
        OUT_COUNT_FILE.write_text("0")
        log_import("Nessun episodio da processare. Fine importazione.")
        return

    if not to_process:
        log("Nessun episodio selezionato per il processing.")
        OUT_COUNT_FILE.write_text("0")
        log_import("Nessun episodio selezionato. Fine importazione.")
        return

    print(f"\nPronti a processare {len(to_process)} episodi con {parallel_workers} worker")
    print(f"(whisper.cpp userà fino a {whisper_threads} thread per istanza).")
    confirm = input("Procedere? [Y/n] ").strip().lower()

    if confirm not in ("", "y", "s", "si"):
        log("Operazione annullata.")
        OUT_COUNT_FILE.write_text("0")
        log_import("Operazione annullata dall'utente. Fine importazione.")
        return

    log_import(f"Avvio processing di {len(to_process)} episodi con {parallel_workers} worker")

    # Interactive skip is only safe in single-worker mode.
    use_skip = parallel_workers == 1 and sys.stdin.isatty()
    skip_ctrl = SkipController() if use_skip else None
    if use_skip:
        log("Tip: premi 'x' per annullare l'episodio in corso.")

    imported_count = 0
    try:
        if parallel_workers == 1:
            for ep in to_process:
                guid = process_episode(
                    ep, whisper_dir, whisper_model, whisper_threads, skip_ctrl, -1
                )
                if guid:
                    seen.add(guid)
                    save_seen(seen)
                    imported_count += 1
                    log_import(f"Importato: {ep.feed_title} — {ep.title}")
                else:
                    log_import(f"Non importato: {ep.feed_title} — {ep.title}")
        else:
            with ThreadPoolExecutor(max_workers=parallel_workers) as executor:
                futures = {
                    executor.submit(
                        process_episode,
                        ep,
                        whisper_dir,
                        whisper_model,
                        whisper_threads,
                        None,
                        worker_idx % parallel_workers,
                    ): ep
                    for worker_idx, ep in enumerate(to_process)
                }
                for future in as_completed(futures):
                    ep = futures[future]
                    try:
                        guid = future.result()
                        if guid:
                            seen.add(guid)
                            save_seen(seen)
                            imported_count += 1
                            log_import(f"Importato: {ep.feed_title} — {ep.title}")
                        else:
                            log_import(f"Non importato: {ep.feed_title} — {ep.title}")
                    except Exception as e:
                        log(f"Errore processando '{ep.title}': {e}")
                        log_import(f"Errore processando '{ep.title}': {e}")
            log(f"Episodi completati in questo batch: {imported_count}/{len(to_process)}")
    finally:
        OUT_COUNT_FILE.write_text(str(imported_count))
        if skip_ctrl is not None:
            skip_ctrl.stop()

    log_import(f"Importazione completata: {imported_count} nuovi elementi importati")
    log("\n✓ All feeds processed.")


if __name__ == "__main__":
    main()
