# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-script personal pipeline: Telegram bot -> yt-dlp download -> Whisper transcription (video)
or PaddleOCR (photo posts/carousels) -> files organized into a dated, readable archive folder. No
LLM/AI step in the pipeline itself - it's deterministic. Everything lives in `collect.py`; there is
no package structure and no tests.

## Commands

```
# Setup (Windows, one-time)
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy config.example.json config.json          # then fill in bot_token

# Run manually
.venv\Scripts\python collect.py

# Run via the same path Task Scheduler uses (also tees output to run.log)
run_collect.bat

# Register/replace the daily scheduled task (default 05:00)
.\setup_task.ps1
.\setup_task.ps1 -Time "22:30"

# Trigger the scheduled task immediately without waiting
Start-ScheduledTask -TaskName "link-collector-daily"
```

There is no lint or test suite in this repo - there's nothing to run beyond `collect.py` itself.
When changing pipeline behavior, the practical way to verify is a manual `python collect.py` run
against a real link sent to the bot (or a leftover `_staging_*` folder for the resume path).

## Architecture

`collect.py` runs as a single pass, structured in phases that all share the *same* `config.json`
state (`last_update_id`), which is what makes manual runs and the scheduled task safe to interleave:

1. **Fetch Telegram updates** (`tg_get_updates`) since `last_update_id`. An `edited_message`/
   `edited_channel_post` update (Telegram sends these for actual edits, but also automatically once
   a link preview attaches to an already-sent URL - no user action needed) is consumed and ignored:
   the original send already produced a job or got logged, so reprocessing it would risk a duplicate
   download. Each remaining message is resolved to a job via `find_message_url` /
   `extract_url_and_note`, which also decides `delete_video` (bare link = keep; link + any other
   text = delete-after-transcribe; the standalone word "save" in that text overrides delete back to
   keep). A bare `"c"` message discards every job accumulated *so far in the loop*
   (`cancelled_count += len(jobs); jobs = []`) and keeps going - it's order-aware within the batch,
   not a whole-batch wipe: links appearing after the "c" in the same fetch are unaffected, since
   Telegram can hand back several unprocessed messages in one `getUpdates` call whenever a run was
   skipped/delayed, so "before/after the c" is about message order, not which run sent them. It also
   never touches anything already mid-download from a prior run. `last_update_id` is advanced via
   `advance_last_update_id` (a `max()`, never regresses) after every message/job, one at a time -
   jobs are collected in this pass but downloaded in a later, separate one, so a plain assignment
   there could move `last_update_id` backwards past a skip/cancel/edit already resolved (and saved)
   later in the same original fetch; `max()` keeps the crash-safety invariant that a mid-batch crash
   never reprocesses or double-processes anything already handled.

2. **Download phase**: for each job, `download_video` (yt-dlp, video path) is tried first. It runs a
   cheap `process=False` pre-flight (skips format-list resolution, the expensive part on YouTube) to
   read `duration`/`extractor_key` before choosing the real format string: long-form YouTube
   (>`LONG_FORM_MIN_SECONDS`) is capped at 720p, everything else (Shorts, other platforms) at 1080p.
   The whole probe+download is retried (`TRANSIENT_ERROR_SUBSTRINGS`: "needs to be reloaded" and
   "http error 403") over `RETRY_BACKOFF_SECONDS`, dropping cookies after the first attempt.
   Root-caused by hand on a real ~20h video that failed 100% of the time with `cookies_from_browser`
   configured and 0% of the time without it, same video, same moment: `cookiesfrombrowser` reads the
   browser's live cookie database directly, and doing that while the browser is actually open and
   writing to it can return a stale/locked snapshot that makes YouTube reject the session - not a
   generic hiccup and not time-based (a longer wait alone never fixed it), so dropping cookies on
   retry is the actual fix, not just backoff. If the content genuinely needs cookies (private/
   login-walled), dropping them just surfaces that as its own, correctly-classified failure.
   "http error 403" showed up separately on the same video, at the actual data-fetch stage after a
   format was already selected (a signed-CDN-URL/session mismatch, not a real permission error -
   those surface earlier, during extraction) - re-extracting on retry gets a fresh URL. Any other
   error fails on the first attempt. If yt-dlp
   reports "no video formats found", it's treated as a photo post/carousel and retried via
   `download_post`, which extracts info without downloading, then pulls each carousel entry as
   either a direct image fetch (CDN thumbnail URL) or a yt-dlp video download for any video mixed
   into the carousel. Photo posts are *finalized immediately* in this phase (OCR via PaddleOCR,
   `source.txt`, `caption.txt`, rename out of `_staging_*`) since there's no Whisper step for them.
   Videos mixed into a carousel (`video_NN.*`, NN = position in the post) are transcribed inline here
   too, via the same lazily-loaded Whisper model and `transcribe_video` helper the staging phase
   uses - they never enter `_staging_*` resume flow, so an interrupted carousel job is simply redone
   from scratch next run. The same `delete_video` flag applies to carousel media: images are
   unlinked after OCR and videos after transcribing, except any item whose OCR/transcription failed.
   Failed carousel video downloads are reported via `video_errors` into `download_failures`.
   Video jobs instead write `source.txt`/`caption.txt` into a `_staging_<update_id>` folder and defer
   transcription to phase 3.

3. **Transcription phase**: globs `output_root/**/​_staging_*` for **every** folder that still has a
   `video.*` in it - not just this run's fresh downloads, but also anything left mid-pipeline from a
   previous run that crashed or was Ctrl+C'd between download and transcribe. This is the resume
   mechanism: a `_staging_*` folder with a video file is unfinished work by definition, regardless of
   which run created it. `read_source_txt` re-derives the job's metadata (note, delete_video, title)
   from the `source.txt` written in phase 2, since the in-memory `jobs` list doesn't span runs.
   Transcript segments are also scanned for "screenshot"/"screengrab"/etc keyword hits
   (`find_flagged_moments`, returning moment dicts: `video_path`/`start`/`word`/`text`) to produce
   `flagged_moments.txt`, since Whisper only captures speech, not on-screen content. If the video is
   also flagged for deletion (`delete_video`), this is no longer just a manual-review pointer: before
   the video is unlinked, `capture_flagged_screenshots` grabs one actual frame per moment via ffmpeg
   (input-side `-ss`, a fast keyframe seek - not frame-exact, but hours-long video makes output-side
   seeking too slow), downscaled to `SCREENSHOT_WIDTH` (960px), saved as `flagged_NN_HH-MM-SS.jpg`
   next to `flagged_moments.txt`. Each screenshot is then run through the same PaddleOCR engine used
   for photo posts (`ocr_flagged_screenshots`, lazily loaded on first use same as the carousel path),
   and the extracted text is appended under that moment's line. A failed grab (e.g. a timestamp at
   the very end of the file) just leaves that one moment as a text-only entry rather than failing the
   run - `capture_flagged_screenshots`/`ocr_flagged_screenshots` mutate each moment dict in place
   (`screenshot`, `ocr_text` keys) rather than returning parallel lists, since gaps from per-moment
   failures would otherwise be easy to misalign. Kept videos (delete_video=False) skip this entirely
   - there's a real video file to go re-watch, nothing to compensate for. The same applies to videos
   mixed into a carousel in phase 2 (per-video, since a carousel can hold more than one).

4. Folder naming (`build_final_name`) always prefers a slug of the actual transcript/caption/OCR
   text over the platform title, because platform titles are frequently boilerplate or missing
   entirely (common on undescribed Reels) - the content itself is a more useful folder name than
   metadata the platform may not even provide.

5. Everything is logged to two places with different lifetimes: console output (plus `run.log` via
   `run_collect.bat`'s `Tee-Object`, for the invisible scheduled-task run) and `log.txt` (append-only
   history: counts, failures classified via `classify_error` as either
   "looks private/login-walled" or "other error", and every personal note - the durable record even
   after individual clip folders might later be pruned by hand). A Telegram summary message is also
   sent back to the user at the end of each run. Just before building that summary, it also peeks at
   `tg_get_updates` again at the same offset (`cfg["last_update_id"] + 1`) purely to count anything
   that arrived after this run's own fetch (a multi-hour transcription leaves a wide window) -
   read-only, since `last_update_id` is never advanced past what this run actually processed, so
   nothing it sees here is skipped later; it's reported in both `log.txt` and the Telegram summary
   ("N new message(s) came in while this was running, will be picked up next run") so a link sent
   mid-run doesn't look lost.

`config.json` (gitignored, real secrets/state) vs `config.example.json` (committed template) is the
only config split; there's no environment-variable or CLI-flag configuration.
