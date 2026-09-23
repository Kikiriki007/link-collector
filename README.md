# link-collector

Send a video link (Instagram Reel, YouTube Short, TikTok, or basically anything
[yt-dlp](https://github.com/yt-dlp/yt-dlp) supports) to a private Telegram bot from your phone.
Once a day (or whenever you run it by hand), it downloads the video and transcribes it with
[Whisper](https://github.com/openai/whisper), filing the pair into a readable folder — building up
a personal, searchable archive of clips + text.

Photo posts and carousels work too: images are downloaded directly and run through
[PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) to pull out any on-image text (the actual
content of most infographic/tip-list posts, which the caption alone usually misses), alongside the
post's own caption. No Whisper step for these - there's no audio.

No AI/LLM step in the pipeline itself — it's a deterministic download-and-transcribe job. You don't
need Claude or any API key besides your own Telegram bot token.

> Prefer to have an AI walk you through setup instead of following the steps below by hand? Open
> this folder in Claude Code (or hand it these instructions) and say "set up link-collector for me."
> Everything below is also written to be followed manually, with no AI involved at all.

## Requirements

- **Python 3.10+**, **[ffmpeg](https://ffmpeg.org/download.html)** on your PATH (for both
  yt-dlp and Whisper), and a Telegram account.
- **[Node.js](https://nodejs.org/)** on your PATH — only needed for YouTube links (yt-dlp uses it
  to solve a JS-obfuscated parameter some formats require). Not needed for Instagram/TikTok.
- **~2 GB free disk**: ~1.5 GB for dependencies (PyTorch for Whisper, PaddlePaddle for OCR - the
  two ML frameworks are most of that), plus ~600 MB for model weights, downloaded once on first
  use and cached under your user profile. Your archive folder grows on top of that with every
  video you keep.
- **CPU-only is fine** for occasional/personal use - no GPU required. An NVIDIA GPU with a CUDA
  build of PyTorch (installed yourself, see [pytorch.org](https://pytorch.org/get-started/locally/))
  transcribes faster, but that's optional.
- The daily-automation piece (`setup_task.ps1`, Windows Task Scheduler) is **Windows-only**;
  `collect.py` itself is plain Python and runs fine by hand on macOS/Linux, or via cron there.
- ~5 minutes for manual setup, see below 
## What you get

For every link you send, once processed:

```
Video Transcripts/
  2026-09-12/
    big-buck-bunny__hey-everyone-welcome-back-to-my-channel/
      video.mp4                 # the downloaded clip (unless you flagged it for deletion - see below)
      video.txt / .srt / .vtt / .json   # the transcript, several formats
      caption.txt                # the platform's own written caption/description, if it had one
      flagged_moments.txt        # only if the narrator said "screenshot"/etc - see below
      source.txt                 # the URL, platform, timestamp, and your personal note if any

    your-exam-is-7-days-away-stop/      # a photo post/carousel instead
      image_01.jpg ... image_09.jpg     # every image in the post
      caption.txt                       # the post's own caption
      ocr.txt                           # on-image text, one labeled block per image
      source.txt                        # URL, platform, timestamp, item counts, note if any
```

Folders are named from the video's own title plus a snippet of its transcript (or, for a photo
post, its caption - falling back to the OCR text when there's no caption at all), so the collection
stays browsable even when a platform gives no real title (common for undescribed Reels).

`caption.txt` holds whatever the platform itself gave as the post's description/caption - separate
from `video.txt` (what Whisper heard spoken). Only written when the platform actually provided one
(common on YouTube, hit-or-miss on Reels/Shorts). Worth keeping distinct from the transcript: a
deleted-after-transcribe video whose narrator says little on-mic would otherwise leave nothing
behind but a near-empty transcript, even if the caption itself had the real context.

## Rules to remember

- Send **just the bare link** → the video is **kept**, no note.
- Add **any other text** to that same message → the video is **deleted right after transcribing**
  (transcript + link stay), and that extra text is saved as a **personal note**.
- Add the standalone word **"save"** anywhere in that extra text → the video is **kept anyway**,
  and any remaining text (besides the word "save" itself) is still saved as the note. Use this when
  you want to both leave yourself a note *and* keep the clip — expected to be rare, so it's an
  opt-in word rather than the default.

A personal note is saved twice, clearly labeled — once in that clip's `source.txt`
(`PERSONAL NOTE: ...`), and once echoed into `log.txt` so you (or an AI pass over your collection
later) can find every note without opening every folder.

Word-boundary matched, so "save" only matches the standalone word — "saved"/"saving"/etc. still
trigger the normal delete-after-transcribe behavior.

### Changed your mind? Send "c"

Send a message that's **just the letter c** (nothing else) to cancel — every link waiting to be
processed in that batch (including ones sent earlier the same run, e.g. a link you sent by mistake
right before) gets discarded and marked as seen, so none of them get downloaded and none of them
come back on the next run either. Handy since this is meant to run unattended (the daily 6am task) -
you don't need console access to call something off, just send "c" from your phone before it runs.
You'll still get a Telegram confirmation of what was discarded.

It only cancels newly-queued links, not anything already mid-download from a previous run.

## Flagged moments — catching what's on screen, not just what's said

Whisper only transcribes speech, so anything shown on screen but not narrated (a slide, a URL, a
settings page) is otherwise invisible to the collection. As a cheap stand-in for real frame
analysis, every video's transcript is scanned for the narrator saying **"screenshot(s)"**,
**"screengrab(s)"**, **"screen grab(s)"**, or **"screen shot(s)"** — a strong verbal cue that
something on screen right then matters. Any match writes a `flagged_moments.txt` in that video's
folder: a count, plus one `[MM:SS] ("matched word") <what was said>` line per hit, using Whisper's
real segment timestamps so you can jump straight to that point in the video and grab it yourself.
The Telegram summary and `log.txt` both mention when a video had flagged moments, so you don't have
to open every folder to find out. Customize the keyword list via `"flag_keywords"` in `config.json`
(defaults shown in `config.example.json`).

This is a manual-search aid, not automatic frame-grabbing — actually pulling a screenshot from the
video at that timestamp would be the natural next step if this proves useful.

## Setup

### 1. Get the code and install dependencies

```
git clone <this repo's URL>
cd link-collector
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt        # Windows
# source .venv/bin/activate && pip install -r requirements.txt   # macOS/Linux
```

That installs `yt-dlp`, `openai-whisper`, `paddleocr`/`paddlepaddle`, and `requests` — see
Requirements above for what that costs in disk space. Model weights download on first use; that
first run will be slower than every run after it.

### 2. Create your Telegram bot

1. Open Telegram, message **@BotFather**, send `/newbot`, follow the prompts.
2. Copy the token it gives you.
3. `copy config.example.json config.json` and paste your token into `"bot_token"`.
4. Send your new bot **any message** from your phone (e.g. "hi") — this is just so the script has
   something to read your chat ID off of.
5. Run it once:
   ```
   .venv\Scripts\python collect.py
   ```
   It'll print `Detected chat_id: ...` and save it into `config.json`. From now on it'll message
   *you* back, and only you.

### 3. Try it

Share a public Reel/Short link to your bot from your phone (native Share sheet → search for your
bot's name, or paste the link directly in the chat). Then run:

```
.venv\Scripts\python collect.py
```

You'll see live download progress, then transcription progress, then a summary reply arrives in the
Telegram chat — which shows up as a normal push notification on your phone, same as any DM, as long
as notifications for that chat aren't muted.

Cancel anytime with **Ctrl+C** — it's safe; nothing partial is left looking finished, and the next
run picks up exactly where it left off.

### 4. (optional) Run it automatically, once a day (Windows only)

```
.\setup_task.ps1                 # defaults to 05:00 daily
.\setup_task.ps1 -Time "22:30"    # or pick your own time
```

This registers a Task Scheduler entry named `link-collector-daily`. You can still run
`python collect.py` (or double-click `run_collect.bat`) by hand any time in between — both share the
same state, so nothing gets double-processed.

To test the scheduled task immediately without waiting for its trigger time:
```
Start-ScheduledTask -TaskName "link-collector-daily"
```

## Notes on failures

If a link fails, it's never silently dropped — it shows up in both `log.txt` and the Telegram summary
with a reason, classified as either **"looks private/login-walled"** or **"other error"**.

The first category is broader than it sounds: Instagram in particular increasingly requires a logged-in
session to serve Reels at all, even ones that are genuinely public (viewable in a browser, sometimes
with a "log in to see more" banner that still plays the video) — its own error text doesn't distinguish
that from an actually private/age-restricted post. If you're hitting this on content you know is
public, see cookies below.

### Optional: cookies, for content that needs a login session

By default this tool makes fully anonymous requests. To let it fetch anything your own logged-in
browser can see, set **one** of these in `config.json`:

- `"cookies_from_browser": "chrome"` (or `"edge"`, `"firefox"`, `"brave"`, `"opera"`, `"vivaldi"`,
  `"safari"`, `"whale"`, `"chromium"`) — yt-dlp reads live from that browser's own cookie store.
  Simplest option, nothing to re-export, but the browser sometimes needs to be closed for it to read
  the file, and works with whatever account you're logged into there.
- `"cookies_file": "cookies.txt"` — a manually exported Netscape-format cookies file (e.g. via the
  "Get cookies.txt LOCALLY" browser extension). More setup, but decoupled from your daily browser
  session. **These cookies expire and need periodic re-export.**

Either way: **this uses your real logged-in identity** to fetch content, and the resulting file/cookie
access is sensitive — `cookies.txt` is already gitignored, never commit it or `config.json`.

## Known limitation: single posts only, no "follow this page"

Every link is a single post/reel/short/carousel - there's no way to point this at an Instagram or
TikTok profile and have it pull that account's posts a few at a time. That's not a design choice:
yt-dlp's own page-listing extractors are currently broken for both platforms (`instagram:user` fails
to parse the profile page; `tiktok:user` can't resolve a plain `@username` URL to its internal
channel ID), even on the latest release. YouTube channel listing works fine via yt-dlp if that's
ever worth wiring up - Instagram/TikTok would need yt-dlp to fix those extractors first.

## Files

| File | Purpose |
|---|---|
| `collect.py` | The whole pipeline. Also the manual entry point (`python collect.py`). |
| `config.json` | Your bot token, chat ID, output folder, and sync state. Not committed to git. |
| `config.example.json` | Template for the above. |
| `run_collect.bat` | Thin wrapper so both Task Scheduler and a manual double-click run the same way. |
| `setup_task.ps1` | One-time script to register the daily scheduled task. |
| `log.txt` | Append-only run history — counts, failures, and every personal note. Not committed. |

## License

[MIT](LICENSE)
