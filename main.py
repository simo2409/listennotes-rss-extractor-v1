"""
RSS podcast extractor: fetches new episodes, downloads audio,
converts to WAV, and transcribes with whisper.cpp.

Press 'x' during processing to skip the current episode.
"""

import json
import re
import select
import subprocess
import sys
import termios
import threading
import tty
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import requests

# ── Configuration ──────────────────────────────────────────────────────────────

CONFIG_FILE = Path(__file__).parent / "config.json"
STATE_FILE = Path(__file__).parent / "seen_items.json"


def load_config() -> dict:
    return json.loads(CONFIG_FILE.read_text())


def _parse_cutoff(date_str: str) -> datetime:
    y, m, d = (int(x) for x in date_str.split("-"))
    return datetime(y, m, d, tzinfo=timezone.utc)

# ── Logging ────────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ── State persistence ──────────────────────────────────────────────────────────

def load_seen() -> set[str]:
    """Return the set of GUIDs that have already been processed."""
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()


def save_seen(seen: set[str]) -> None:
    STATE_FILE.write_text(json.dumps(sorted(seen), indent=2))


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
        if self._is_tty:
            self._orig = termios.tcgetattr(sys.stdin.fileno())
            self._thread = threading.Thread(target=self._monitor, daemon=True)
            self._thread.start()

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
            termios.tcsetattr(fd, termios.TCSADRAIN, self._orig)

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


# Pattern for whisper timestamped output: [HH:MM:SS.mmm --> HH:MM:SS.mmm]  text
_SEGMENT_RE = re.compile(r'\[(\d{2}:\d{2}:\d{2}\.\d{3}) --> (\d{2}:\d{2}:\d{2}\.\d{3})\]\s*(.*)')

# ── Processing steps ───────────────────────────────────────────────────────────

def _run_proc(cmd: list[str], skip_ctrl: SkipController, **kwargs) -> subprocess.CompletedProcess:
    """Run a subprocess registered with skip_ctrl so it can be killed on skip."""
    proc = subprocess.Popen(cmd, **kwargs)
    skip_ctrl.set_proc(proc)
    try:
        stdout, stderr = proc.communicate()
    finally:
        skip_ctrl.clear_proc()
    if skip_ctrl.skip_event.is_set():
        raise SkipEpisode()
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def download_mp3(url: str, dest: Path, skip_ctrl: SkipController) -> None:
    log(f"  ↓ Downloading audio from {url}")
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                if skip_ctrl.skip_event.is_set():
                    raise SkipEpisode()
                f.write(chunk)
    log(f"  ✓ Saved to {dest.name} ({dest.stat().st_size // 1024} KB)")


def convert_to_wav(mp3: Path, wav: Path, skip_ctrl: SkipController) -> None:
    """Convert MP3 to 16 kHz mono PCM WAV as required by whisper.cpp."""
    log("  ⚙ Converting to WAV …")
    _run_proc(
        ["ffmpeg", "-y", "-i", str(mp3), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)],
        skip_ctrl,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    log(f"  ✓ WAV ready: {wav.name}")


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


def _run_whisper(wav: Path, whisper_bin: Path, model: Path, whisper_dir: Path, skip_ctrl: SkipController) -> str:
    """Run whisper-cli with timestamps and return raw stdout."""
    result = _run_proc(
        [str(whisper_bin), "-m", str(model), "-f", str(wav.resolve()), "--language", "it"],
        skip_ctrl,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(whisper_dir),
    )
    return result.stdout


def _cut_wav(src: Path, dest: Path, start_sec: float, skip_ctrl: SkipController) -> None:
    """Extract audio from start_sec to end, re-encoding as clean 16 kHz mono WAV."""
    _run_proc(
        ["ffmpeg", "-y", "-i", str(src), "-ss", str(start_sec),
         "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(dest)],
        skip_ctrl,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def transcribe(wav: Path, txt: Path, whisper_dir: Path, whisper_model: str, skip_ctrl: SkipController) -> None:
    """Transcribe wav to txt, restarting whisper whenever a hallucination loop
    is detected so that the remainder of the audio is still recovered."""
    log("  🎙 Transcribing with whisper.cpp …")
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
                log(f"  🔄 Pass {pass_num + 1}: restarting from {abs_offset:.1f}s into audio …")

            raw = _run_whisper(current_wav, whisper_bin, model, whisper_dir, skip_ctrl)
            segments = _parse_segments(raw)

            if not segments:
                break

            loop_start: float | None = None
            for start, end, text in segments:
                if _is_loop(text):
                    loop_start = start
                    log(f"  ⚠ Loop at +{start:.1f}s (abs {abs_offset + start:.1f}s) — skipping ahead")
                    break
                collected.append(text)

            if loop_start is None:
                break  # clean pass — done

            # Skip 0.5 s past the loop start to avoid re-triggering on the same spot
            skip = loop_start + 0.5
            remaining = segments[-1][1] - skip
            if remaining < 2.0:
                log("  ℹ Less than 2s remaining after loop — stopping.")
                break

            temp_wav = wav.parent / f"_temp_{pass_num}.wav"
            temp_wavs.append(temp_wav)
            _cut_wav(current_wav, temp_wav, skip, skip_ctrl)
            abs_offset += skip
            current_wav = temp_wav

    finally:
        for f in temp_wavs:
            f.unlink(missing_ok=True)

    final = "\n".join(t for t in collected if t)
    txt.write_text(final)
    log(f"  ✓ Transcript saved: {txt.name} ({len(final)} chars)")


# ── Main ───────────────────────────────────────────────────────────────────────

def process_feed(feed_cfg: dict, seen: set[str], whisper_dir: Path, whisper_model: str, cutoff: datetime, skip_ctrl: SkipController) -> None:
    rss_url = feed_cfg["url"]
    output_dir = Path(feed_cfg["output_dir"])
    want_txt = feed_cfg.get("output_txt", True)
    want_mp3 = feed_cfg.get("output_mp3", True)
    want_wav = feed_cfg.get("output_wav", False)

    output_dir.mkdir(parents=True, exist_ok=True)

    log(f"Fetching feed: {rss_url}")
    feed = feedparser.parse(rss_url)

    if feed.bozo:
        log(f"Warning: feed parser issue — {feed.bozo_exception}")

    feed_title = feed.feed.get("title", "Unknown feed")
    log(f"Feed: {feed_title}  ({len(feed.entries)} total entries)")

    to_process = [
        e for e in feed.entries
        if e.get("id", e.get("link", "")) not in seen
        and (entry_date(e) is None or entry_date(e) >= cutoff)
    ]

    log(f"New episodes to process: {len(to_process)}")

    if not to_process:
        log("Nothing to do for this feed.")
        return

    for entry in to_process:
        title = entry.get("title", "untitled")
        guid = entry.get("id", entry.get("link", ""))
        pub = entry_date(entry)
        url = audio_url(entry)

        date_str = pub.strftime("%Y-%m-%d") if pub else "undated"
        log(f"\n── Episode: {title} [{date_str}]")

        if not url:
            log("  ✗ No audio enclosure found — skipping.")
            seen.add(guid)
            save_seen(seen)
            continue

        episode_dir = output_dir / f"{date_str}-{slugify(title)}"
        episode_dir.mkdir(parents=True, exist_ok=True)

        stem = stem_from_url(url)
        mp3_path = episode_dir / f"{stem}.mp3"
        wav_path = episode_dir / f"{stem}.wav"
        txt_path = episode_dir / f"{stem}.txt"

        skip_ctrl.reset()

        try:
            download_mp3(url, mp3_path, skip_ctrl)

            if want_txt or want_wav:
                convert_to_wav(mp3_path, wav_path, skip_ctrl)

            if want_txt:
                transcribe(wav_path, txt_path, whisper_dir, whisper_model, skip_ctrl)

            if not want_wav and wav_path.exists():
                wav_path.unlink()

            if not want_mp3 and mp3_path.exists():
                mp3_path.unlink()

        except SkipEpisode:
            for f in [mp3_path, wav_path, txt_path]:
                f.unlink(missing_ok=True)
            seen.add(guid)
            save_seen(seen)
            log(f"  ⏭ Saltato → {episode_dir} (cartella vuota, marcato come visto)")
            continue

        except subprocess.CalledProcessError as e:
            log(f"  ✗ Subprocess error: {e.stderr[:200] if e.stderr else e}")
            continue
        except Exception as e:
            log(f"  ✗ Unexpected error: {e}")
            continue

        seen.add(guid)
        save_seen(seen)
        log(f"  ✓ Episode complete → {episode_dir}")


def main() -> None:
    cfg = load_config()
    whisper_dir = Path(cfg["whisper_dir"])
    whisper_model = cfg["whisper_model"]
    cutoff = _parse_cutoff(cfg["cutoff_date"])
    seen = load_seen()

    skip_ctrl = SkipController()
    if skip_ctrl._is_tty:
        log("Tip: premi 'x' per annullare l'episodio in corso.")

    try:
        for feed_cfg in cfg["feeds"]:
            process_feed(feed_cfg, seen, whisper_dir, whisper_model, cutoff, skip_ctrl)
    finally:
        skip_ctrl.stop()

    log("\n✓ All feeds processed.")


if __name__ == "__main__":
    main()
