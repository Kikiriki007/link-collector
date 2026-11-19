r"""
collect.py - Telegram link -> download -> transcribe collector.

Send a video link (Reel/Short/TikTok/YouTube/etc.) to your Telegram bot. Running this script
(manually or via the daily scheduled task) pulls new messages, downloads each link's video with
yt-dlp, transcribes it with Whisper, and files the pair into a readable folder under
`output_root\<date>\<title>__<transcript-snippet>\`. A voice note or audio file (.m4a, .mp3, ...)
sent directly, no link at all, gets transcribed the same way - downloaded straight from Telegram
instead of yt-dlp. Set `"whisper_language"` in config.json to force a language (e.g. `"sr"`) for
that path instead of Whisper's auto-detect, useful for consistently-non-English or code-switched
audio; it only applies to direct audio messages, not video links.

Message rule: send *only* the bare link -> the video is kept. Add any other text to the message ->
that text becomes a PERSONAL NOTE (saved in that clip's source.txt and echoed into log.txt) *and*
signals "delete the video after transcribing" (transcript + note stay either way). Send just the
letter "c" -> cancels every link sent *before* it (nothing downloaded, nothing re-queued next run
either) - a remote way to retract a just-sent link without console access. Links sent after the "c"
are unaffected, even if they all arrive in the same batch (e.g. after a skipped/delayed run).

Usage:
    python collect.py

Requires config.json next to this file (copy config.example.json and fill in your bot token).
See README.md for full setup.
"""
import atexit
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests
import torch
import whisper
import yt_dlp
from whisper.utils import get_writer

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
LOG_PATH = BASE_DIR / "log.txt"
LOCK_PATH = BASE_DIR / "collect.lock"

URL_RE = re.compile(r"https?://\S+")
SAVE_RE = re.compile(r"\bsave\b", re.I)
PRIVATE_KEYWORDS = ("login", "log in", "private", "restrict", "permission", "not available",
                    "n't available", "sign in", "certain audiences", "age-restricted")
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".flv", ".ts"}
AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".ogg", ".oga", ".opus", ".flac", ".aac", ".wma"}

# Words that, when the video's own narrator says them, are a strong hint that something on
# screen right then matters - Whisper only catches speech, so this is the cheap way to flag
# "go look at the video here" moments without doing actual frame analysis (yet).
DEFAULT_FLAG_KEYWORDS = ["screenshot", "screenshots", "screengrab", "screengrabs",
                          "screen grab", "screen grabs", "screen shot", "screen shots"]


# --- locking -------------------------------------------------------------------
def acquire_lock():
    """Grabs an OS-level exclusive lock on LOCK_PATH, held for as long as the returned file object
    stays open - which, as a local variable in main(), is exactly this process's lifetime. The OS
    releases the lock automatically on exit (even a crash or kill), so there's no stale-lock state
    to detect or clean up by hand, unlike a plain PID-in-a-file scheme. Returns None if another
    instance already holds it - a manual run and the scheduled task can otherwise genuinely overlap
    (e.g. the task catching up on a missed trigger right as someone runs it by hand), each racing
    to download into the same deterministic _staging_<update_id> folder at once."""
    f = None
    try:
        f = open(LOCK_PATH, "w+")
        f.write(f"{os.getpid()}\n")
        f.flush()
        f.seek(0)
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except OSError:
        # Windows can already deny the plain write/flush above with a sharing violation while
        # another process holds the file open - not just the explicit lock call below - so this
        # has to wrap the whole sequence, not just msvcrt.locking/fcntl.flock. The close() below
        # can raise the same sharing violation again (it flushes whatever didn't make it out
        # above), which isn't a new problem worth surfacing - we already know we don't have the
        # lock, that's all this function promises to report.
        if f is not None:
            try:
                f.close()
            except OSError:
                pass
        return None


# --- config -------------------------------------------------------------------
def load_config() -> dict:
    if not CONFIG_PATH.exists():
        print("config.json not found. Copy config.example.json to config.json and fill in your "
              "bot token (see README.md).", file=sys.stderr)
        raise SystemExit(1)
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def advance_last_update_id(cfg: dict, update_id: int) -> None:
    """Bumps and saves last_update_id, never letting it regress. Jobs are collected in one pass
    but downloaded/transcribed in later, separate passes (possibly out of update_id order relative
    to skipped/cancelled/edited messages resolved immediately in the first pass) - a plain
    assignment here could otherwise move last_update_id backwards and cause already-handled
    messages to be re-fetched and re-logged (though not re-downloaded) on the next run."""
    cfg["last_update_id"] = max(cfg.get("last_update_id", 0), update_id)
    save_config(cfg)


# --- telegram -------------------------------------------------------------------
def tg_get_updates(token: str, offset: int) -> list:
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                      params={"offset": offset}, timeout=30)
    r.raise_for_status()
    return r.json()["result"]


TELEGRAM_RETENTION = dt.timedelta(hours=24)       # Bot API deletes unfetched updates after this
TELEGRAM_ID_RESET_AFTER = dt.timedelta(days=7)    # after a week with no updates, update_id restarts randomly


def check_dropped_updates(prev_fetch_at, fetch_at: dt.datetime, expected_first_id: int,
                          updates: list):
    """Returns a warning string if Telegram likely dropped messages before this fetch, else None.

    Telegram keeps a bot's unfetched updates for at most 24h, then deletes them silently - no
    error, they just never show up in getUpdates. Hit for real: the PC was off ~34h, 4 of 5 queued
    links were gone, and the run reported a clean "1 ok" with nothing hinting anything was lost.
    update_ids are sequential, so a gap between last_update_id+1 and the first update actually
    returned is a missing update (edits/link-preview updates count too, so it's an upper bound on
    links). With no updates returned, or after a week (ids restart randomly), the count is
    unknowable - then it's a conditional "if you sent anything in this window" warning instead."""
    if not prev_fetch_at:
        return None  # first run with this check - no previous fetch time to compare against
    try:
        prev = dt.datetime.fromisoformat(prev_fetch_at)
    except ValueError:
        return None
    fmt = "%Y-%m-%d %H:%M"
    elapsed = fetch_at - prev
    gap = updates[0]["update_id"] - expected_first_id if updates else None
    if elapsed <= TELEGRAM_RETENTION:
        if gap and gap > 0:
            return (f"{gap} update(s) went missing since the last check ({prev:{fmt}}) even though "
                    f"less than 24h passed - something else may be calling getUpdates on this bot. "
                    f"Resend any links that never got a result.")
        return None
    if gap == 0:
        return None  # first waiting update is exactly the next expected one - nothing was lost
    cutoff = fetch_at - TELEGRAM_RETENTION
    window = f"between {prev:{fmt}} and {cutoff:{fmt}}"
    hours = f"{elapsed.total_seconds() / 3600:.0f}h"
    if gap and gap > 0 and elapsed < TELEGRAM_ID_RESET_AFTER:
        return (f"Last check was {hours} ago and Telegram deletes unread bot messages after 24h - "
                f"{gap} update(s) were lost (link previews/edits count too, so likely fewer links). "
                f"Resend anything you sent {window}.")
    return (f"Last check was {hours} ago and Telegram deletes unread bot messages after 24h - "
            f"if you sent anything {window}, it was lost. Resend those links.")


def tg_send_message(token: str, chat_id, text: str) -> None:
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                           data={"chat_id": chat_id, "text": text}, timeout=30)
        r.raise_for_status()  # Telegram rejects a >4096-char message (e.g. a very verbose failure
        # reason) with a 400, not a connection-level error - without this, that response was
        # silently ignored: the run finished and logged normally, but no summary ever arrived.
    except requests.RequestException as e:
        print(f"  (warning: failed to send Telegram summary: {e})", flush=True)


LOCAL_BOT_API_CONTAINER_DIR = "/var/lib/telegram-bot-api"
LOCAL_BOT_API_CONTAINER_NAME = "link-collector-bot-api"  # matches docker-compose.yml
DOCKER_DESKTOP_EXE = r"C:\Program Files\Docker\Docker\Docker Desktop.exe"


def _docker_daemon_ready() -> bool:
    try:
        r = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def _minimize_docker_desktop_window(attempts: int = 20, delay: float = 0.5) -> None:
    """Docker Desktop is Electron-based and creates its own window regardless of the launching
    process's requested window style (confirmed by hand: Start-Process -WindowStyle Hidden still
    produced a fully visible, focus-stealing window) - the only way to keep it off-screen is to
    find the actual window after the fact and minimize it directly via the Win32 API, the same
    technique a published PowerShell module (GenXdev) uses for this exact problem. Windows-only
    and purely cosmetic: never raises, never blocks startup - if the window can't be found in time
    (or this isn't Windows), the daemon-readiness wait that follows just proceeds with the window
    left alone. Confirmed by hand: IsIconic true (genuinely minimized, not just a no-op call) after
    finding a real launched window. Worth the trouble because a popup stealing focus is genuinely
    disruptive mid-game, not just mid-work - it still flashes on screen for a moment first (the
    window takes a few seconds to even exist), just not indefinitely."""
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    def get_process_name(pid):
        h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(260)
            size = wintypes.DWORD(260)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return buf.value
            return ""
        finally:
            kernel32.CloseHandle(h)

    for _ in range(attempts):
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def callback(hwnd, lparam):
            if not user32.IsWindowVisible(hwnd) or user32.GetWindowTextLengthW(hwnd) == 0:
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if get_process_name(pid.value).lower().endswith("docker desktop.exe"):
                found.append(hwnd)
            return True

        user32.EnumWindows(callback, 0)
        if found:
            for hwnd in found:
                user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
            return
        time.sleep(delay)


def ensure_docker_desktop_running(cfg: dict, attempts: int = 60, delay: float = 2.0) -> None:
    """Launches Docker Desktop if its daemon isn't already reachable, and waits for it to come up.
    This machine doesn't have Docker Desktop's own "start at login" setting enabled, and even one
    that does can still lose the race: a fully unattended run (PC off overnight, turned on once
    the next morning and left alone) can have the scheduled task's missed-trigger catch-up fire
    while Docker Desktop is still mid-launch (30-60+ seconds cold). Without this, that scenario
    fails at the very first step every time - confirmed by hand that the daemon genuinely isn't
    reachable immediately after login with autostart off."""
    if _docker_daemon_ready():
        return
    exe = cfg.get("docker_desktop_exe") or DOCKER_DESKTOP_EXE
    if not Path(exe).exists():
        raise RuntimeError(f'Docker daemon isn\'t running and Docker Desktop wasn\'t found at '
                           f'"{exe}" to launch it - set "docker_desktop_exe" in config.json if '
                           f"it's installed somewhere else.")
    subprocess.Popen([exe])
    _minimize_docker_desktop_window()
    for attempt in range(attempts):
        if _docker_daemon_ready():
            return
        time.sleep(delay)
    raise RuntimeError(f"launched Docker Desktop but its daemon never became reachable after "
                       f"{attempts * delay:.0f}s")


def start_local_bot_api(container: str, base_url: str, cfg: dict,
                        attempts: int = 10, delay: float = 1.0) -> None:
    """Starts the local Bot API container for this run - idempotent (docker start on an
    already-running container is a harmless no-op), so it's safe to call unconditionally rather
    than checking state first. Raises with Docker's own error text (not just a generic non-zero
    exit) if the start itself fails. `docker start` returns as soon as the container process
    launches, well before the server inside is actually ready to accept connections - confirmed by
    hand, the very first real request right after a start failed every time with "Remote end
    closed connection" - so this polls the port afterward instead of assuming it's ready; any HTTP
    response at all (even a 404) means it's actually listening. Every Telegram call this run makes
    depends on this container being reachable, so every failure mode here raises rather than
    limping into a run full of connection errors."""
    ensure_docker_desktop_running(cfg)
    try:
        subprocess.run(["docker", "start", container], check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(e.stderr.strip() or str(e)) from e
    except FileNotFoundError as e:
        raise RuntimeError("docker command not found - is Docker Desktop installed?") from e

    for attempt in range(attempts):
        try:
            requests.get(base_url, timeout=3)
            return
        except requests.RequestException:
            if attempt == attempts - 1:
                raise RuntimeError(f"container started but never became reachable at {base_url}")
            time.sleep(delay)


def stop_local_bot_api(container: str) -> None:
    """Stops the local Bot API container - registered via atexit so it runs whether this run
    finishes normally, fails, or hits Ctrl+C (not a hard kill/crash - same caveat as the lock
    file). Best-effort: a failure here shouldn't mask whatever this run actually did, just warn."""
    try:
        subprocess.run(["docker", "stop", container], check=True, capture_output=True, text=True)
    except Exception as e:
        print(f"  (warning: failed to stop local bot API container: {e})", flush=True)


def stop_docker_desktop() -> None:
    """Quits Docker Desktop entirely, not just the bot-api container - registered via atexit
    *before* stop_local_bot_api so it fires after it (atexit runs LIFO): the container gets a
    clean `docker stop` first, then the whole app goes down, rather than yanking the backend out
    from under a running container. Keeps this fully off-footprint between batches rather than
    leaving Docker Desktop (and the WSL2 VM backing it) idling in the background all day for the
    sake of one scheduled run. Best-effort, same reasoning as stop_local_bot_api."""
    try:
        subprocess.run(["taskkill", "/IM", "Docker Desktop.exe", "/F"],
                       capture_output=True, text=True)
    except Exception as e:
        print(f"  (warning: failed to stop Docker Desktop: {e})", flush=True)


def _tg_getfile_error(r: requests.Response) -> str:
    # requests' own raise_for_status() only gives "400 Client Error: Bad Request for url: ..." -
    # Telegram's real reason (e.g. "file is too big") is in the JSON body, thrown away by default.
    try:
        return r.json().get("description", r.text)
    except ValueError:
        return r.text


def tg_download_file(token: str, file_id: str, dest_path: Path, cfg: dict) -> None:
    """Downloads a Telegram-hosted file (voice note/audio/document attachment) - no yt-dlp
    involved, this isn't a social-media link. Always tries the real api.telegram.org first - cheap,
    no Docker involved, and correct for the overwhelming majority of files (anything under the
    public API's 20MB cap). Docker/the local server (local-bot-api/README.md) only get started as
    a fallback, on demand, the moment a file genuinely turns out to be too big - never upfront on
    every run regardless of whether anything in this batch actually needs it. This also means a
    Docker/Docker Desktop problem only fails the one job that needed it, not the whole run."""
    r = requests.get(f"https://api.telegram.org/bot{token}/getFile",
                      params={"file_id": file_id}, timeout=30)
    if r.ok:
        file_path = r.json()["result"]["file_path"]
        r2 = requests.get(f"https://api.telegram.org/file/bot{token}/{file_path}", timeout=120)
        r2.raise_for_status()
        dest_path.write_bytes(r2.content)
        return

    desc = _tg_getfile_error(r)
    local_url = cfg.get("local_bot_api_url")
    if "too big" not in desc.lower() or not local_url:
        raise RuntimeError(f"Telegram getFile failed: {desc}")

    # Genuinely too big for the public API, and a local server is configured - start it now, only
    # because this specific file actually needs it. start_local_bot_api/atexit registration are
    # both safely idempotent, so calling this again for a second oversized file later in the same
    # run just confirms it's already up rather than restarting anything.
    container = cfg.get("local_bot_api_container_name") or LOCAL_BOT_API_CONTAINER_NAME
    try:
        start_local_bot_api(container, local_url, cfg)
    except Exception as e:
        raise RuntimeError(f"file too big for the public API, and couldn't start the local Bot "
                           f"API server ({container}) to handle it: {e}") from e
    atexit.register(stop_docker_desktop)
    atexit.register(stop_local_bot_api, container)

    # The local server fetches the file's actual bytes over MTProto before this call can return at
    # all - confirmed by hand that a 58MB file took well over 15s (succeeded at 60s) on its first
    # request, so this needs real headroom, not the quick timeout that's enough against the
    # public API's lightweight metadata-only response.
    r = requests.get(f"{local_url}/bot{token}/getFile", params={"file_id": file_id}, timeout=180)
    if not r.ok:
        raise RuntimeError(f"Telegram getFile failed (local server): {_tg_getfile_error(r)}")
    file_path = r.json()["result"]["file_path"]

    if file_path.startswith(LOCAL_BOT_API_CONTAINER_DIR):
        # Local mode (TELEGRAM_LOCAL=1): file_path is an absolute path *inside the server's own
        # container*, not a URL - the old HTTP download route is simply gone in this mode
        # (confirmed by hand: a real getFile there now 404s). `docker cp` pulls it out instead,
        # working regardless of how the container's volume is set up - verified against a real
        # local server: a host bind-mount straight to /var/lib/telegram-bot-api crashes the server
        # outright (SIGABRT), since telegram-bot-api names each bot's data directory after the
        # literal token, which contains a colon - an invalid path character on Windows. `docker cp`
        # sidesteps that entirely by going through the Docker daemon instead of the host filesystem.
        subprocess.run(["docker", "cp", f"{container}:{file_path}", str(dest_path)],
                       check=True, capture_output=True, text=True)
        return

    r2 = requests.get(f"{local_url}/file/bot{token}/{file_path}", timeout=120)
    r2.raise_for_status()
    dest_path.write_bytes(r2.content)


def _finalize_note(remainder: str):
    """Given the message text with the URL substring already removed (or the whole text, if
    the URL wasn't literally part of the visible text - see find_message_url), returns
    (note, delete_video) applying the same save-word and delete-after-transcribe rules."""
    if not remainder:
        return None, False
    keep_video = bool(SAVE_RE.search(remainder))
    if keep_video:
        remainder = " ".join(SAVE_RE.sub("", remainder).split())
    return (remainder or None), (not keep_video)


def extract_url_and_note(text: str):
    """Returns (url, note, delete_video).

    Bare link only -> keep the video, no note.
    Link + any other text -> delete the video after transcribing, that text becomes the note.
    Link + text containing the standalone word "save" -> keep the video anyway; "save" is
    stripped out of the saved note (any remaining text around it is still saved as the note).
    """
    if not text:
        return None, None, False
    m = URL_RE.search(text)
    if not m:
        return None, None, False
    url = m.group(0)
    remainder = " ".join((text[:m.start()] + text[m.end():]).split())
    note, delete_video = _finalize_note(remainder)
    return url, note, delete_video


def find_message_url(msg: dict):
    """Returns (url, note, delete_video) for a Telegram message. Checks plain text/caption
    first (the normal case), then falls back to text_link entities: some share-sheet flows
    hand Telegram a hyperlinked title instead of a literal pasted URL, in which case the URL
    lives in message.entities/caption_entities, not in the text itself - missing this meant
    those messages were silently skipped with no error, no log line, nothing."""
    text = msg.get("text") or msg.get("caption") or ""
    url, note, delete_video = extract_url_and_note(text)
    if url:
        return url, note, delete_video
    for ent in (msg.get("entities") or []) + (msg.get("caption_entities") or []):
        if ent.get("type") == "text_link" and ent.get("url"):
            note, delete_video = _finalize_note(text.strip())
            return ent["url"], note, delete_video
    return None, None, False


def find_message_audio(msg: dict):
    """Returns {'file_id', 'file_name'} for a voice note, audio file, or a document that's
    actually audio (by mime type or extension) - None otherwise. Only checked when no URL was
    found (find_message_url takes priority), since pasting a link alongside an audio attachment
    is rare and the link is the more actionable thing to act on."""
    for key in ("voice", "audio"):
        att = msg.get(key)
        if att:
            return {"file_id": att["file_id"],
                    "file_name": att.get("file_name") or f"{att['file_id']}.ogg"}
    doc = msg.get("document")
    if doc:
        name = doc.get("file_name") or ""
        mime = doc.get("mime_type") or ""
        if mime.startswith("audio/") or Path(name).suffix.lower() in AUDIO_EXTS:
            return {"file_id": doc["file_id"], "file_name": name or f"{doc['file_id']}.m4a"}
    return None


# --- download -------------------------------------------------------------------
def classify_error(err_text: str) -> str:
    low = err_text.lower()
    if any(k in low for k in PRIVATE_KEYWORDS):
        return "looks private/login-walled"
    return "other error"


def looks_like_image_post(err_text: str) -> bool:
    return "no video formats found" in err_text.lower()


LONG_FORM_MIN_SECONDS = 180  # yt-dlp's "duration" field, in seconds

# Substrings of known-transient yt-dlp/YouTube errors worth a retry rather than an immediate
# failure - a plain re-extraction typically succeeds. "needs to be reloaded" is a documented,
# intermittent YouTube-side hiccup unrelated to the video itself. "http error 403" showed up
# separately on the same real video once cookies were dropped for the reload error above - it
# happens at the actual data-fetch stage, after a format was already successfully selected, so
# it's a signed-CDN-URL/session mismatch rather than a real permission error (which surfaces
# earlier, during extraction) - re-extracting gets a fresh URL and has succeeded every time so far.
# "video unavailable" is a third symptom of the exact same stale-cookie-DB root cause: isolated by
# hand on a real video that failed with cookies and succeeded immediately without them, same URL,
# same moment, cookies the only variable - not an actually-deleted/region-blocked video, which
# would fail the same way with or without cookies. If a video genuinely is gone, dropping cookies
# on retry just gets the same (or an equally definitive) failure, so this never masks a real one.
TRANSIENT_ERROR_SUBSTRINGS = ("needs to be reloaded", "http error 403", "video unavailable")


RETRY_BACKOFF_SECONDS = (0, 5, 15, 30)


def download_video(url: str, dest_dir: Path, cfg: dict) -> dict:
    dest_dir.mkdir(parents=True, exist_ok=True)
    auth_opts = _yt_dlp_auth_opts(cfg)

    for attempt, wait in enumerate(RETRY_BACKOFF_SECONDS, 1):
        if wait:
            time.sleep(wait)
        # Cookies are dropped after the first attempt. Root-caused by hand: yt-dlp's
        # cookiesfrombrowser reads the browser's live cookie database directly, and doing that
        # while the browser is actually open and writing to it reproducibly returns a stale/locked
        # snapshot that makes YouTube reject the session with "needs to be reloaded" - confirmed
        # 3/3 failures with Firefox cookies vs 0/3 without, same video, same moment, cookies were
        # the only variable. This isn't time-based (a longer wait alone never fixed it), so if the
        # content genuinely needs cookies (private/login-walled), dropping them just surfaces that
        # as its own, correctly-classified failure instead of retrying forever.
        this_auth = auth_opts if attempt == 1 else {}
        try:
            # Cheap pre-flight (process=False skips format-list resolution, the expensive part
            # for YouTube) just to read duration/extractor_key - long-form YouTube can run well
            # past an hour, where 1080p adds a lot of disk cost for marginal gain in this
            # archive's use case (search + rewatch). Shorts and other platforms keep the higher cap.
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, **this_auth}) as probe:
                preinfo = probe.extract_info(url, download=False, process=False)
            is_long_youtube = (preinfo.get("extractor_key") == "Youtube"
                               and (preinfo.get("duration") or 0) > LONG_FORM_MIN_SECONDS)
            max_height = 720 if is_long_youtube else 1080

            ydl_opts = {
                "format": f"bv*[height<={max_height}]+ba/b[height<={max_height}]/best",
                "merge_output_format": "mp4",
                "outtmpl": str(dest_dir / "video.%(ext)s"),
                "quiet": False,
                "retries": 2,
                # YouTube requires solving a JS-obfuscated "n" parameter for some formats; yt-dlp
                # only trusts "deno" for this by default. Add "node" as a fallback since that's
                # commonly already installed (and harmless to list even if neither is present).
                "js_runtimes": {"deno": {}, "node": {}},
                **this_auth,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=True)
        except Exception as e:
            transient = any(s in str(e).lower() for s in TRANSIENT_ERROR_SUBSTRINGS)
            if not transient or attempt == len(RETRY_BACKOFF_SECONDS):
                raise
            dropped = " (dropping cookies)" if this_auth else ""
            print(f"  transient error{dropped}, retrying "
                  f"({attempt}/{len(RETRY_BACKOFF_SECONDS)}): {e}", flush=True)


def _yt_dlp_auth_opts(cfg: dict) -> dict:
    opts = {}
    browser = cfg.get("cookies_from_browser")
    cookies_file = cfg.get("cookies_file")
    if browser:
        opts["cookiesfrombrowser"] = (browser,)
    elif cookies_file and Path(cookies_file).is_file():
        opts["cookiefile"] = cookies_file
    return opts


def download_post(url: str, dest_dir: Path, cfg: dict) -> dict:
    """Handles a photo post or carousel (no video formats) - a single post, or one entry per
    carousel item. Images are fetched directly (they're plain signed CDN URLs, no yt-dlp
    downloader needed); any video mixed into a carousel goes through yt-dlp's own downloader
    so DASH/merge handling stays correct. Returns
    {'platform', 'caption', 'image_paths': [Path,...], 'video_paths': [Path,...],
     'video_errors': [(item_index, message), ...]}.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    base_opts = {"quiet": True, "no_warnings": True, **_yt_dlp_auth_opts(cfg)}

    with yt_dlp.YoutubeDL(base_opts) as ydl:
        info = ydl.extract_info(url, download=False, process=False)

    entries = list(info["entries"]) if info.get("_type") == "playlist" else [info]
    platform = info.get("extractor_key") or info.get("extractor") or "unknown"
    caption = (info.get("description") or "").strip()

    image_paths, video_paths, video_errors = [], [], []
    for idx, entry in enumerate(entries, 1):
        if entry.get("formats"):
            # a failed video item is reported back rather than failing the whole post
            try:
                v_opts = {**base_opts, "outtmpl": str(dest_dir / f"video_{idx:02d}.%(ext)s"),
                          "merge_output_format": "mp4"}
                with yt_dlp.YoutubeDL(v_opts) as vydl:
                    vydl.process_ie_result(dict(entry), download=True)
                found = sorted(p for p in dest_dir.glob(f"video_{idx:02d}.*")
                               if p.suffix.lower() in VIDEO_EXTS)
                if found:
                    video_paths.append(found[0])
                else:
                    video_errors.append((idx, "no video file produced"))
            except Exception as e:
                video_errors.append((idx, str(e)))
            continue

        thumbs = entry.get("thumbnails") or []
        img_url = thumbs[-1].get("url") if thumbs else None
        if not img_url:
            continue
        r = requests.get(img_url, timeout=30)
        r.raise_for_status()
        img_path = dest_dir / f"image_{idx:02d}.jpg"
        img_path.write_bytes(r.content)
        image_paths.append(img_path)

    if not image_paths and not video_paths:
        detail = "; ".join(f"item {i}: {m}" for i, m in video_errors)
        raise ValueError("no downloadable image or video items found in this post"
                         + (f" ({detail})" if detail else ""))

    return {"platform": platform, "caption": caption, "image_paths": image_paths,
            "video_paths": video_paths, "video_errors": video_errors}


# --- OCR (for image posts/carousels - on-image text the caption alone misses) ---
OCR_VRAM_MB = 2560  # measured: about 2.5 GB in use after OCRing a carousel of dense small print


def load_ocr_engine():
    # Same engine + settings already proven in Instagram-Autopost/ig-research/reocr_full_corpus.py
    # paddlepaddle-gpu is installed without its own CUDA packages and relies on the CUDA libraries
    # torch loads, which works because collect.py always imports torch first.
    import paddle
    from paddleocr import PaddleOCR
    device = "cpu"
    if paddle.device.is_compiled_with_cuda() and paddle.device.cuda.device_count() > 0:
        busy = gpu_busy_reason(OCR_VRAM_MB)
        if busy:
            print(f"  {busy} - running OCR on the CPU for this run instead", flush=True)
        else:
            device = "gpu:0"
    else:
        note_missing_gpu_build("PaddleOCR")
    print(f"  PaddleOCR device: {device}", flush=True)
    return PaddleOCR(
        lang="en", use_doc_orientation_classify=False, use_doc_unwarping=False,
        use_textline_orientation=False, enable_mkldnn=False, device=device,
    )


def run_ocr(engine, image_paths: list) -> list:
    """Returns one text block per image, same order as image_paths."""
    texts = []
    for p in image_paths:
        try:
            result = engine.predict(str(p))
            found = []
            for res in result:
                found.extend(res.get("rec_texts", []))
            texts.append("\n".join(found).strip())
        except Exception as e:
            texts.append(f"[OCR failed: {e}]")
    return texts


# --- flagged keyword moments (video content only - things worth screenshotting by hand) ---
def format_timestamp(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def build_flag_keyword_re(keywords: list) -> re.Pattern:
    escaped = sorted((re.escape(k) for k in keywords), key=len, reverse=True)
    return re.compile(r"\b(" + "|".join(escaped) + r")\b", re.I)


def find_flagged_moments(video_path: Path, segments: list, keyword_re: re.Pattern) -> list:
    """Returns a list of moment dicts ({video_path, start, word, text}) for every Whisper segment
    whose text matches one of the flag keywords - segments carry real timestamps, unlike the plain
    .txt output, which is what makes it possible to screenshot the exact moment later, not just log
    that it happened."""
    moments = []
    for seg in segments:
        text = seg.get("text", "")
        m = keyword_re.search(text)
        if m:
            moments.append({"video_path": video_path, "start": seg.get("start", 0.0),
                             "word": m.group(0), "text": text.strip()})
    return moments


SCREENSHOT_WIDTH = 960  # well below the source video - just enough to stay readable on review


def capture_flagged_screenshots(moments: list, out_dir: Path) -> None:
    """Grabs one frame per moment via ffmpeg and sets moment['screenshot'] to the saved Path (left
    unset on failure). Uses input-side -ss (a fast keyframe seek) rather than seeking after -i -
    frame-exact isn't the point here ("something was on screen around here"), and output-side
    seeking would decode from the start of the file every time, which is far too slow on an
    hours-long video. Called before the video is deleted - this is what keeps the moment's visual
    content once the source file is gone."""
    for idx, moment in enumerate(moments, 1):
        ts = format_timestamp(moment["start"]).replace(":", "-")
        out_path = out_dir / f"flagged_{idx:02d}_{ts}.jpg"
        try:
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-ss", str(moment["start"]),
                 "-i", str(moment["video_path"]), "-frames:v", "1",
                 "-vf", f"scale={SCREENSHOT_WIDTH}:-2", str(out_path)],
                check=True, capture_output=True,
            )
            moment["screenshot"] = out_path
        except Exception as e:
            print(f"  (warning: screenshot failed at {format_timestamp(moment['start'])}: {e})",
                  flush=True)


def ocr_flagged_screenshots(engine, moments: list) -> None:
    """OCRs every moment that got a screenshot, setting moment['ocr_text']. Kept as a separate pass
    from capture_flagged_screenshots so a failed screenshot doesn't block OCR of the ones that
    succeeded, and so the (lazily-loaded) OCR engine is only touched when there's actually
    something to read."""
    shot_moments = [m for m in moments if m.get("screenshot")]
    if not shot_moments:
        return
    texts = run_ocr(engine, [m["screenshot"] for m in shot_moments])
    for m, text in zip(shot_moments, texts):
        m["ocr_text"] = text


def write_flagged_moments(stage_dir: Path, moments: list, keywords: list) -> None:
    lines = [f"{len(moments)} flagged moment(s) found (keywords: {', '.join(keywords)})", ""]
    for m in moments:
        lines.append(f"[{format_timestamp(m['start'])}] (\"{m['word']}\") {m['text']}")
        if m.get("screenshot"):
            lines.append(f"  screenshot: {m['screenshot'].name}")
            ocr_text = (m.get("ocr_text") or "").strip()
            lines.append(f"  on-screen text (OCR): {ocr_text}" if ocr_text
                         else "  on-screen text (OCR): (none detected - check the screenshot by eye)")
        lines.append("")
    (stage_dir / "flagged_moments.txt").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


# --- source.txt (single source of truth for both fresh and resumed items) ------
def write_source_txt(stage_dir: Path, url: str, platform: str, title: str, note, delete_video: bool):
    lines = [
        f"URL: {url}",
        f"PLATFORM: {platform}",
        f"TITLE: {title or ''}",
        f"RECEIVED: {dt.datetime.now().isoformat(timespec='seconds')}",
        f"VIDEO: {'deleted-after-transcribe' if delete_video else 'keep'}",
    ]
    if note:
        lines.append(f"PERSONAL NOTE: {note}")
    (stage_dir / "source.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_source_txt(stage_dir: Path) -> dict:
    content = (stage_dir / "source.txt").read_text(encoding="utf-8")

    def field(name):
        m = re.search(rf"^{name}: (.*)$", content, re.M)
        return m.group(1) if m else None

    return {
        "url": field("URL"),
        "platform": field("PLATFORM"),
        "title": field("TITLE"),
        "delete_video": field("VIDEO") == "deleted-after-transcribe",
        "note": field("PERSONAL NOTE"),
    }


# --- naming ---------------------------------------------------------------------
def slugify(text: str, max_len: int) -> str:
    text = (text or "").lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text).strip("-")
    return text[:max_len].rstrip("-")


def build_final_name(title: str, transcript_text: str, fallback_id: str) -> str:
    # Named from the transcript's own opening words - platform titles are usually boilerplate
    # ("Video by so-and-so") and just make the name longer without adding information.
    snippet = " ".join((transcript_text or "").strip().split()[:8])
    snippet_slug = slugify(snippet, 60)
    if snippet_slug:
        return snippet_slug
    title_slug = slugify(title, 60)
    if title_slug:
        return title_slug
    return f"clip-{fallback_id or 'unknown'}"


def find_video_file(stage_dir: Path):
    """Returns the staged video file, or None. Filtered to VIDEO_EXTS rather than a bare
    video.* glob - a staging dir that already has transcript sidecars (video.json/.srt/.vtt/.txt)
    from an earlier attempt would otherwise let one of those get handed to Whisper/ffmpeg
    instead of the actual video."""
    matches = sorted(p for p in stage_dir.glob("video.*") if p.suffix.lower() in VIDEO_EXTS)
    return matches[0] if matches else None


def unique_dest(parent: Path, name: str) -> Path:
    candidate = parent / name
    n = 2
    while candidate.exists():
        candidate = parent / f"{name}-{n}"
        n += 1
    return candidate


# --- transcription ---------------------------------------------------------------
GPU_BUSY_UTIL_PERCENT = 30  # average load at or above this means something else is using the GPU
GPU_BUSY_SAMPLES = 3        # GPU load is spiky - a single reading can catch a momentary blip
WHISPER_VRAM_MB = {"tiny": 1024, "base": 1024, "small": 2048, "medium": 5120, "large": 10240,
                   "turbo": 6144}  # openai-whisper's published requirements


DEVICE_WARNINGS = []  # reported in log.txt and the Telegram summary at the end of the run


def note_missing_gpu_build(library: str) -> None:
    """Records a warning when an NVIDIA GPU is present but this install can't use it. A plain
    `pip install` on Windows silently gets CPU-only builds, which went unnoticed here for months
    because everything still worked, just several times slower."""
    if shutil.which("nvidia-smi"):
        warning = (f"{library} can't use the NVIDIA GPU in this install, so it's running on the "
                   f"CPU - see \"GPU setup\" in README.md")
        print(f"WARNING: {warning}", flush=True)
        if warning not in DEVICE_WARNINGS:
            DEVICE_WARNINGS.append(warning)


_GPU_UNCHECKED = object()
_gpu_verdict = _GPU_UNCHECKED


def gpu_busy_reason(needed_mb: int):
    """Returns this run's GPU verdict, reading it only the first time it's asked for. That first
    call happens before the run has put any load on the GPU; checking again later would mistake
    our own work for someone else's (caught for real: OCR finishing left the GPU reading 33% busy,
    which sent Whisper to the CPU for the whole run)."""
    global _gpu_verdict
    if _gpu_verdict is _GPU_UNCHECKED:
        _gpu_verdict = _read_gpu_busy_reason(needed_mb)
    return _gpu_verdict


def _read_gpu_busy_reason(needed_mb: int):
    """Returns why the GPU shouldn't be used for this run (already under load, or less free VRAM
    than needed_mb), or None if it's free. Being unable to tell (nvidia-smi missing, or reporting
    [N/A]) also returns None: the library already confirmed CUDA works, so an unreadable
    monitoring tool isn't a reason to give up the much faster device."""
    utils = []
    used = total = 0
    for i in range(GPU_BUSY_SAMPLES):
        if i:
            time.sleep(1)
        try:
            r = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total",
                                "--format=csv,noheader,nounits"],
                               capture_output=True, text=True, timeout=10)
            util, used, total = (int(x) for x in r.stdout.splitlines()[0].split(","))
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            return None
        utils.append(util)
    avg_util = sum(utils) / len(utils)
    if avg_util >= GPU_BUSY_UTIL_PERCENT:
        return f"GPU is {avg_util:.0f}% busy"
    if total - used < needed_mb:
        return f"only {total - used} MB of GPU memory free, about {needed_mb} MB needed"
    return None


def load_whisper(cfg: dict):
    model_name = cfg.get("whisper_model", "small")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    if device == "cuda":
        needed = WHISPER_VRAM_MB.get(re.split(r"[.-]", model_name)[0], WHISPER_VRAM_MB["small"])
        busy = gpu_busy_reason(needed)
        if busy:
            print(f"{busy} - using the CPU for this run instead", flush=True)
            device = "cpu"
    else:
        note_missing_gpu_build("Whisper")
    print(f"loading Whisper model '{model_name}' ({device})...", flush=True)
    return whisper.load_model(model_name, device=device), device


def transcribe_video(model, device: str, video_path: Path, out_dir: Path,
                     delete_video: bool, language: str = None) -> dict:
    """Transcribes video_path (or any audio file - Whisper doesn't care) and writes the
    transcript files into out_dir, named after the source's own stem (video.txt, video_02.srt,
    ...). language forces Whisper's language instead of auto-detecting from the first ~30s, which
    matters for code-switched audio (e.g. mostly-Serbian voice notes with the odd English word) -
    None keeps the existing auto-detect behavior for every other caller. Returns Whisper's
    result dict."""
    try:
        result = model.transcribe(str(video_path), fp16=(device == "cuda"), verbose=False,
                                  language=language)
    except RuntimeError as e:
        # A silent video (no audio stream at all - some Reels/carousel clips genuinely have none)
        # makes Whisper's ffmpeg call fail with its *entire* compiled-in banner (version, build
        # flags, library versions) crammed into the exception text - thousands of characters for
        # what's really just "there's no audio here". Caught once, by hand, on a real clip: this
        # was long enough (stacked with a couple other failures the same run) to push the Telegram
        # summary over Telegram's 4096-char limit, which failed the whole send silently.
        if "does not contain any stream" in str(e):
            raise RuntimeError("no audio track (silent video)") from e
        raise
    opts = {"max_line_width": 80, "max_line_count": 2}
    if delete_video:
        # no video to sync captions to - keep just the plain-text transcript
        get_writer("txt", str(out_dir))(result, str(video_path), opts)
    else:
        get_writer("all", str(out_dir))(result, str(video_path), opts)
        (out_dir / f"{video_path.stem}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


# --- main -------------------------------------------------------------------
def main() -> int:
    lock = acquire_lock()
    if lock is None:
        print("Another collect.py run is already in progress (collect.lock is held) - exiting "
              "rather than risk a duplicate download or a racing transcription. Just try again "
              "once that run finishes.", file=sys.stderr)
        return 1

    cfg = load_config()
    token = cfg["bot_token"]
    output_root = Path(cfg["output_root"])
    output_root.mkdir(parents=True, exist_ok=True)

    # Docker/the local Bot API server, if configured, are deliberately *not* started here - only
    # tg_download_file starts them, on demand, the moment a file actually turns out to be too big
    # for the public API. Most runs never touch an oversized file, so most runs never see Docker
    # at all - no unconditional startup cost, no Docker Desktop window popping up (even briefly)
    # on a run that didn't need it.
    fetch_at = dt.datetime.now()
    expected_first_id = cfg.get("last_update_id", 0) + 1
    updates = tg_get_updates(token, expected_first_id)
    # last_fetch_at is only saved once this warning has made it into log.txt (or there was nothing
    # to warn about) - if the run crashes first, the next run still measures from the older fetch
    # time and re-derives the same warning instead of losing it.
    dropped_warning = check_dropped_updates(cfg.get("last_fetch_at"), fetch_at,
                                            expected_first_id, updates)
    if dropped_warning:
        print(f"WARNING: {dropped_warning}", flush=True)

    if cfg.get("chat_id") is None:
        if not updates:
            print("No chat_id configured yet, and no messages found.\n"
                  "Send the bot any message from your phone first, then run this again.")
            return 1
        first_msg = updates[0].get("message") or updates[0].get("channel_post") or {}
        chat_id = first_msg.get("chat", {}).get("id")
        if chat_id is None:
            print("Couldn't read a chat_id from the first update - try sending the bot a plain "
                  "text message and re-running.")
            return 1
        cfg["chat_id"] = chat_id
        print(f"Detected chat_id: {chat_id} (saved to config.json)")
        save_config(cfg)

    jobs = []
    skipped = []  # descriptions of messages with no extractable URL - see log for why this matters
    cancelled_count = 0
    for upd in updates:
        update_id = upd["update_id"]
        if "edited_message" in upd or "edited_channel_post" in upd:
            # Telegram sends one of these whenever a message is edited - including automatically,
            # e.g. once a link preview attaches to an already-sent URL, with no user action at all.
            # The original message/channel_post already produced a job (or got logged) when it was
            # first sent, so this is consumed silently rather than reprocessed - re-running it would
            # risk a duplicate download, and ignoring it outright showed up as spurious
            # "[empty message]" skip noise (it has neither "message" nor "channel_post" key).
            advance_last_update_id(cfg, update_id)
            continue
        msg = upd.get("message") or upd.get("channel_post") or {}
        text = msg.get("text") or msg.get("caption") or ""
        if text.strip().lower() == "c":
            # Bare "c", nothing else - a remote cancel signal (e.g. you sent a link by mistake
            # and want to retract it before it's processed). Only discards jobs queued *earlier*
            # in this same fetched batch - anything sent after the "c" is unaffected and processed
            # normally below, even though it's all one batch by the time a run picks it up.
            cancelled_count += len(jobs)
            jobs = []
            advance_last_update_id(cfg, update_id)
            continue
        url, note, delete_video = find_message_url(msg)
        if url:
            jobs.append({"update_id": update_id, "kind": "url", "url": url, "note": note,
                         "delete_video": delete_video})
            continue
        audio = find_message_audio(msg)
        if audio:
            note, delete_video = _finalize_note(text.strip())
            jobs.append({"update_id": update_id, "kind": "audio", "note": note,
                         "delete_video": delete_video, **audio})
            continue
        has_media = bool(msg.get("photo") or msg.get("video") or msg.get("document"))
        preview = text.strip()[:60] or ("[media, no text/caption]" if has_media else "[empty message]")
        skipped.append(preview)
        advance_last_update_id(cfg, update_id)

    cancel_seen = cancelled_count > 0
    if cancel_seen:
        print(f"Cancel ('c') received - discarding {cancelled_count} queued link(s) sent before "
              f"it, nothing downloaded.", flush=True)

    today_dir = output_root / dt.date.today().isoformat()
    download_ok = 0
    download_failures = []   # list of (url, reason)
    transcribe_ok = 0
    transcribe_failures = []  # list of (identifier, reason)
    personal_notes = []
    flagged_counts = []
    silent_videos = []  # finalized with no transcript - a confirmed fact, not worth retrying forever
    staging_dirs = []
    ocr_engine = None  # lazy: loaded on first image post this run, reused after that
    whisper_bundle = None  # lazy (model, device): loaded on first video this run, reused after
    flag_keywords = cfg.get("flag_keywords", DEFAULT_FLAG_KEYWORDS)
    flag_keyword_re = build_flag_keyword_re(flag_keywords)

    try:
        for i, job in enumerate(jobs, 1):
            stage_dir = today_dir / f"_staging_{job['update_id']}"

            if job["kind"] == "audio":
                # A direct voice note/audio attachment, not a social-media link - no yt-dlp, no
                # OCR, no carousel, and no _staging_* resume (same reasoning as carousels: these
                # are short, so an interrupted one is simply redone from scratch next run rather
                # than needing the deferred-transcription machinery built for hours-long video).
                print(f"[{i}/{len(jobs)}] downloading audio: {job['file_name']}", flush=True)
                try:
                    stage_dir.mkdir(parents=True, exist_ok=True)
                    ext = Path(job["file_name"]).suffix.lower() or ".m4a"
                    audio_path = stage_dir / f"audio{ext}"
                    tg_download_file(token, job["file_id"], audio_path, cfg)

                    if whisper_bundle is None:
                        whisper_bundle = load_whisper(cfg)
                    model, device = whisper_bundle
                    result = transcribe_video(model, device, audio_path, stage_dir,
                                              job["delete_video"],
                                              language=cfg.get("whisper_language"))

                    final_name = build_final_name("", result["text"], fallback_id=stage_dir.name)
                    final_dir = unique_dest(stage_dir.parent, final_name)

                    lines = [
                        f"SOURCE: telegram audio ({job['file_name']})",
                        f"RECEIVED: {dt.datetime.now().isoformat(timespec='seconds')}",
                        f"AUDIO: {'deleted-after-transcribe' if job['delete_video'] else 'keep'}",
                    ]
                    if job["note"]:
                        lines.append(f"PERSONAL NOTE: {job['note']}")
                    (stage_dir / "source.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

                    stage_dir.rename(final_dir)
                    if job["delete_video"]:
                        (final_dir / audio_path.name).unlink(missing_ok=True)

                    moments = find_flagged_moments(final_dir / audio_path.name,
                                                   result.get("segments", []), flag_keyword_re)
                    if moments:
                        write_flagged_moments(final_dir, moments, flag_keywords)
                        print(f"  -> {len(moments)} flagged moment(s) "
                              f"(screenshot/screengrab mentions)", flush=True)
                        flagged_counts.append(f"{final_dir.name}: {len(moments)} moment(s)")

                    if job["note"]:
                        personal_notes.append(f'{final_dir.name}: "{job["note"]}"')
                    download_ok += 1
                    transcribe_ok += 1
                except Exception as e:
                    print(f"  FAILED (audio): {e}", flush=True)
                    download_failures.append((job["file_name"], classify_error(str(e))))
                    shutil.rmtree(stage_dir, ignore_errors=True)
                advance_last_update_id(cfg, job["update_id"])
                continue

            print(f"[{i}/{len(jobs)}] downloading: {job['url']}", flush=True)
            post = None
            try:
                info = download_video(job["url"], stage_dir, cfg)
            except Exception as e:
                if looks_like_image_post(str(e)):
                    # download_video can partially succeed before erroring - e.g. a carousel's
                    # first entry is itself a video, downloaded fine as video.<ext>, before a later
                    # image-only entry raises "no video formats found" for the whole extraction.
                    # Clear whatever it left behind before retrying as a carousel, so that stray,
                    # untracked file doesn't survive alongside download_post's own correctly-named
                    # output (video_NN.*) - never transcribed, never deleted, just dead weight.
                    shutil.rmtree(stage_dir, ignore_errors=True)
                    try:
                        post = download_post(job["url"], stage_dir, cfg)
                    except Exception as e2:
                        e = e2
                        post = None
                if post is None:
                    reason = classify_error(str(e))
                    print(f"  FAILED ({reason}): {e}", flush=True)
                    download_failures.append((job["url"], reason))
                    shutil.rmtree(stage_dir, ignore_errors=True)
                    advance_last_update_id(cfg, job["update_id"])
                    continue

            if post is not None:
                # Photo post/carousel - finalized right here rather than deferring to the staging
                # phase below: images go through OCR, any videos mixed in are transcribed inline.
                # The caption stands in for a transcript when naming and as the saved text.
                caption = post["caption"]
                n_img, n_vid = len(post["image_paths"]), len(post["video_paths"])
                print(f"  -> {n_img} image(s), {n_vid} video(s)", flush=True)
                for idx, err in post["video_errors"]:
                    print(f"  carousel video #{idx} failed to download: {err}", flush=True)
                    download_failures.append((f"{job['url']} (carousel video #{idx})",
                                              classify_error(err)))

                ocr_texts = []
                if post["image_paths"]:
                    if ocr_engine is None:
                        print("  loading PaddleOCR model...", flush=True)
                        ocr_engine = load_ocr_engine()
                    print(f"  running OCR on {n_img} image(s)...", flush=True)
                    ocr_texts = run_ocr(ocr_engine, post["image_paths"])
                    if any(t.strip() for t in ocr_texts):
                        blocks = [f"=== {p.name} ===\n{t}"
                                  for p, t in zip(post["image_paths"], ocr_texts)]
                        (stage_dir / "ocr.txt").write_text(
                            "\n\n".join(blocks) + "\n", encoding="utf-8")

                transcripts = []  # (video_path, whisper result) for each video that transcribed ok
                silent_in_post = []  # video names with no audio track - reported once final_dir exists
                for vp in post["video_paths"]:
                    print(f"  transcribing carousel video: {vp.name}", flush=True)
                    try:
                        if whisper_bundle is None:
                            whisper_bundle = load_whisper(cfg)
                        model, device = whisper_bundle
                        transcripts.append(
                            (vp, transcribe_video(model, device, vp, stage_dir, job["delete_video"])))
                        transcribe_ok += 1
                    except Exception as e:
                        if "no audio track" in str(e):
                            # same as the single-video case: a fixed fact about the clip, not a
                            # failure - it's kept (not in `transcripts`, so never deletable)
                            print(f"  no audio track ({vp.name}) - keeping video, skipping transcript",
                                  flush=True)
                            silent_in_post.append(vp.name)
                            continue
                        print(f"  TRANSCRIBE FAILED ({vp.name}): {e}", flush=True)
                        transcribe_failures.append(
                            (f"{job['url']} [{vp.name}]", f"transcription error: {e}"))

                # Same keep/delete rule as single videos. Media whose text extraction failed (OCR
                # error, transcription error) is kept - deleting it would leave nothing of it behind.
                deletable_images = [p for p, t in zip(post["image_paths"], ocr_texts)
                                    if job["delete_video"] and not t.startswith("[OCR failed")]
                deletable_videos = [vp for vp, _ in transcripts if job["delete_video"]]

                lines = [
                    f"URL: {job['url']}",
                    f"PLATFORM: {post['platform']}",
                    f"RECEIVED: {dt.datetime.now().isoformat(timespec='seconds')}",
                    f"CONTENT: {n_img} image(s), {n_vid} video(s)",
                ]
                if n_img:
                    lines.append(f"IMAGES: {'deleted-after-ocr' if deletable_images else 'keep'}")
                if n_vid:
                    lines.append(f"VIDEO: {'deleted-after-transcribe' if deletable_videos else 'keep'}")
                if job["note"]:
                    lines.append(f"PERSONAL NOTE: {job['note']}")
                (stage_dir / "source.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
                if caption:
                    (stage_dir / "caption.txt").write_text(caption + "\n", encoding="utf-8")

                plain_ocr = " ".join(t for t in ocr_texts if t and not t.startswith("[OCR failed"))
                spoken = " ".join(r["text"].strip() for _, r in transcripts)
                naming_source = next((s for s in (caption, plain_ocr, spoken) if s.strip()), "")
                final_name = build_final_name("", naming_source, fallback_id=stage_dir.name)
                final_dir = unique_dest(stage_dir.parent, final_name)
                stage_dir.rename(final_dir)
                silent_videos.extend(f"{final_dir.name} [{name}]" for name in silent_in_post)

                # re-point every path at final_dir now that the rename moved the files under it
                transcripts = [(final_dir / vp.name, result) for vp, result in transcripts]
                deletable_images = [final_dir / p.name for p in deletable_images]
                deletable_videos = [final_dir / vp.name for vp in deletable_videos]

                moments = []
                for vp, result in transcripts:
                    for m in find_flagged_moments(vp, result.get("segments", []), flag_keyword_re):
                        if len(transcripts) > 1:
                            m["text"] = f"{vp.name}: {m['text']}"
                        moments.append(m)

                if moments and job["delete_video"]:
                    # screenshot before the video goes - it's about to be unlinked below
                    capture_flagged_screenshots(moments, final_dir)
                    if any(m.get("screenshot") for m in moments):
                        if ocr_engine is None:
                            print("  loading PaddleOCR model...", flush=True)
                            ocr_engine = load_ocr_engine()
                        ocr_flagged_screenshots(ocr_engine, moments)

                for media in deletable_images + deletable_videos:
                    try:
                        media.unlink(missing_ok=True)
                    except OSError as e:
                        print(f"  (warning: couldn't delete {media.name}: {e})", flush=True)

                if moments:
                    write_flagged_moments(final_dir, moments, flag_keywords)
                    print(f"  -> {len(moments)} flagged moment(s) "
                          f"(screenshot/screengrab mentions)", flush=True)
                    flagged_counts.append(f"{final_dir.name}: {len(moments)} moment(s)")

                if job["note"]:
                    personal_notes.append(f'{final_dir.name}: "{job["note"]}"')
            else:
                platform = info.get("extractor_key") or info.get("extractor") or "unknown"
                title = info.get("title") or ""
                if title.strip().lower() == str(info.get("id", "")).lower():
                    title = ""  # some extractors return the id itself as a fake "title"
                caption = (info.get("description") or "").strip()
                write_source_txt(stage_dir, job["url"], platform, title, job["note"],
                                  delete_video=job["delete_video"])
                if caption:
                    # The post's own written caption, distinct from what Whisper hears spoken -
                    # save it now, since if this video is flagged for deletion it (and any
                    # written-only context in the caption) would otherwise be gone for good
                    # once the file goes and only the spoken-word transcript remains.
                    (stage_dir / "caption.txt").write_text(caption + "\n", encoding="utf-8")

            download_ok += 1
            advance_last_update_id(cfg, job["update_id"])

        # transcribe every staging dir that still has a video - this run's fresh downloads
        # AND any left over from a previous run that got interrupted before this phase.
        staging_dirs = sorted(
            d for d in output_root.rglob("_staging_*")
            if d.is_dir() and find_video_file(d)
        )

        if staging_dirs:
            if whisper_bundle is None:
                whisper_bundle = load_whisper(cfg)
            model, device = whisper_bundle

            for i, stage_dir in enumerate(staging_dirs, 1):
                try:
                    meta = read_source_txt(stage_dir)
                    video_path = find_video_file(stage_dir)
                    print(f"[{i}/{len(staging_dirs)}] transcribing: {video_path.name}", flush=True)
                    try:
                        result = transcribe_video(model, device, video_path, stage_dir,
                                                  meta["delete_video"])
                    except RuntimeError as e:
                        if "no audio track" not in str(e):
                            raise
                        # A confirmed structural fact about the file, not a transient failure -
                        # retrying it every run forever would just pile up in _staging_* forever.
                        # Finalize it now instead: kept regardless of delete_video (nothing else
                        # captures its content), named from the caption if there is one since
                        # there's no transcript to name it from.
                        print(f"  no audio track - keeping video, skipping transcript", flush=True)
                        caption_path = stage_dir / "caption.txt"
                        caption = caption_path.read_text(encoding="utf-8") if caption_path.exists() else ""
                        final_name = build_final_name(meta["title"], caption, fallback_id=stage_dir.name)
                        final_dir = unique_dest(stage_dir.parent, final_name)
                        (stage_dir / "video.txt").write_text(
                            "[no audio track - this video has no speech to transcribe]\n",
                            encoding="utf-8")
                        stage_dir.rename(final_dir)
                        silent_videos.append(final_dir.name)
                        if meta["note"]:
                            personal_notes.append(f'{final_dir.name}: "{meta["note"]}"')
                        continue

                    final_name = build_final_name(meta["title"], result["text"],
                                                   fallback_id=stage_dir.name)
                    final_dir = unique_dest(stage_dir.parent, final_name)
                    stage_dir.rename(final_dir)
                    video_path = final_dir / video_path.name

                    moments = find_flagged_moments(video_path, result.get("segments", []),
                                                    flag_keyword_re)
                    if moments and meta["delete_video"]:
                        # screenshot before the video goes - it's about to be unlinked below
                        capture_flagged_screenshots(moments, final_dir)
                        if any(m.get("screenshot") for m in moments):
                            if ocr_engine is None:
                                print("  loading PaddleOCR model...", flush=True)
                                ocr_engine = load_ocr_engine()
                            ocr_flagged_screenshots(ocr_engine, moments)

                    if meta["delete_video"]:
                        for vf in final_dir.glob("video.*"):
                            if vf.suffix.lower() in VIDEO_EXTS:
                                vf.unlink()

                    if moments:
                        write_flagged_moments(final_dir, moments, flag_keywords)
                        print(f"  -> {len(moments)} flagged moment(s) "
                              f"(screenshot/screengrab mentions)", flush=True)
                        flagged_counts.append(f"{final_dir.name}: {len(moments)} moment(s)")

                    if meta["note"]:
                        personal_notes.append(f'{final_dir.name}: "{meta["note"]}"')
                    transcribe_ok += 1
                except Exception as e:
                    print(f"  TRANSCRIBE FAILED ({stage_dir.name}): {e}", flush=True)
                    transcribe_failures.append((stage_dir.name, f"transcription error: {e}"))
                    # left as _staging_* on disk - picked up again next run

    except KeyboardInterrupt:
        print(f"\nCancelled - {download_ok}/{len(jobs)} downloaded, "
              f"{transcribe_ok}/{len(staging_dirs)} transcribed this run, state saved.",
              flush=True)
        return 130

    # --- log + notify ---------------------------------------------------------
    if not jobs and not staging_dirs and not cancel_seen and not skipped and not dropped_warning:
        cfg["last_fetch_at"] = fetch_at.isoformat(timespec="seconds")
        save_config(cfg)
        print("Nothing new.")
        return 0

    # A run can take hours (a long video's download+transcribe) - anything sent to the bot in that
    # window was never in this run's own `updates` fetch, so it's silently left unread rather than
    # dropped (last_update_id was never advanced past it) and will be picked up automatically next
    # run. This just peeks at the same offset again to report how many are waiting, purely
    # informational - it doesn't process or acknowledge them, so nothing here can cause a miss.
    late_updates = []
    try:
        late_updates = tg_get_updates(token, cfg.get("last_update_id", 0) + 1)
    except requests.RequestException as e:
        print(f"  (warning: couldn't check for late-arriving messages: {e})", flush=True)

    all_failures = download_failures + transcribe_failures
    timestamp = dt.datetime.now().isoformat(timespec="seconds")
    log_lines = [
        f"=== {timestamp} ===",
        f"downloaded: {download_ok} ok, {len(download_failures)} failed",
        f"transcribed: {transcribe_ok} ok, {len(transcribe_failures)} failed",
    ]
    if dropped_warning:
        log_lines.append(f"DROPPED BY TELEGRAM: {dropped_warning}")
    for warning in DEVICE_WARNINGS:
        log_lines.append(f"GPU SETUP: {warning}")
    if cancel_seen:
        log_lines.append(f"cancelled: {cancelled_count} queued link(s) discarded (received \"c\")")
    if all_failures:
        log_lines.append("failures:")
        for url, reason in all_failures:
            log_lines.append(f"  - {url} ({reason})")
    if personal_notes:
        log_lines.append("notes:")
        for note_line in personal_notes:
            log_lines.append(f"  - {note_line}")
    if flagged_counts:
        log_lines.append("flagged moments (see flagged_moments.txt in each folder):")
        for line in flagged_counts:
            log_lines.append(f"  - {line}")
    if silent_videos:
        log_lines.append("silent (no audio track, kept without a transcript):")
        for name in silent_videos:
            log_lines.append(f"  - {name}")
    if skipped:
        log_lines.append(f"skipped (no URL found, {len(skipped)}):")
        for preview in skipped:
            log_lines.append(f"  - {preview}")
    if late_updates:
        log_lines.append(f"{len(late_updates)} message(s) arrived after this run started fetching - "
                          f"not yet processed, will be picked up next run")
    log_lines.append("")
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write("\n".join(log_lines) + "\n")
    cfg["last_fetch_at"] = fetch_at.isoformat(timespec="seconds")
    save_config(cfg)

    summary_parts = []
    if dropped_warning:
        summary_parts.append(f"WARNING: {dropped_warning}")
    for warning in DEVICE_WARNINGS:
        summary_parts.append(f"WARNING: {warning}")
    if cancel_seen:
        summary_parts.append(f"Cancelled: {cancelled_count} queued link(s) discarded.")
    if jobs or staging_dirs:
        summary = (f"Downloaded {download_ok}/{len(jobs)} link(s) ok "
                    f"({len(download_failures)} failed). "
                    f"Transcribed {transcribe_ok} ({len(transcribe_failures)} failed).")
        if flagged_counts:
            summary += f"\n{len(flagged_counts)} video(s) had flagged moments - see flagged_moments.txt."
        if silent_videos:
            summary += f"\n{len(silent_videos)} video(s) had no audio track - kept, no transcript."
        if all_failures:
            summary += "\n" + "\n".join(f"- {url} ({reason})" for url, reason in all_failures)
        summary_parts.append(summary)
    if skipped:
        summary_parts.append(f"{len(skipped)} message(s) had no extractable link, skipped - see log.txt.")
    if late_updates:
        summary_parts.append(f"{len(late_updates)} new message(s) came in while this was running, "
                              f"will be picked up next run.")
    if summary_parts:
        tg_send_message(token, cfg["chat_id"], "\n\n".join(summary_parts))

    print("\nDone.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
