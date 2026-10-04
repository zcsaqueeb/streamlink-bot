"""
web/media.py — real subtitle and audio-track support for streamed video.

Why this module exists
──────────────────────
The player used to offer exactly one subtitle track, found by looking for a
sibling `.vtt` in the same batch, and its "Audio" selector was wired to
`HTMLMediaElement.audioTracks` — an API no shipping browser enables by
default (Chrome, Firefox and Safari all leave it unset). So the control was
visible in Chromium-derived builds that expose it, and did nothing anywhere
else. A multi-audio MKV (the common case for anything ripped with, say,
Hindi + English dubs) had no way to switch language at all, and `.srt`/`.ass`
subtitles were detected by MIME type but never rendered, because browsers
only parse WebVTT in a `<track>` element.

This module fixes both by using FFmpeg server-side:

  • Subtitles — every embedded subtitle stream is extracted to WebVTT
    (`ffmpeg -map 0:s:N out.vtt`, which also handles ASS/SSA styling
    conversion), so the player gets a genuine multi-language track list.
  • Audio — each embedded audio stream is remuxed to a browser-playable
    file. The player then switches language by swapping the `src` of a
    hidden `<audio>` element kept in sync with the muted `<video>`, which
    works in every browser instead of depending on `audioTracks`.

Everything degrades gracefully: if FFmpeg isn't installed, `available()`
returns False, no extraction is attempted, and the page falls back to
sidecar subtitle files (still useful, and converted by the pure-Python SRT
converter below, which needs no FFmpeg at all).

Cost control
────────────
Extraction needs the real bytes, so the source file is downloaded from
Telegram to a temp path, probed, and the small outputs kept while the
download is deleted immediately. It runs as a background task after the
user already has their link — never blocking upload — and is skipped for
files over `MEDIA_TRACK_MAX_GB` (default 4) so a huge ISO can't fill a
small container disk.
"""

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Where extracted tracks are cached, and how much of them to keep.
MEDIA_CACHE_DIR = (os.environ.get("MEDIA_CACHE_DIR", "media_cache").strip()
                   or "media_cache")
MEDIA_TRACK_MAX_GB = float(os.environ.get("MEDIA_TRACK_MAX_GB", "4") or 4)
_PROBE_TIMEOUT = 60       # seconds for one ffprobe/ffmpeg invocation
_EXTRACT_TIMEOUT = 15 * 60

_LANG_NAMES = {
    "en": "English", "eng": "English", "hi": "Hindi", "hin": "Hindi",
    "ta": "Tamil", "tam": "Tamil", "te": "Telugu", "tel": "Telugu",
    "ml": "Malayalam", "mal": "Malayalam", "kn": "Kannada", "kan": "Kannada",
    "bn": "Bengali", "ben": "Bengali", "ur": "Urdu", "urd": "Urdu",
    "pa": "Punjabi", "pan": "Punjabi", "mr": "Marathi", "mar": "Marathi",
    "gu": "Gujarati", "guj": "Gujarati", "es": "Spanish", "spa": "Spanish",
    "fr": "French", "fra": "French", "fre": "French", "de": "German",
    "ger": "German", "deu": "German", "it": "Italian", "ita": "Italian",
    "pt": "Portuguese", "por": "Portuguese", "ru": "Russian", "rus": "Russian",
    "ja": "Japanese", "jpn": "Japanese", "ko": "Korean", "kor": "Korean",
    "zh": "Chinese", "chi": "Chinese", "zho": "Chinese", "ar": "Arabic",
    "ara": "Arabic", "tr": "Turkish", "tur": "Turkish", "id": "Indonesian",
    "ind": "Indonesian", "th": "Thai", "tha": "Thai", "vi": "Vietnamese",
    "vie": "Vietnamese", "nl": "Dutch", "dut": "Dutch", "nld": "Dutch",
    "pl": "Polish", "pol": "Polish", "uk": "Ukrainian", "ukr": "Ukrainian",
    "sv": "Swedish", "swe": "Swedish", "fa": "Persian", "per": "Persian",
    "fas": "Persian", "und": "Unknown",
}

_SUB_CODECS_VTT = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt",
                   "text", "dvb_subtitle", "hdmv_pgs_subtitle"}
# Codec → output container for an extracted audio stream. Copying is only
# safe where the browser can actually decode the remuxed result, so each
# entry maps to the transcode/copy choice made in _extract_audio_track.
_AUDIO_TARGET = {
    "aac": ("m4a", "copy"), "mp4a": ("m4a", "copy"),
    "libmp3lame": ("mp3", "copy"), "mp3": ("mp3", "copy"),
    "libopus": ("oga", "copy"), "opus": ("oga", "copy"),
    "vorbis": ("ogg", "copy"), "flac": ("flac", "copy"),
    "pcm_s16le": ("wav", "copy"), "pcm_f32le": ("wav", "copy"),
}


def available() -> bool:
    """True when both ffmpeg and ffprobe are on PATH."""
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def track_dir(file_uid: str) -> Path:
    # file_uid is a hex uuid we generated, but never trust that at a path
    # boundary — a caller that ever learns to build one from user input
    # shouldn't be able to walk out of the cache directory.
    safe = re.sub(r"[^A-Za-z0-9_-]", "", file_uid)
    return Path(MEDIA_CACHE_DIR) / (safe or "unknown")


def language_name(code: str) -> str:
    """Readable name for a bare language code, or "" if it isn't known.

    Used by /sub, where the only language signal is a 2/3-letter token in the
    filename rather than a stream tag FFmpeg can hand us.
    """
    return _LANG_NAMES.get((code or "").strip().lower(), "")


def language_label(stream: dict, index: int, kind: str) -> dict:
    """Build a display label + BCP-47 tag for one stream.

    Prefers the stream's own title (e.g. "Director's commentary"), falls back
    to a readable language name, and finally to a positional label.
    """
    tags = stream.get("tags") or {}
    lang = (tags.get("language") or "").strip().lower()
    title = (tags.get("title") or "").strip()
    name = _LANG_NAMES.get(lang)

    if title:
        label = title if not name or name == title else f"{title} ({name})"
    elif name:
        label = name
    else:
        label = f"{kind} track {index + 1}"

    return {
        "label": label,
        "language": lang if lang and lang != "und" else "",
        "default": bool(tags.get("default") == "1"),
    }


async def _run(cmd: list, timeout: int) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def probe_file(path: str) -> dict:
    """Return {subtitles: [...], audio: [...], duration} for a media file."""
    code, out, err = await _run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_streams", "-show_format", path],
        _PROBE_TIMEOUT,
    )
    if code != 0 or not out.strip():
        raise RuntimeError(f"ffprobe failed ({code}): {err[:200]}")
    data = json.loads(out)

    subtitles, audio = [], []
    sub_i = aud_i = 0
    for s in data.get("streams", []):
        codec = (s.get("codec_name") or "").lower()
        stype = s.get("codec_type")
        if stype == "subtitle":
            label = language_label(s, sub_i, "Subtitle")
            subtitles.append({
                "index": sub_i,
                "stream_ref": s.get("index"),
                "codec": codec,
                # Anything we can't reliably convert is reported as
                # unavailable so the UI doesn't offer a dead track.
                "supported": codec in _SUB_CODECS_VTT,
                **label,
            })
            sub_i += 1
        elif stype == "audio":
            label = language_label(s, aud_i, "Audio")
            target = _AUDIO_TARGET.get(codec)
            audio.append({
                "index": aud_i,
                "stream_ref": s.get("index"),
                "codec": codec,
                "channels": s.get("channels") or 2,
                "ext": target[0] if target else "m4a",
                "mode": target[1] if target else "transcode",
                "supported": True,
                **label,
            })
            aud_i += 1

    fmt = data.get("format") or {}
    try:
        duration = float(fmt.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0

    return {"subtitles": subtitles, "audio": audio, "duration": duration}


async def _extract_subtitle(src: str, stream_ref, out_path: str) -> bool:
    code, _, err = await _run(
        ["ffmpeg", "-y", "-v", "error", "-i", src,
         "-map", f"0:{stream_ref}", "-c:s", "webvtt", out_path],
        _EXTRACT_TIMEOUT,
    )
    if code != 0:
        logger.debug("subtitle extract failed: %s", err[:200])
        return False
    return os.path.exists(out_path) and os.path.getsize(out_path) > 0


async def _extract_audio(src: str, stream_ref, out_path: str, mode: str) -> bool:
    if mode == "copy":
        cmd = ["ffmpeg", "-y", "-v", "error", "-no-padding", "-i", src,
               "-map", f"0:{stream_ref}", "-c:a", "copy", out_path]
    else:
        # Unknown/exotic codec (DTS, TrueHD, AC3…): browsers can't decode it,
        # so transcode to AAC rather than emit a file that won't play.
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", src,
               "-map", f"0:{stream_ref}", "-c:a", "aac", "-b:a", "192k", out_path]
    code, _, err = await _run(cmd, _EXTRACT_TIMEOUT)
    if code != 0:
        logger.debug("audio extract failed: %s", err[:200])
        return False
    return os.path.exists(out_path) and os.path.getsize(out_path) > 0


async def extract_tracks(src_path: str, file_uid: str, meta: dict) -> dict:
    """
    Probe `src_path` and write out every usable subtitle (WebVTT) and extra
    audio track. Returns a `media` dict ready to store on the file record:

        {"duration": float, "subtitles": [{..., "url"}], "audio": [{..., "url"}]}

    Single-audio files get NO audio entries — the muxed stream already plays,
    so extracting a copy would only waste disk. Subtitles are always emitted
    when present.
    """
    info = await probe_file(src_path)
    out_dir = track_dir(file_uid)
    out_dir.mkdir(parents=True, exist_ok=True)

    result: dict = {"duration": info["duration"], "subtitles": [], "audio": []}

    for sub in info["subtitles"]:
        if not sub["supported"]:
            continue
        name = f"sub{sub['index']}.vtt"
        dest = out_dir / name
        try:
            if await _extract_subtitle(src_path, sub["stream_ref"], str(dest)):
                result["subtitles"].append({
                    "index": sub["index"], "label": sub["label"],
                    "language": sub["language"], "default": sub["default"],
                    "kind": "subtitles",
                })
        except Exception as e:
            logger.warning("Subtitle track %s failed for %s: %s", sub["index"], file_uid, e)

    if len(info["audio"]) > 1:
        for aud in info["audio"]:
            name = f"audio{aud['index']}.{aud['ext']}"
            dest = out_dir / name
            try:
                if await _extract_audio(src_path, aud["stream_ref"], str(dest), aud["mode"]):
                    result["audio"].append({
                        "index": aud["index"], "label": aud["label"],
                        "language": aud["language"], "default": aud["default"],
                        "ext": aud["ext"], "channels": aud["channels"],
                    })
            except Exception as e:
                logger.warning("Audio track %s failed for %s: %s", aud["index"], file_uid, e)

    # If nothing usable came out, don't leave an empty directory behind.
    if not result["subtitles"] and not result["audio"]:
        shutil.rmtree(out_dir, ignore_errors=True)
        return {}
    return result


_VTT_CUE_SETTINGS = ("line", "position", "size", "align", "vertical",
                     "region", "snapToLines")


def _vtt_settings(extra: str) -> str:
    """
    Keep only the cue-positioning tokens from an SRT timestamp line that are
    also legal WebVTT settings.

    SRT writers append positioning to the timing line in their own dialect —
    `X1:1 X2:2`, `{y:0.4}` tags, or sometimes a genuine VTT `line:16%`.
    Forwarding the SRT-only forms verbatim produces a timestamp line the
    browser can't parse, which silently drops the ENTIRE cue (not just its
    positioning), so a single carried-over token is enough to lose text.
    """
    if not extra or not extra.strip():
        return ""
    kept = []
    for token in extra.replace("{", " ").replace("}", " ").split():
        key = token.split(":", 1)[0].strip().lower()
        if key in _VTT_CUE_SETTINGS and ":" in token:
            kept.append(token.strip())
    return " ".join(kept)


def srt_to_vtt(srt_text: str) -> str:
    """
    Convert SubRip to WebVTT in pure Python (no FFmpeg needed) — used for
    sidecar .srt uploads, which are tiny and by far the most common manual
    subtitle format.

    Handles the real incompatibilities: VTT requires the `WEBVTT` header;
    timestamps use `.` instead of SRT's `,`; numeric cue identifiers are
    dropped (optional in VTT, and they render as stray on-screen numbers in
    some players); ASS-style override blocks like `{\an8}` are stripped,
    since VTT has no equivalent and browsers print them literally; and SRT
    positioning on the timing line is filtered down to the tokens VTT
    actually accepts (see _vtt_settings).
    """
    text = srt_text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    out = ["WEBVTT", ""]
    ts = re.compile(
        # The optional group between the two timestamps is deliberate: many
        # SRT writers (Aegisub, Subtitle Edit's "Karafel" export) put their
        # positioning *before* the arrow —
        #   00:00:01,000 X1:16 X2:60 Y1:8 Y2:25 --> 00:00:04,000
        # Requiring the arrow straight after the first timestamp made those
        # whole files fail to match, and every cue was dropped as plain text.
        # Whatever sits there is discarded; only post-arrow tokens are
        # candidate VTT settings.
        r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
        r"(?:\s+[^\n]*?)?\s*-->\s*"
        r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})(.*)"
    )
    for block in re.split(r"\n{2,}", text):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        kept, timing = [], False
        for line in lines:
            m = ts.match(line.strip())
            if m:
                h1, m1, s1, ms1, h2, m2, s2, ms2, extra = m.groups()
                # SRT writes milliseconds as exactly 3 digits; a shorter run
                # ("50") is that many MILLISECONDS, not a truncated decimal —
                # read it as ffmpeg's own SRT decoder does.
                ms1 = ms1.rjust(3, "0")[-3:]
                ms2 = ms2.rjust(3, "0")[-3:]
                # Some SRT files write hours as one digit ("1:00:01,000").
                # WebVTT accepts it, but padding to two digits matches what
                # ffmpeg emits, so the same subtitle never renders two ways
                # depending on which converter produced it.
                h1, h2 = h1.zfill(2), h2.zfill(2)
                settings = _vtt_settings(extra)
                out.append(
                    f"{h1}:{m1}:{s1}.{ms1} --> {h2}:{m2}:{s2}.{ms2}"
                    + (f" {settings}" if settings else "")
                )
                timing = True
                continue
            if re.fullmatch(r"\d+", line.strip()) and not timing:
                continue  # SRT cue id
            if line.strip().upper().startswith("WEBVTT"):
                continue
            # ASS override blocks ({\an8}, {\pos(x,y)}, …) mean nothing to
            # VTT and would otherwise be printed on screen verbatim.
            kept.append(re.sub(r"\{\\[^}]*\}", "", line).rstrip())
        if timing:
            out.append("\n".join(kept))
            out.append("")
    return "\n".join(out).strip() + "\n"


# ASS/SSA alignment is a 3x3 numpad grid; WebVTT only expresses horizontal
# alignment plus a line position, so the middle row has no equivalent and is
# left at the default (bottom), which is what both formats do anyway.
_ASS_AN_TOP = {"7": "start", "8": "middle", "9": "end"}
_ASS_AN_BOTTOM = {"1": "start", "2": "middle", "3": "end"}
_ASS_OVERRIDE = re.compile(r"\{\\[^}]*\}")
_ASS_TIME = re.compile(r"(\d+):(\d{2}):(\d{2})\.(\d{1,3})$")

# The field order ASS itself defines per-file via a `Format:` line inside
# `[Events]`. Used as the fallback for a `Dialogue:` line that arrives with no
# Format declared (common in files trimmed by subtitle downloaders).
_ASS_DEFAULT_FIELDS = ["Layer", "Start", "End", "Style", "Name",
                       "MarginL", "MarginR", "MarginV", "Effect", "Text"]


def ass_to_vtt(ass_text: str) -> str:
    r"""
    Convert SubStation Alpha (.ass/.ssa) to WebVTT in pure Python.

    Needed because .ass is a sectioned document, not a list of timestamp
    blocks: run through srt_to_vtt() it yields a valid header with ZERO cues,
    i.e. a subtitle track that silently does nothing. The real differences
    handled here:

      • Timing lives in `Dialogue:` fields, and ASS writes fractions of a
        second (2 digits = centiseconds) where SRT writes milliseconds.
      • `{\...}` override blocks have to go, but `\an` is the one carrying
        placement information, so it's read before being stripped.
      • The field order comes from the file's own `Format:` line — SSA 4.0
        files put `Marked` first, not `Layer`.
      • `{\p1}` switches the text field to vector drawing coordinates, which
        would otherwise be printed on screen as gibberish.
    """
    text = ass_text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    fields = list(_ASS_DEFAULT_FIELDS)
    in_events = False
    out = ["WEBVTT", ""]

    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("["):
            in_events = line.lower() == "[events]"
            continue
        if not in_events:
            continue

        key, _, value = line.partition(":")
        key = key.strip().lower()
        if key == "format":
            names = [f.strip() for f in value.split(",") if f.strip()]
            if len(names) >= 2:
                fields = names
            continue
        if key != "dialogue":
            continue  # Comment:, and everything else in the section

        # maxsplit keeps a comma inside the subtitle text out of the parse.
        parts = [p.strip() for p in value.split(",", len(fields) - 1)]
        if len(parts) < len(fields):
            continue
        rec = dict(zip(fields, parts))

        starts = _ASS_TIME.match(rec.get("Start") or "")
        ends = _ASS_TIME.match(rec.get("End") or "")
        if not (starts and ends):
            continue

        blocks = _ASS_OVERRIDE.findall(rec.get("Text") or "")
        if any(re.search(r"\\p[1-9]", b) for b in blocks):
            continue  # vector-drawing cue, no readable text

        an = next((m.group(1) for b in blocks
                   if (m := re.search(r"\\an([1-9])", b))), "")
        cue = _ASS_OVERRIDE.sub("", rec.get("Text") or "")
        cue = (cue.replace(r"\N", "\n").replace(r"\n", "\n")
                  .replace(r"\h", " ").strip())
        if not cue:
            continue

        settings = []
        if an in _ASS_AN_TOP:
            settings += ["line:10%", f"align:{_ASS_AN_TOP[an]}"]
        elif an in _ASS_AN_BOTTOM and an != "2":
            settings.append(f"align:{_ASS_AN_BOTTOM[an]}")

        def _stamp(m):
            h, mi, s, frac = m.groups()
            # "50" is 50 hundredths of a second, not 50 ms — pad on the right.
            return f"{h.zfill(2)}:{mi}:{s}.{int(frac.ljust(3, '0')):03d}"

        out.append(f"{_stamp(starts)} --> {_stamp(ends)}"
                   + (f" {' '.join(settings)}" if settings else ""))
        out.append(cue)
        out.append("")

    return "\n".join(out).strip() + "\n"


def sidecar_to_vtt(text: str, kind: str) -> str:
    """Convert a sidecar subtitle body to WebVTT, choosing by sub format."""
    if kind == "ass":
        return ass_to_vtt(text)
    return text if kind == "vtt" else srt_to_vtt(text)


def language_from_name(file_name: str) -> str:
    """
    Pull a language code out of a subtitle filename, or "".

    Release-named sidecars carry the language as a token (`Movie.eng.srt`,
    `Movie.[Eng].srt`), which is the only clue a manually attached subtitle
    ever has. Matched against the same table the FFmpeg extractor uses, so a
    name and an embedded stream tag produce the same label in the player.
    """
    stem = os.path.splitext(os.path.basename(file_name or ""))[0]
    for token in re.split(r"[.\[\]_\-() ]+", stem):
        code = token.lower()
        if len(code) in (2, 3) and code in _LANG_NAMES:
            return code
    return ""


def has_sidecar_subtitle(file_name: str) -> "str | None":
    """Return the subtitle kind for a sidecar filename, or None."""
    ext = os.path.splitext(file_name or "")[1].lower()
    return {".vtt": "vtt", ".srt": "srt", ".ass": "ass", ".ssa": "ass"}.get(ext)


def cleanup_dir(file_uid: str) -> None:
    shutil.rmtree(track_dir(file_uid), ignore_errors=True)


async def sweep_orphans(db) -> int:
    """
    Delete cached track directories whose file record no longer exists.

    Tracks are produced at upload time, but files leave the database through
    several paths (the /delete command, "Remove Last" in a batch, admin
    /deleteall, and link expiry) — teaching each one to clean up disk would
    leak gigabytes of stale audio on the first path someone forgets. Running
    against the authoritative record set in one place can't drift.

    Returns the number of directories removed.
    """
    root = Path(MEDIA_CACHE_DIR)
    if not root.is_dir():
        return 0
    removed = 0
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        try:
            exists = await db.get_file(entry.name)
        except Exception:
            continue  # never delete on a lookup error
        if not exists:
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
    if removed:
        logger.info("Track cache sweep removed %d orphaned director(ies)", removed)
    return removed


def tmp_path_for(file_uid: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "", file_uid)
    return os.path.join(tempfile.gettempdir(), f"sl_{safe}")


async def prepare_tracks(client, db, file_uid: str, msg_id: int,
                         file_size: int = 0, file_name: str = "") -> dict:
    """
    Download one stored message, extract its subtitle/audio tracks, and write
    the resulting manifest onto the file record under `media`.

    Returns the manifest ({} when there's nothing to offer). Safe to call as
    a fire-and-forget task: every failure is logged and swallowed, because a
    file with no extractable tracks must still stream and download normally.
    """
    from web.cache import get_cached_message

    max_bytes = int(MEDIA_TRACK_MAX_GB * 1024 ** 3)
    if not available():
        logger.debug("FFmpeg not installed — skipping track extraction for %s", file_uid)
        return {}
    if file_size and file_size > max_bytes:
        logger.info("Skipping track extraction for %s: %.1f GB over the %.1f GB cap",
                    file_uid, file_size / 1024 ** 3, MEDIA_TRACK_MAX_GB)
        return {}

    dest = tmp_path_for(file_uid)
    ext = os.path.splitext(file_name or "")[1] or ".bin"
    path = dest + ext
    try:
        message = await get_cached_message(client, msg_id)
        if not message or message.empty:
            return {}

        # download_media writes to the path we give it; keep the real
        # extension so ffprobe's demuxer picks correctly.
        written = await client.download_media(message, file_name=path)
        if not written or not os.path.exists(path):
            logger.warning("Track extraction: download produced no file for %s", file_uid)
            return {}

        manifest = await extract_tracks(path, file_uid, {})

        if manifest:
            await db.set_file_media(file_uid, manifest)
            logger.info("Extracted tracks for %s: %d subtitle(s), %d audio track(s)",
                        file_uid, len(manifest.get("subtitles", [])),
                        len(manifest.get("audio", [])))
        return manifest
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning("Track extraction failed for %s: %s", file_uid, e)
        return {}
    finally:
        # The source download can be gigabytes and this runs after the user
        # already has their link, so it must never linger on disk — ephemeral
        # container filesystems are small and shared with the track cache.
        with contextlib.suppress(OSError):
            if os.path.exists(path):
                os.remove(path)
