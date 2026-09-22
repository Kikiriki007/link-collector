r"""
collect.py - Telegram link -> download -> transcribe collector.

Send a video link (Reel/Short/TikTok/YouTube/etc.) to your Telegram bot. Running this script
(manually or via the daily scheduled task) pulls new messages, downloads each link's video with
yt-dlp, transcribes it with Whisper, and files the pair into a readable folder under
`output_root\<date>\<title>__<transcript-snippet>\`.

Message rule: send *only* the bare link -> the video is kept. Add any other text to the message ->
that text becomes a PERSONAL NOTE (saved in that clip's source.txt and echoed into log.txt) *and*
signals "delete the video after transcribing" (transcript + note stay either way). Send just the
letter "c" -> cancels every link queued in this batch (nothing downloaded, nothing re-queued next
run either) - a remote way to retract a just-sent link without console access.

Usage:
    python collect.py

Requires config.json next to this file (copy config.example.json and fill in your bot token).
See README.md for full setup.
"""
import datetime as dt
import json
import re
import shutil
import sys
from pathlib import Path

import requests
import torch
import whisper
import yt_dlp
from whisper.utils import get_writer

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
LOG_PATH = BASE_DIR / "log.txt"

URL_RE = re.compile(r"https?://\S+")
SAVE_RE = re.compile(r"\bsave\b", re.I)
PRIVATE_KEYWORDS = ("login", "log in", "private", "restrict", "permission", "not available",
                    "n't available", "sign in", "certain audiences", "age-restricted")
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".flv", ".ts"}

# Words that, when the video's own narrator says them, are a strong hint that something on
# screen right then matters - Whisper only catches speech, so this is the cheap way to flag
# "go look at the video here" moments without doing actual frame analysis (yet).
DEFAULT_FLAG_KEYWORDS = ["screenshot", "screenshots", "screengrab", "screengrabs",
                          "screen grab", "screen grabs", "screen shot", "screen shots"]


# --- config -------------------------------------------------------------------
def load_config() -> dict:
    if not CONFIG_PATH.exists():
        print("config.json not found. Copy config.example.json to config.json and fill in your "
              "bot token (see README.md).", file=sys.stderr)
        raise SystemExit(1)
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


# --- telegram -------------------------------------------------------------------
def tg_get_updates(token: str, offset: int) -> list:
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                      params={"offset": offset}, timeout=30)
    r.raise_for_status()
    return r.json()["result"]


def tg_send_message(token: str, chat_id, text: str) -> None:
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                       data={"chat_id": chat_id, "text": text}, timeout=30)
    except requests.RequestException as e:
        print(f"  (warning: failed to send Telegram summary: {e})", flush=True)


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


# --- download -------------------------------------------------------------------
def classify_error(err_text: str) -> str:
    low = err_text.lower()
    if any(k in low for k in PRIVATE_KEYWORDS):
        return "looks private/login-walled"
    return "other error"


def looks_like_image_post(err_text: str) -> bool:
    return "no video formats found" in err_text.lower()


def download_video(url: str, dest_dir: Path, cfg: dict) -> dict:
    dest_dir.mkdir(parents=True, exist_ok=True)
    ydl_opts = {
        "format": "bv*[height<=1080]+ba/b[height<=1080]/best",
        "merge_output_format": "mp4",
        "outtmpl": str(dest_dir / "video.%(ext)s"),
        "quiet": False,
        "retries": 2,
        # YouTube requires solving a JS-obfuscated "n" parameter for some formats; yt-dlp only
        # trusts "deno" for this by default. Add "node" as a fallback since that's commonly
        # already installed (and harmless to list even if neither is present).
        "js_runtimes": {"deno": {}, "node": {}},
        **_yt_dlp_auth_opts(cfg),
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=True)


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
    {'platform', 'caption', 'image_paths': [Path,...], 'video_paths': [Path,...]}.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    base_opts = {"quiet": True, "no_warnings": True, **_yt_dlp_auth_opts(cfg)}

    with yt_dlp.YoutubeDL(base_opts) as ydl:
        info = ydl.extract_info(url, download=False, process=False)

    entries = list(info["entries"]) if info.get("_type") == "playlist" else [info]
    platform = info.get("extractor_key") or info.get("extractor") or "unknown"
    caption = (info.get("description") or "").strip()

    image_paths, video_paths = [], []
    for idx, entry in enumerate(entries, 1):
        if entry.get("formats"):
            try:
                v_opts = {**base_opts, "outtmpl": str(dest_dir / f"video_{idx:02d}.%(ext)s"),
                          "merge_output_format": "mp4"}
                with yt_dlp.YoutubeDL(v_opts) as vydl:
                    vydl.process_ie_result(dict(entry), download=True)
                found = sorted(dest_dir.glob(f"video_{idx:02d}.*"))
                if found:
                    video_paths.append(found[0])
            except Exception:
                pass  # skip this one item rather than failing the whole post
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
        raise ValueError("no downloadable image or video items found in this post")

    return {"platform": platform, "caption": caption,
            "image_paths": image_paths, "video_paths": video_paths}


# --- OCR (for image posts/carousels - on-image text the caption alone misses) ---
def load_ocr_engine():
    # Same engine + settings already proven in Instagram-Autopost/ig-research/reocr_full_corpus.py
    from paddleocr import PaddleOCR
    return PaddleOCR(
        lang="en", use_doc_orientation_classify=False, use_doc_unwarping=False,
        use_textline_orientation=False, enable_mkldnn=False,
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


def find_flagged_moments(segments: list, keyword_re: re.Pattern) -> list:
    """Returns [(start_seconds, matched_word, segment_text), ...] for every segment whose text
    matches one of the flag keywords - Whisper's segments carry real timestamps, unlike the
    plain .txt output, so this is what lets you jump straight to the moment in the video."""
    moments = []
    for seg in segments:
        text = seg.get("text", "")
        m = keyword_re.search(text)
        if m:
            moments.append((seg.get("start", 0.0), m.group(0), text.strip()))
    return moments


def write_flagged_moments(stage_dir: Path, moments: list, keywords: list) -> None:
    lines = [f"{len(moments)} flagged moment(s) found (keywords: {', '.join(keywords)})", ""]
    for start, word, text in moments:
        lines.append(f"[{format_timestamp(start)}] (\"{word}\") {text}")
    (stage_dir / "flagged_moments.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


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


def unique_dest(parent: Path, name: str) -> Path:
    candidate = parent / name
    n = 2
    while candidate.exists():
        candidate = parent / f"{name}-{n}"
        n += 1
    return candidate


# --- main -------------------------------------------------------------------
def main() -> int:
    cfg = load_config()
    token = cfg["bot_token"]
    output_root = Path(cfg["output_root"])
    output_root.mkdir(parents=True, exist_ok=True)

    updates = tg_get_updates(token, cfg.get("last_update_id", 0) + 1)

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
    cancel_seen = False
    for upd in updates:
        update_id = upd["update_id"]
        msg = upd.get("message") or upd.get("channel_post") or {}
        text = msg.get("text") or msg.get("caption") or ""
        if text.strip().lower() == "c":
            # Bare "c", nothing else - a remote cancel signal (e.g. you sent a link by mistake
            # and want to retract it before it's processed). Consumed like any other message so
            # it's never seen again; the whole batch's queued jobs get discarded further down.
            cancel_seen = True
            cfg["last_update_id"] = update_id
            save_config(cfg)
            continue
        url, note, delete_video = find_message_url(msg)
        if not url:
            has_media = bool(msg.get("photo") or msg.get("video") or msg.get("document"))
            preview = text.strip()[:60] or ("[media, no text/caption]" if has_media else "[empty message]")
            skipped.append(preview)
            cfg["last_update_id"] = update_id
            save_config(cfg)
            continue
        jobs.append({"update_id": update_id, "url": url, "note": note, "delete_video": delete_video})

    cancelled_count = 0
    if cancel_seen:
        cancelled_count = len(jobs)
        print(f"Cancel ('c') received - discarding {cancelled_count} queued link(s) from this "
              f"batch, nothing downloaded.", flush=True)
        jobs = []
        # Discarded jobs never ran the per-job save below (that only happens once a job is
        # actually downloaded), so force last_update_id past this whole fetched batch here -
        # otherwise a cancelled link would resurface and get re-queued next run.
        if updates:
            cfg["last_update_id"] = updates[-1]["update_id"]
            save_config(cfg)

    today_dir = output_root / dt.date.today().isoformat()
    download_ok = 0
    download_failures = []   # list of (url, reason)
    transcribe_ok = 0
    transcribe_failures = []  # list of (identifier, reason)
    personal_notes = []
    flagged_counts = []
    staging_dirs = []
    ocr_engine = None  # lazy: loaded on first image post this run, reused after that

    try:
        for i, job in enumerate(jobs, 1):
            print(f"[{i}/{len(jobs)}] downloading: {job['url']}", flush=True)
            stage_dir = today_dir / f"_staging_{job['update_id']}"
            post = None
            try:
                info = download_video(job["url"], stage_dir, cfg)
            except Exception as e:
                if looks_like_image_post(str(e)):
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
                    cfg["last_update_id"] = job["update_id"]
                    save_config(cfg)
                    continue

            if post is not None:
                # Photo post/carousel - no audio to transcribe, so finalize right here rather
                # than deferring to the Whisper phase below. The caption stands in for a
                # transcript, both for naming and as the saved text.
                caption = post["caption"]
                n_img, n_vid = len(post["image_paths"]), len(post["video_paths"])
                print(f"  -> {n_img} image(s), {n_vid} video(s)", flush=True)

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

                lines = [
                    f"URL: {job['url']}",
                    f"PLATFORM: {post['platform']}",
                    f"RECEIVED: {dt.datetime.now().isoformat(timespec='seconds')}",
                    f"CONTENT: {n_img} image(s), {n_vid} video(s)",
                ]
                if job["note"]:
                    lines.append(f"PERSONAL NOTE: {job['note']}")
                (stage_dir / "source.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
                if caption:
                    (stage_dir / "caption.txt").write_text(caption + "\n", encoding="utf-8")

                plain_ocr = " ".join(t for t in ocr_texts if t and not t.startswith("[OCR failed"))
                naming_source = caption if caption.strip() else plain_ocr
                final_name = build_final_name("", naming_source, fallback_id=stage_dir.name)
                final_dir = unique_dest(stage_dir.parent, final_name)
                stage_dir.rename(final_dir)
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
            cfg["last_update_id"] = job["update_id"]
            save_config(cfg)

        # transcribe every staging dir that still has a video - this run's fresh downloads
        # AND any left over from a previous run that got interrupted before this phase.
        staging_dirs = sorted(
            d for d in output_root.rglob("_staging_*")
            if d.is_dir() and list(d.glob("video.*"))
        )

        if staging_dirs:
            device = "cuda" if torch.cuda.is_available() else "cpu"
            model_name = cfg.get("whisper_model", "small")
            print(f"loading Whisper model '{model_name}' ({device})...", flush=True)
            model = whisper.load_model(model_name, device=device)
            flag_keywords = cfg.get("flag_keywords", DEFAULT_FLAG_KEYWORDS)
            flag_keyword_re = build_flag_keyword_re(flag_keywords)

            for i, stage_dir in enumerate(staging_dirs, 1):
                try:
                    meta = read_source_txt(stage_dir)
                    video_path = list(stage_dir.glob("video.*"))[0]
                    print(f"[{i}/{len(staging_dirs)}] transcribing: {video_path.name}", flush=True)
                    result = model.transcribe(str(video_path), fp16=(device == "cuda"),
                                               verbose=False)
                    if meta["delete_video"]:
                        # no video to sync captions to - keep just the plain-text transcript
                        writer = get_writer("txt", str(stage_dir))
                        writer(result, str(video_path), {"max_line_width": 80, "max_line_count": 2})
                    else:
                        writers = get_writer("all", str(stage_dir))
                        writers(result, str(video_path), {"max_line_width": 80, "max_line_count": 2})
                        (stage_dir / "video.json").write_text(
                            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

                    final_name = build_final_name(meta["title"], result["text"],
                                                   fallback_id=stage_dir.name)
                    final_dir = unique_dest(stage_dir.parent, final_name)
                    stage_dir.rename(final_dir)

                    if meta["delete_video"]:
                        for vf in final_dir.glob("video.*"):
                            if vf.suffix.lower() in VIDEO_EXTS:
                                vf.unlink()

                    moments = find_flagged_moments(result.get("segments", []), flag_keyword_re)
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
    if not jobs and not staging_dirs and not cancel_seen and not skipped:
        print("Nothing new.")
        return 0

    all_failures = download_failures + transcribe_failures
    timestamp = dt.datetime.now().isoformat(timespec="seconds")
    log_lines = [
        f"=== {timestamp} ===",
        f"downloaded: {download_ok} ok, {len(download_failures)} failed",
        f"transcribed: {transcribe_ok} ok, {len(transcribe_failures)} failed",
    ]
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
    if skipped:
        log_lines.append(f"skipped (no URL found, {len(skipped)}):")
        for preview in skipped:
            log_lines.append(f"  - {preview}")
    log_lines.append("")
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write("\n".join(log_lines) + "\n")

    summary_parts = []
    if cancel_seen:
        summary_parts.append(f"Cancelled: {cancelled_count} queued link(s) discarded.")
    if jobs or staging_dirs:
        summary = (f"Downloaded {download_ok}/{len(jobs)} link(s) ok "
                    f"({len(download_failures)} failed). "
                    f"Transcribed {transcribe_ok} ({len(transcribe_failures)} failed).")
        if flagged_counts:
            summary += f"\n{len(flagged_counts)} video(s) had flagged moments - see flagged_moments.txt."
        if all_failures:
            summary += "\n" + "\n".join(f"- {url} ({reason})" for url, reason in all_failures)
        summary_parts.append(summary)
    if skipped:
        summary_parts.append(f"{len(skipped)} message(s) had no extractable link, skipped - see log.txt.")
    if summary_parts:
        tg_send_message(token, cfg["chat_id"], "\n\n".join(summary_parts))

    print("\nDone.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
