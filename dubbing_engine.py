#!/usr/bin/env python3
"""
Dubbing engine for Dubbing Studio (app.py).

Interface (what app.py calls):
    python dubbing_engine.py --url URL --subtitles PATH_OR_URL --output FILE --voice VOICE

Pipeline:
    1. Download the video (yt-dlp).
    2. Get subtitles: local .srt/.vtt, direct URL, or straight from YouTube
       (pass --subtitles youtube).
    3. Clean captions: strip tags/[Music], remove YouTube's rolling duplicate lines.
    4. Merge cues into whole sentences so speech is not chopped mid-sentence.
    5. Synthesize each sentence with edge-tts at natural speed (cached on disk).
    6. Place clips on a timeline: never overlap, borrow silence from gaps first,
       speed up only as much as needed (capped), and let small overruns drift
       forward and recover during the next pause instead of cutting speech.
    7. Mux with the original video (optionally keeping quiet background audio).

Requires: ffmpeg + ffprobe on PATH, and `pip install yt-dlp edge-tts`.
Only the standard library is used for audio handling.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import json
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.request
import wave
from dataclasses import dataclass
from pathlib import Path

SR = 24000            # working sample rate (mono, 16-bit)
GAP_MS = 120          # minimum silence between consecutive sentences
LAG_LIMIT_MS = 1500   # if speech falls this far behind the video, allow faster catch-up

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def progress(value: float, message: str) -> None:
    print(f"PROGRESS {int(value)} {message}", flush=True)


def fail(message: str) -> None:
    print(f"ERROR: {message}", flush=True)
    sys.exit(1)


# --------------------------------------------------------------------------- #
# Subtitle parsing and cleanup
# --------------------------------------------------------------------------- #
TIME_RE = re.compile(
    r"((?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})\s*-->\s*((?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})"
)
TAG_RE = re.compile(r"<[^>]+>")
BRACKET_RE = re.compile(r"\[[^\]]*\]")          # [Music], [संगीत], [Applause]
SENT_END_RE = re.compile(r"[।.?!…][\"')\]]*$")


@dataclass
class Seg:
    start: int  # ms
    end: int    # ms
    text: str
    rate: str | None = None   # faster edge-tts rate for this sentence, if it needed one


def to_ms(stamp: str) -> int:
    parts = stamp.replace(",", ".").split(":")
    seconds = float(parts[-1])
    minutes = int(parts[-2])
    hours = int(parts[-3]) if len(parts) > 2 else 0
    return int(round(((hours * 60 + minutes) * 60 + seconds) * 1000))


def parse_subs(text: str):
    """Parse SRT or WebVTT into (start_ms, end_ms, [lines])."""
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    cues = []
    for block in re.split(r"\n\s*\n", text):
        lines = block.split("\n")
        idx = next((i for i, l in enumerate(lines) if TIME_RE.search(l)), None)
        if idx is None:
            continue
        m = TIME_RE.search(lines[idx])
        cues.append((to_ms(m.group(1)), to_ms(m.group(2)), lines[idx + 1:]))
    return cues


def clean_line(line: str) -> str:
    line = html.unescape(TAG_RE.sub("", line))
    line = BRACKET_RE.sub(" ", line)
    line = re.sub(r"[♪♫♬]+", " ", line)
    line = re.sub(r"^\s*(>>+|-+)\s*", "", line)
    return re.sub(r"\s+", " ", line).strip()


def dedupe(cues) -> list[Seg]:
    """Remove YouTube's rolling / repeated caption lines.

    Typical pattern: cue 1 = "A", cue 2 = "A / B", cue 3 = "B / C".
    Only lines not already shown in the previous (adjacent) cue are kept.
    """
    out: list[Seg] = []
    prev_lines: list[str] = []
    prev_end = -10**9
    for start, end, raw in cues:
        lines = [c for c in (clean_line(l) for l in raw) if c]
        if not lines:
            continue
        adjacent = start - prev_end <= 300
        fresh = [l for l in lines if l not in prev_lines] if adjacent else lines
        prev_lines, prev_end = lines, end
        if not fresh:
            # Same text repeated (YouTube emits tiny duplicate cues): just extend.
            if out:
                out[-1].end = max(out[-1].end, end)
            continue
        text = " ".join(fresh)
        # Growing line ("hello" -> "hello world"): replace instead of repeating.
        if out and adjacent and text.startswith(out[-1].text):
            out[-1].text = text
            out[-1].end = max(out[-1].end, end)
            continue
        out.append(Seg(start, max(end, start + 1), text))
    return out


def merge_sentences(segs: list[Seg], join_gap=500, max_ms=14000, max_chars=220) -> list[Seg]:
    """Join cues that belong to one sentence so TTS sounds continuous."""
    out: list[Seg] = []
    for s in segs:
        if out:
            p = out[-1]
            if (
                s.start - p.end <= join_gap
                and not SENT_END_RE.search(p.text)
                and s.end - p.start <= max_ms
                and len(p.text) + len(s.text) < max_chars
            ):
                p.text = f"{p.text} {s.text}"
                p.end = max(p.end, s.end)
                continue
        out.append(Seg(s.start, s.end, s.text))
    # Keep strictly ordered starts.
    for a, b in zip(out, out[1:]):
        if b.start < a.start:
            b.start = a.start
    return out


# --------------------------------------------------------------------------- #
# Media helpers
# --------------------------------------------------------------------------- #
SCRIPT_RANGES = [
    ("Devanagari", 0x0900, 0x097F), ("Gujarati", 0x0A80, 0x0AFF),
    ("Gurmukhi", 0x0A00, 0x0A7F), ("Bengali", 0x0980, 0x09FF),
    ("Tamil", 0x0B80, 0x0BFF), ("Telugu", 0x0C00, 0x0C7F),
    ("Kannada", 0x0C80, 0x0CFF), ("Malayalam", 0x0D00, 0x0D7F),
    ("Arabic", 0x0600, 0x06FF),
]
# Which script each voice language can read.
VOICE_SCRIPT = {"hi": "Devanagari", "mr": "Devanagari", "ne": "Devanagari",
                "gu": "Gujarati", "bn": "Bengali", "ta": "Tamil", "te": "Telugu",
                "kn": "Kannada", "ml": "Malayalam", "ur": "Arabic"}
# A voice to use when the captions are in that script.
SCRIPT_VOICE = {"Devanagari": "hi-IN-SwaraNeural (or hi-IN-MadhurNeural)",
                "Gujarati": "gu-IN-DhwaniNeural (or gu-IN-NiranjanNeural)"}


def is_speakable(text: str) -> bool:
    """True only if the text contains a letter or digit, not just punctuation."""
    return any(unicodedata.category(c)[0] in "LN" for c in text)


def dominant_script(segs) -> str:
    counts: dict[str, int] = {}
    for s in segs:
        for ch in s.text:
            if not ch.isalpha():
                continue
            o = ord(ch)
            for name, lo, hi in SCRIPT_RANGES:
                if lo <= o <= hi:
                    counts[name] = counts.get(name, 0) + 1
                    break
            else:
                counts["Latin"] = counts.get("Latin", 0) + 1 if o < 0x0250 else counts.get("Latin", 0)
    return max(counts, key=counts.get) if counts else "none"


def check_voice_matches_text(segs, voice: str) -> None:
    """Stop early if the captions are in an Indic script the selected voice cannot read."""
    lang = voice.split("-")[0].lower()
    expected = VOICE_SCRIPT.get(lang)
    found = dominant_script(segs)
    if not expected or found in ("none", "Latin", expected):
        return
    suggestion = SCRIPT_VOICE.get(found, "a voice for that language")
    fail(
        f"Your subtitles are in {found} script, but the selected voice ({voice}) reads {expected}. "
        f"The speech service returns no audio for that mismatch.\n"
        f"Fix: use {expected} subtitles (translate the file first), or choose {found} as the language "
        f"and use {suggestion}."
    )


def yt_dlp_run(args: list[str], on_line=None):
    """Run yt-dlp; return (exit_code, last_output_lines) without exiting on failure."""
    cmd = [sys.executable, "-m", "yt_dlp", "--no-playlist", "--newline",
           "--retries", "5", "--fragment-retries", "10", "--socket-timeout", "30", *args]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    tail: list[str] = []
    for line in proc.stdout:
        line = line.rstrip()
        tail = (tail + [line])[-8:]
        if on_line:
            on_line(line)
    return proc.wait(), [l for l in tail if l]


def run_yt_dlp(args: list[str], on_line=None) -> None:
    code, tail = yt_dlp_run(args, on_line)
    if code != 0:
        fail("yt-dlp failed: " + " | ".join(tail)[-300:])


VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".mov")

# Extra yt-dlp options tried in order when YouTube answers 403 Forbidden.
DOWNLOAD_ATTEMPTS = [
    [],
    ["--rm-cache-dir", "--extractor-args", "youtube:player-client=default,-android_sdkless"],
    ["-4", "--extractor-args", "youtube:player_client=tv"],
]

FIX_403 = (
    "YouTube refused the video download (HTTP 403).\n"
    "Fix, in this order:\n"
    '1) python -m pip install -U "yt-dlp[default]"\n'
    "2) Install Deno (winget install DenoLand.Deno), then restart the app.\n"
    "3) Or download the video yourself and save it as source.mp4 inside the\n"
    "   '<output name>_work' folder next to your output file. It will be reused."
)


# Written so it does not contain the text "429", because the app retries on that text.
RATE_LIMIT_MSG = (
    "YouTube is limiting subtitle requests right now (too many requests). "
    "Wait 10 to 30 minutes, avoid clicking Check or Download repeatedly, then try again. "
    "Or download the caption from YouTube Studio and choose it with Browse."
)


def wait_for_rate_limit(attempt: int) -> None:
    wait = 15 * 2 ** attempt                      # 15s, 30s, 60s
    progress(1, f"YouTube is limiting requests. Waiting {wait}s before retrying…")
    time.sleep(wait)


def list_subtitles(url: str) -> None:
    """Print one line per available subtitle track: SUB|lang|manual/auto|name."""
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
    info = None
    for attempt in range(4):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            break
        except Exception as exc:
            if "429" in str(exc):
                if attempt < 3:
                    wait_for_rate_limit(attempt)
                    continue
                fail(RATE_LIMIT_MSG)
            fail(f"Could not read the video's subtitle list: {str(exc)[-250:]}")
    rows = []
    for kind, key in (("manual", "subtitles"), ("auto", "automatic_captions")):
        for lang, tracks in (info.get(key) or {}).items():
            if lang == "live_chat":
                continue
            name = (tracks[0].get("name") if tracks else "") or lang
            rows.append((lang, kind, name))
    # first, then manual before auto, then alphabetical.
    rows.sort(key=lambda r: (not r[0].lower().startswith("hi"), r[1] != "manual", r[0]))
    print(f"TITLE|{info.get('title', '')}", flush=True)
    for lang, kind, name in rows:
        print(f"SUB|{lang}|{kind}|{name}", flush=True)
    if not rows:
        print("NOSUBS", flush=True)


def download_subtitle(url: str, lang: str, kind: str, out_dir: Path) -> None:
    """Download one subtitle track as .srt into out_dir and print SAVED|path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = out_dir / f"subtitle_{lang}_{kind}"
    for old in out_dir.glob(f"subtitle_{lang}_{kind}*"):
        old.unlink()
    flag = "--write-subs" if kind == "manual" else "--write-auto-subs"
    args = ["--skip-download", flag, "--sub-langs", lang, "--sub-format", "srt/vtt/best",
            "--convert-subs", "srt", "--sleep-subtitles", "3",
            "-o", str(stem) + ".%(ext)s", url]
    for attempt in range(4):
        code, tail = yt_dlp_run(args)
        found = sorted(out_dir.glob(f"subtitle_{lang}_{kind}*.srt")) or sorted(out_dir.glob(f"subtitle_{lang}_{kind}*.vtt"))
        if found:
            print(f"SAVED|{found[0]}", flush=True)
            return
        if not any("429" in l for l in tail) or attempt == 3:
            break
        wait_for_rate_limit(attempt)
    if any("429" in l for l in tail):
        fail(RATE_LIMIT_MSG)
    fail("YouTube did not return that subtitle track. " + " | ".join(tail)[-200:])


def find_existing_video(work: Path):
    """A finished video already in the work folder (video.* or source.*), not a partial piece."""
    for stem in ("video", "source"):
        for p in sorted(work.glob(f"{stem}.*")):
            if (p.suffix.lower() in VIDEO_EXTS
                    and not re.search(r"\.f\d+\.", p.name)      # unmerged yt-dlp stream
                    and p.stat().st_size > 100_000):
                return p
    return None


def warn_if_old_yt_dlp() -> None:
    try:
        from datetime import date
        from yt_dlp.version import __version__
        y, m, d = (int(x) for x in __version__.split(".")[:3])
        age = (date.today() - date(y, m, d)).days
        if age > 45:
            progress(1, f"yt-dlp is {age} days old. If the download fails, update it.")
    except Exception:
        pass


def download_video(url: str, work: Path, force: bool, local_video: str | None = None) -> Path:
    if local_video:
        p = Path(local_video).expanduser()
        if not p.is_file():
            fail(f"The video file you chose was not found: {p}")
        progress(30, f"Using your video file: {p.name}")
        return p
    existing = None if force else find_existing_video(work)
    if existing:
        progress(30, f"Reusing downloaded video: {existing.name}")
        return existing
    best = {"v": 0.0}

    def on_line(line: str) -> None:
        m = re.search(r"\[download\]\s+(\d+(?:\.\d+)?)%", line)
        if m:
            best["v"] = max(best["v"], float(m.group(1)))
            progress(2 + best["v"] * 0.28, f"Downloading video… {best['v']:.0f}%")

    warn_if_old_yt_dlp()
    progress(2, "Downloading video…")
    tail: list[str] = []
    for i, extra in enumerate(DOWNLOAD_ATTEMPTS):
        if i:
            progress(2, f"YouTube refused the download. Trying another method ({i + 1}/{len(DOWNLOAD_ATTEMPTS)})…")
            best["v"] = 0.0
        code, tail = yt_dlp_run(
            [*extra, "-f", "bv*[height<=1080]+ba/b", "--merge-output-format", "mp4",
             "-o", str(work / "video.%(ext)s"), url],
            on_line,
        )
        done = find_existing_video(work)   # a finished, merged file counts even if yt-dlp complained
        if done:
            return done
        if code == 0:
            break
        if not any("403" in l for l in tail):
            break                           # not a 403: other methods will not help
    if any("403" in l for l in tail):
        fail(FIX_403)
    fail("yt-dlp failed: " + " | ".join(tail)[-300:] if tail else "Video download failed.")


def get_subtitle_text(source: str, url: str, work: Path) -> str:
    src = source.strip()
    if src.lower() in ("", "youtube", "auto") or re.search(r"(youtube\.com|youtu\.be)/", src, re.I):
        progress(32, "Fetching subtitles from YouTube…")
        d = work / "subs"
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
        run_yt_dlp(
            ["--skip-download", "--write-subs", "--write-auto-subs",
             "--sub-langs", "hi,hi-.*", "--sub-format", "vtt/srt/best",
             "-o", str(d / "sub.%(ext)s"), url]
        )
        files = sorted(list(d.glob("*.vtt")) + list(d.glob("*.srt")))
        if not files:
            fail("No subtitles found on YouTube for this video. "
                 "Download the file from YouTube Studio and choose it with Browse.")
        return files[0].read_text(encoding="utf-8", errors="replace")
    if re.match(r"^https?://", src, re.I):
        progress(32, "Downloading subtitle file…")
        req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read().decode("utf-8", errors="replace")
    return Path(src).expanduser().read_text(encoding="utf-8", errors="replace")


def probe_duration_ms(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return float(out) * 1000


def has_audio(path: Path) -> bool:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=codec_type", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    ).stdout.strip()
    return bool(out)


def decode(path: Path, tempo: float = 1.0) -> bytes:
    """Decode a clip to raw mono s16 PCM, trimming leading silence, optional speed-up."""
    filters = ["silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.04"]
    if tempo > 1.001:
        filters.append(f"atempo={tempo:.4f}")
    cmd = ["ffmpeg", "-v", "error", "-i", str(path), "-af", ",".join(filters),
           "-ar", str(SR), "-ac", "1", "-f", "s16le", "-"]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        detail = r.stderr.decode("utf-8", errors="replace").strip()[-250:]
        raise RuntimeError(f"ffmpeg could not decode a speech clip ({path.name}): {detail}")
    return r.stdout


def pcm_ms(pcm: bytes) -> float:
    return len(pcm) / 2 / SR * 1000


# --------------------------------------------------------------------------- #
# Speech synthesis
# --------------------------------------------------------------------------- #
def clip_path(clips: Path, voice: str, rate: str, text: str) -> Path:
    key = hashlib.sha1(f"{voice}|{rate}|{text}".encode("utf-8")).hexdigest()[:20]
    return clips / f"{key}.mp3"


async def synth_all(segs: list[Seg], voice: str, rate: str, clips: Path, concurrency: int) -> list:
    """Generate speech for every sentence. Returns the sentences that could not be spoken."""
    import edge_tts

    sem = asyncio.Semaphore(concurrency)
    done = 0
    failed: list[tuple[Seg, str]] = []

    async def one(seg: Seg) -> None:
        nonlocal done
        path = clip_path(clips, voice, rate, seg.text)
        if not (path.exists() and path.stat().st_size > 0):
            async with sem:
                last = None
                ok = False
                for attempt in range(4):
                    try:
                        await edge_tts.Communicate(seg.text, voice, rate=rate).save(str(path))
                        if path.exists() and path.stat().st_size > 0:
                            ok = True
                            break
                    except Exception as exc:  # network hiccup or "no audio", retry
                        last = exc
                    await asyncio.sleep(1.5 * (attempt + 1))
                if not ok:
                    path.unlink(missing_ok=True)
                    failed.append((seg, str(last) or "no audio returned"))
                    done += 1
                    progress(36 + 39 * done / len(segs), f"Generating speech… {done}/{len(segs)}")
                    return
        done += 1
        progress(36 + 39 * done / len(segs), f"Generating speech… {done}/{len(segs)}")

    await asyncio.gather(*(one(s) for s in segs))
    return failed


# --------------------------------------------------------------------------- #
# Timeline assembly
# --------------------------------------------------------------------------- #
def assemble(segs, voice, rate, clips, max_speed, hard_speed, min_total_ms, wav_path, report):
    track = bytearray()

    def ensure(nbytes: int) -> None:
        if len(track) < nbytes:
            track.extend(bytes(nbytes - len(track)))

    cursor = 0
    speech_end = 0.0
    for i, seg in enumerate(segs):
        cp = clip_path(clips, voice, seg.rate or rate, seg.text)
        if not cp.exists() or cp.stat().st_size == 0:
            cp = clip_path(clips, voice, rate, seg.text)          # fall back to the normal-speed clip
        if not cp.exists() or cp.stat().st_size == 0:
            continue                       # could not be spoken: leave this part silent
        pcm = decode(cp)
        if not pcm:
            continue
        dur = pcm_ms(pcm)
        pos = max(seg.start, cursor)
        lag = pos - seg.start
        nxt = segs[i + 1].start if i + 1 < len(segs) else None

        factor = 1.0
        if nxt is not None:
            avail = nxt - pos - GAP_MS
            if dur > avail:
                cap = hard_speed if lag > LAG_LIMIT_MS else max_speed
                need = dur / avail if avail > 0 else cap
                factor = min(need, cap)
                if factor < 1.03:
                    factor = 1.0
        if factor > 1.0:
            pcm = decode(cp, factor)
            dur = pcm_ms(pcm)

        off = int(pos * SR / 1000) * 2
        ensure(off + len(pcm))
        track[off:off + len(pcm)] = pcm
        cursor = int(pos + dur + GAP_MS)
        speech_end = pos + dur
        report.append({
            "index": i, "start_ms": seg.start, "placed_ms": int(pos),
            "lag_ms": int(lag), "speed": round(factor, 3),
            "duration_ms": int(dur), "text": seg.text,
        })

    ensure(int(min_total_ms * SR / 1000) * 2)
    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(bytes(track))
    return speech_end


def plan_natural_speed(segs, voice, rate, clips, video_ms, concurrency) -> None:
    """Re-speak sentences that are too long for their slot at a faster natural voice rate.

    A faster voice rate sounds better than stretching audio, so this runs before assemble().
    Small leftover overruns are still handled by the speed-up inside assemble().
    """
    total_normal = 0.0
    groups: dict[str, list[Seg]] = {}
    for i, seg in enumerate(segs):
        cp = clip_path(clips, voice, rate, seg.text)
        if not cp.exists() or cp.stat().st_size == 0:
            continue
        dur = pcm_ms(decode(cp))
        total_normal += dur
        nxt = segs[i + 1].start if i + 1 < len(segs) else video_ms
        slot = nxt - seg.start - GAP_MS
        need = dur / slot if slot > 0 else 1.5
        if need > 1.05:
            pct = min(45, int((min(need, 1.6) - 1) * 100) + 5)   # small margin, capped at +45%
            seg.rate = f"{pct:+d}%"
            groups.setdefault(seg.rate, []).append(seg)
    count = sum(len(g) for g in groups.values())
    print(f"Speech at normal speed: {total_normal / 1000:.0f}s | video: {video_ms / 1000:.0f}s | "
          f"re-speaking {count} sentence(s) faster", flush=True)
    for r, group in groups.items():
        asyncio.run(synth_all(group, voice, r, clips, concurrency))


def mux(video: Path, wav: Path, out: Path, video_ms: float, speech_end_ms: float, bg_volume: float) -> None:
    overrun = speech_end_ms - video_ms
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(video), "-i", str(wav)]
    if bg_volume > 0 and has_audio(video):
        cmd += ["-filter_complex",
                f"[0:a]volume={bg_volume}[bg];[bg][1:a]amix=inputs=2:duration=longest:normalize=0[a]",
                "-map", "0:v", "-map", "[a]"]
    else:
        cmd += ["-map", "0:v", "-map", "1:a"]
    # The output always keeps the original video length. Speech past the end is cut (see main).
    if video.suffix.lower() == ".mp4":
        cmd += ["-c:v", "copy"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        fail("ffmpeg mux failed: " + r.stderr.strip()[-300:])


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description="dubbing engine")
    ap.add_argument("--url", required=True, help="YouTube video URL")
    ap.add_argument("--subtitles", default="youtube",
                    help="Local .srt/.vtt path, direct URL, or 'youtube' to fetch from the video")
    ap.add_argument("--output", help="Output .mp4 path (required to dub)")
    ap.add_argument("--list-subs", action="store_true", help="List the video's subtitle tracks and exit")
    ap.add_argument("--get-sub", metavar="LANG", help="Download this subtitle language as .srt and exit")
    ap.add_argument("--sub-kind", choices=["manual", "auto"], default="manual")
    ap.add_argument("--sub-dir", help="Folder for --get-sub downloads")
    ap.add_argument("--video", help="Use this local video file instead of downloading")
    ap.add_argument("--voice", default="hi-IN-SwaraNeural")
    ap.add_argument("--rate", default="+0%", help="Base edge-tts rate, e.g. -5%% for slightly slower")
    ap.add_argument("--max-speed", type=float, default=1.25, help="Normal speed-up cap (default 1.25)")
    ap.add_argument("--hard-speed", type=float, default=1.45, help="Cap when badly behind (default 1.45)")
    ap.add_argument("--bg-volume", type=float, default=0.0,
                    help="Keep original audio at this volume (0 = replace, 0.15 = quiet background)")
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--force", action="store_true", help="Re-download the video")
    args = ap.parse_args()

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            fail(f"{tool} was not found on PATH. Install ffmpeg and restart the app.")
    try:
        import edge_tts  # noqa: F401
        import yt_dlp    # noqa: F401
    except ImportError as exc:
        fail(f"Missing Python package ({exc.name}). Run: pip install yt-dlp edge-tts")

    if args.list_subs:
        list_subtitles(args.url)
        return
    if args.get_sub:
        download_subtitle(args.url, args.get_sub, args.sub_kind,
                          Path(args.sub_dir or Path.home() / "Videos" / "subtitles").expanduser())
        return
    if not args.output:
        fail("--output is required.")

    out = Path(args.output).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    work = Path(args.workdir) if args.workdir else out.parent / f"{out.stem}_work"
    clips = work / "clips"
    clips.mkdir(parents=True, exist_ok=True)

    # Subtitles first: a bad subtitle file fails in seconds, before the video download.
    raw = parse_subs(get_subtitle_text(args.subtitles, args.url, work))
    if not raw:
        fail("The subtitle file contained no timed cues. Use a valid .srt or .vtt file.")
    segs = merge_sentences(dedupe(raw))
    segs = [s for s in segs if is_speakable(s.text)]   # drop punctuation-only lines like "." or "?"
    if not segs:
        fail("No speakable text found in the subtitles.")
    check_voice_matches_text(segs, args.voice)

    video = download_video(args.url, work, args.force, args.video)
    video_ms = probe_duration_ms(video)
    progress(35, f"Prepared {len(segs)} sentences from {len(raw)} caption cues.")

    failed = asyncio.run(synth_all(segs, args.voice, args.rate, clips, max(1, args.concurrency)))
    if failed:
        first_text = failed[0][0].text[:80]
        if len(failed) > 0.2 * len(segs):
            fail(f"{len(failed)} of {len(segs)} sentences could not be spoken. "
                 f"The voice returned no audio ({failed[0][1][:120]}). "
                 f"Check that the subtitle language matches the voice. First sentence: {first_text}")
        print(f"WARN: {len(failed)} sentence(s) could not be spoken and stay silent. First: {first_text}",
              flush=True)

    plan_natural_speed(segs, args.voice, args.rate, clips, video_ms, max(1, args.concurrency))
    progress(76, "Aligning speech to the video timeline…")
    report: list[dict] = []
    wav = work / "hindi.wav"
    speech_end = assemble(segs, args.voice, args.rate, clips, args.max_speed,
                          args.hard_speed, video_ms, wav, report)
    if speech_end > video_ms + 250:
        print(f"WARN: speech runs {(speech_end - video_ms) / 1000:.1f}s past the end of the video. "
              "That part is cut so the video keeps its original length. "
              "Shorten the subtitle wording for the longest sentences.", flush=True)
    (work / "timing_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    progress(88, "Building the final video…")
    mux(video, wav, out, video_ms, speech_end, args.bg_volume)

    sped = sum(1 for r in report if r["speed"] > 1.0)
    worst = max((r["lag_ms"] for r in report), default=0)
    print(f"Sentences: {len(report)} | sped up: {sped} | max drift: {worst} ms "
          f"| report: {work / 'timing_report.json'}", flush=True)
    progress(100, "Finished.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as exc:  # make sure the app always gets a readable reason
        import traceback
        traceback.print_exc()
        fail(f"{type(exc).__name__}: {exc}")