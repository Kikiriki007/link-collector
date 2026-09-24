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

1. **Fetch Telegram updates** (`tg_get_updates`) since `last_update_id`. Each message is resolved to
   a job via `find_message_url` / `extract_url_and_note`, which also decides `delete_video` (bare
   link = keep; link + any other text = delete-after-transcribe; the standalone word "save" in that
   text overrides delete back to keep). A bare `"c"` message cancels every job queued *in that same
   batch* (see `cancel_seen` handling) - it does not affect anything already mid-download from a
   prior run. `last_update_id` is advanced and saved after every message/job, one at a time, so a
   crash mid-batch never reprocesses or double-processes anything already handled.

2. **Download phase**: for each job, `download_video` (yt-dlp, video path) is tried first. If yt-dlp
   reports "no video formats found", it's treated as a photo post/carousel and retried via
   `download_post`, which extracts info without downloading, then pulls each carousel entry as
   either a direct image fetch (CDN thumbnail URL) or a yt-dlp video download for any video mixed
   into the carousel. Photo posts are *finalized immediately* in this phase (OCR via PaddleOCR,
   `source.txt`, `caption.txt`, rename out of `_staging_*`) since there's no Whisper step for them.
   The same `delete_video` flag applies: images are unlinked after OCR (except any whose OCR failed);
   videos mixed into a carousel are never transcribed, so they're always kept.
   Video jobs instead write `source.txt`/`caption.txt` into a `_staging_<update_id>` folder and defer
   transcription to phase 3.

3. **Transcription phase**: globs `output_root/**/​_staging_*` for **every** folder that still has a
   `video.*` in it - not just this run's fresh downloads, but also anything left mid-pipeline from a
   previous run that crashed or was Ctrl+C'd between download and transcribe. This is the resume
   mechanism: a `_staging_*` folder with a video file is unfinished work by definition, regardless of
   which run created it. `read_source_txt` re-derives the job's metadata (note, delete_video, title)
   from the `source.txt` written in phase 2, since the in-memory `jobs` list doesn't span runs.
   Transcript segments are also scanned for "screenshot"/"screengrab"/etc keyword hits
   (`find_flagged_moments`) to produce `flagged_moments.txt`, timestamped for manual review since
   Whisper only captures speech, not on-screen content.

4. Folder naming (`build_final_name`) always prefers a slug of the actual transcript/caption/OCR
   text over the platform title, because platform titles are frequently boilerplate or missing
   entirely (common on undescribed Reels) - the content itself is a more useful folder name than
   metadata the platform may not even provide.

5. Everything is logged to two places with different lifetimes: console output (plus `run.log` via
   `run_collect.bat`'s `Tee-Object`, for the invisible scheduled-task run) and `log.txt` (append-only
   history: counts, failures classified via `classify_error` as either
   "looks private/login-walled" or "other error", and every personal note - the durable record even
   after individual clip folders might later be pruned by hand). A Telegram summary message is also
   sent back to the user at the end of each run.

`config.json` (gitignored, real secrets/state) vs `config.example.json` (committed template) is the
only config split; there's no environment-variable or CLI-flag configuration.
