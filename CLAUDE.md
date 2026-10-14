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
state (`last_update_id`), which is what makes manual runs and the scheduled task safe to interleave
- but only because they don't actually overlap in time. Before anything else, `main()` calls
`acquire_lock()`, which grabs an OS-level exclusive lock on `collect.lock` (`msvcrt.locking` on
Windows, `fcntl.flock` elsewhere) and exits immediately (code 1, no work done) if another instance
already holds it. This matters because a manual run and the scheduled task *can* genuinely overlap
- e.g. the task's `StartWhenAvailable` catching up on a missed trigger right as someone runs it by
hand - and without this, two instances would race to download into the same deterministic
`_staging_<update_id>` folder (derived from the message's own `update_id`, not randomized per
process), double up on Whisper/PaddleOCR load, and last-write-wins on `config.json`. The lock is
held by keeping the file object open as a local variable for `main()`'s entire body - no explicit
release needed, and crucially no stale-lock cleanup logic either: the OS drops the lock the moment
the process exits for *any* reason, including a hard kill, which a plain PID-in-a-file scheme
can't offer (verified by hard-killing a lock holder mid-run and confirming a new instance could
acquire it immediately after, with no leftover state to detect or clear).

1. **Fetch Telegram updates** (`tg_get_updates`) since `last_update_id`. An `edited_message`/
   `edited_channel_post` update (Telegram sends these for actual edits, but also automatically once
   a link preview attaches to an already-sent URL - no user action needed) is consumed and ignored:
   the original send already produced a job or got logged, so reprocessing it would risk a duplicate
   download. Each remaining message is resolved to a job via `find_message_url` /
   `extract_url_and_note`, which also decides `delete_video` (bare link = keep; link + any other
   text = delete-after-transcribe; the standalone word "save" in that text overrides delete back to
   keep). If no URL is found, `find_message_audio` checks for a voice note, audio file, or a
   `document` that's actually audio (by mime type or extension against `AUDIO_EXTS`) and produces a
   `"kind": "audio"` job instead of `"kind": "url"` - same keep/delete/note rules, just driven by
   the message's caption directly since there's no URL substring to strip out first. A bare `"c"` message discards every job accumulated *so far in the loop*
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
   The whole probe+download is retried (`TRANSIENT_ERROR_SUBSTRINGS`: "needs to be reloaded",
   "http error 403", "video unavailable") over `RETRY_BACKOFF_SECONDS`, dropping cookies after the
   first attempt. Root-caused by hand on a real ~20h video that failed 100% of the time with
   `cookies_from_browser` configured and 0% of the time without it, same video, same moment:
   `cookiesfrombrowser` reads the browser's live cookie database directly, and doing that while the
   browser is actually open and writing to it can return a stale/locked snapshot that makes YouTube
   reject the session - not a generic hiccup and not time-based (a longer wait alone never fixed
   it), so dropping cookies on retry is the actual fix, not just backoff. If the content genuinely
   needs cookies (private/login-walled), dropping them just surfaces that as its own,
   correctly-classified failure. "http error 403" showed up separately on the same video, at the
   actual data-fetch stage after a format was already selected (a signed-CDN-URL/session mismatch,
   not a real permission error - those surface earlier, during extraction) - re-extracting on retry
   gets a fresh URL. "video unavailable" is a third symptom of the same stale-cookie-DB cause,
   isolated the same way on a different real video: failed 100% of the time with cookies, 0% of the
   time without, same URL, same moment - not an actually-deleted/region-blocked video, which would
   fail identically either way. A real failure here (and in the two cases above) gets
   `advance_last_update_id`'d like any other processed job, so a dead link doesn't retry forever on
   its own - the link has to be resent to actually get reprocessed once whatever was wrong is fixed.
   Any other error fails on the first attempt. If yt-dlp
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
   `"kind": "audio"` jobs are handled separately at the very top of this same loop, before any of
   the above: `tg_download_file` pulls the file straight from Telegram's file API (`getFile` then
   the `.../file/bot<token>/<path>` URL) into `_staging_<update_id>/audio<ext>`, then
   `transcribe_video` runs on it directly - no yt-dlp, no OCR, and (like carousels) no `_staging_*`
   resume, since these are short and an interrupted one is just redone from scratch next run.
   `language=cfg.get("whisper_language")` is passed here specifically (every other `transcribe_video`
   call site omits it, keeping Whisper's normal auto-detect) - sent to `model.transcribe` as-is, so
   `None` (the default, nothing configured) is the same as not passing it at all. Finalized the same
   way as a single video (naming, delete rule, flagged-moment text scan - never screenshotted, since
   there's no video to grab a frame from) immediately in this phase, with its own minimal
   `source.txt` (`SOURCE: telegram audio (<original filename>)`, `AUDIO: keep|deleted-after-transcribe`)
   instead of the URL/platform fields a real link would have. `tg_download_file` surfaces Telegram's
   actual JSON `description` on a non-200 `getFile` response instead of `raise_for_status()`'s
   generic "400 Client Error" - needed in practice: the regular Bot API hard-caps file downloads at
   20 MB (`"Bad Request: file is too big"`), hit for real on a ~20-minute voice note, and that's the
   one piece of information actually worth showing in `log.txt`/the Telegram summary.
   `tg_api_base(cfg)` picks `api.telegram.org` unless `"local_bot_api_url"` is set, in which case
   `tg_download_file`'s `getFile` call - and *only* that call - goes through a self-hosted server
   instead (`local-bot-api/` - optional, dormant unless configured, see its own README).
   `getUpdates`/`sendMessage` deliberately always use the real API regardless of this setting -
   root-caused by hand after a real crash left a stray `_staging_<id>` folder with a number that
   matched nothing: a self-hosted local server assigns its *own* `update_id` sequence, completely
   disconnected from the real API's numbering for the exact same messages (the same file was
   `958651485` there vs. `649679817` here), and it even dropped 2 of 6 real pending messages
   outright. `last_update_id` has to mean the same thing every run - a stored local-server value
   would make the real API's offset-based `getUpdates` stop seeing new messages forever once this
   is turned off again, since its own numbering will never catch up to one that high. `file_id`,
   unlike `update_id`, is confirmed portable between the two servers (confirmed by hand: a
   `file_id` from a real `getUpdates` call resolved fine through the local server's `getFile`),
   which is exactly why only that one call needs to route through it. That's the only thing that
   actually lifts the 20MB/50MB caps above - a self-hosted server run with
   `TELEGRAM_LOCAL=1` serves unlimited downloads / 2000MB uploads. In that mode `getFile`'s
   `file_path` stops being a relative path to download over HTTP and becomes an absolute path
   *inside that server's own container* (`LOCAL_BOT_API_CONTAINER_DIR`, `/var/lib/telegram-bot-api`)
   - confirmed by hand that the old HTTP route simply 404s once local mode is on, so
   `tg_download_file` detects that prefix and pulls the file out with `docker cp
   <container>:<path> <dest>` instead (`"local_bot_api_container_name"`, defaulting to
   `LOCAL_BOT_API_CONTAINER_NAME` - both read with `or`, not `.get(key, default)`, since
   `config.example.json` documents the key as an explicit `null` rather than leaving it out, and
   `.get` only falls back to its default on a *missing* key, not an explicit `None`). `docker cp`
   works regardless of the container's own storage setup, which is why `docker-compose.yml` uses a
   named volume rather than a host bind-mount for `/var/lib/telegram-bot-api` - confirmed by hand
   that a bind-mount there crashes the server outright (SIGABRT) the moment it tries to create a
   bot's data directory, since that directory is named after the literal bot token, which contains
   a colon (not a valid path character on Windows). This whole path - real `api_id`/`api_hash`,
   real container, real `sendDocument` → `getFile` → `docker cp` round trip - has been run for real
   and confirmed working, not just mocked.
   Nothing Docker-related is left running between batches - not just the container, Docker Desktop
   itself. Right after `cfg` is loaded, if `"local_bot_api_url"` is set, `main()` calls
   `start_local_bot_api`, which first calls `ensure_docker_desktop_running(cfg)`: a cheap `docker
   info` checks whether the daemon is even reachable, and if not, launches
   `"docker_desktop_exe"` (default `DOCKER_DESKTOP_EXE`) and polls until it is. This matters for a
   fully unattended run - PC off overnight, turned on once and left alone - since Docker Desktop's
   own "start at login" setting isn't enabled here, and even a machine that does enable it can
   still have the scheduled task's missed-trigger catch-up fire while Docker Desktop is still
   mid-launch (confirmed by hand: a real run from a fully-quit Docker Desktop recovered on its own
   in ~11s, versus ~4s when it was already running). Only then does `docker start` run on the
   container itself (idempotent either way), followed by the same base_url readiness poll as
   before (`docker start` returns before the server inside is actually listening - confirmed by
   hand, "Remote end closed connection" every time without this). Teardown is registered in the
   opposite order from how you'd read it, because `atexit` runs LIFO:
   `atexit.register(stop_docker_desktop)` first, then `atexit.register(stop_local_bot_api,
   container)` second, so `stop_local_bot_api` (a clean `docker stop`) actually *fires* first,
   before `stop_docker_desktop` (`taskkill /IM "Docker Desktop.exe" /F`) yanks the whole backend
   out from under it. Confirmed by hand end-to-end from a cold start: Docker Desktop fully killed
   beforehand, daemon unreachable, real run launched it, waited for the daemon, did real Telegram
   work through the container, then killed both the container and Docker Desktop again
   afterward - nothing left running. Every failure mode along this path (`docker` not found,
   Docker Desktop exe missing, daemon never comes up, container never becomes reachable) raises
   and `return 1`s before any Telegram call is attempted, rather than cascading into a run full of
   connection errors.

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
   `transcribe_video` also catches the specific case of a silent video (no audio stream at all -
   some Reels/carousel clips genuinely have none): Whisper's ffmpeg call fails on those with its
   entire compiled-in banner crammed into the exception text, thousands of characters for what's
   really just "no audio here" - caught by `"does not contain any stream" in str(e)` and re-raised
   as a short `"no audio track (silent video)"` instead. Root-caused by hand: a real run hit this on
   three clips at once, and the stacked raw ffmpeg-banner failures pushed the end-of-run Telegram
   summary over Telegram's 4096-char limit - see phase 5.
   In the single-video staging loop specifically, that same error is caught a second time, one
   level up, and treated as final rather than retry-worthy: a silent video is a fixed fact about
   the file, not a transient failure, so leaving it in `_staging_*` would just re-fail and re-log
   the same way every run forever. It's finalized immediately instead - kept regardless of
   `delete_video` (nothing else captures its content), named from `caption.txt` if there is one
   since there's no transcript to name it from, with a placeholder `video.txt` explaining why
   there's no real transcript. Reported separately from both `transcribe_ok` and
   `transcribe_failures` (a distinct "silent (no audio track, kept without a transcript):" section
   in `log.txt` and the Telegram summary), since it's neither a success nor an ongoing problem.
   Carousel videos don't need this: a carousel is always finalized in phase 2 regardless of any
   one video's transcribe outcome (a failed one is simply excluded from `deletable_videos`, so it's
   kept automatically), and carousels never re-enter `_staging_*` resume flow anyway.

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
   mid-run doesn't look lost. `tg_send_message` also checks `r.raise_for_status()` now rather than
   only catching connection-level errors - Telegram rejects an over-limit message (>4096 chars) with
   a plain 400 response, not a network exception, so without this a run could finish and log
   normally while the summary silently never arrived. Caught by hand: the silent-video failure above
   was verbose enough, stacked with a couple other failures in the same run, to trigger exactly that.

`config.json` (gitignored, real secrets/state) vs `config.example.json` (committed template) is the
only config split; there's no environment-variable or CLI-flag configuration.
