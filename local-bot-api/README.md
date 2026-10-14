# Local Bot API server (optional)

Telegram's regular Bot API won't let a bot download a file over **20MB** or upload one over
50MB - a hard server-side limit, not something `collect.py` can retry past. Running your own copy
of Telegram's own Bot API server removes that cap entirely (unlimited download, 2000MB upload).

**You don't need this unless a file is actually too big.** `collect.py` works exactly as it always
has until you deliberately turn this on - nothing about normal links/photos/videos changes.

## One-time setup

1. **Get your own API credentials** (separate from the bot token) at
   [my.telegram.org](https://my.telegram.org) - log in with your own Telegram account, go to
   "API development tools", and create an app. Any name/platform is fine; you'll get an
   `api_id` and `api_hash`.
   - If you hit a generic "ERROR" popup on submit even with every field filled in correctly, the
     page's submit token has likely gone stale from earlier failed attempts - hard refresh
     (Ctrl+Shift+R) and submit once on a clean load.
2. **Install [Docker Desktop](https://www.docker.com/products/docker-desktop/)** if you don't
   have it, and make sure it's running.
3. In this folder: `copy .env.example .env`, then fill in `TELEGRAM_API_ID`/`TELEGRAM_API_HASH`.
4. Create the container once:
   ```
   docker compose up -d
   ```
5. Point `collect.py` at it by adding this to `config.json` (one level up):
   ```json
   "local_bot_api_url": "http://127.0.0.1:8081"
   ```

**You don't need to keep anything running between runs - not even Docker Desktop itself.**
`collect.py` manages the whole thing automatically at the top of every run:

1. Checks if Docker's daemon is reachable; if not, launches Docker Desktop and waits for it
   (handles a fully cold machine - PC off overnight, turned on once and left alone - even though
   Docker Desktop's own "start at login" setting isn't enabled).
2. Starts the container (`docker start`, harmless no-op if it's somehow already running) and polls
   it until it's actually accepting connections - `docker start` returns well before the server
   inside is ready, confirmed by hand that the first real request right after a bare start
   reliably fails.
3. Runs normally.
4. When the run ends (success, failure, or Ctrl+C), stops the container with a clean `docker stop`,
   then kills Docker Desktop entirely (`taskkill`).

Confirmed by hand end-to-end from a genuinely cold start: Docker Desktop fully quit, daemon
unreachable, a real run launched it, waited, did real Telegram work, and tore everything back down
again afterward - nothing Docker-related left running. `docker compose up -d` is only ever needed
once, manually, the very first time, to create the container at all.

That's it - but **only file downloads actually route through your local server, not everything**.
`getUpdates`/`sendMessage` always use the real `api.telegram.org`, no matter what this is set to.
Only `tg_download_file`'s `getFile` call uses the local server, and only to pull large files out
with `docker cp` instead of downloading over HTTP.

This split isn't an oversight - it's a fix for a real bug. The local server assigns its **own**
`update_id` numbering for incoming messages, completely disconnected from the real API's numbers
for the exact same messages (caught by hand: the identical file was `update_id` `958651485` on the
local server vs. `649679817` on the real API - and the local server even silently dropped 2 of 6
real pending messages outright). `collect.py` tracks which messages it's already processed as a
single `last_update_id` number in `config.json`, and that number has to mean the same thing every
run. If `getUpdates` ever routed through the local server, a run could save a local-server-style
`update_id` into `config.json` - and the next time you point back at the real API (or just restart
the container, which starts the local server's numbering over), that saved number would be
meaningless there, silently and permanently: the real API would wait forever for its own counter
to climb to a number it'll never reach, so `collect.py` would stop seeing new messages with no
error at all. `file_id` doesn't have this problem - confirmed by hand that a `file_id` obtained
from the real API resolves just fine through the local server's `getFile` - which is exactly why
only that one call is safe to route through it.

For large files specifically: that's what `TELEGRAM_LOCAL=1` changes about `getFile` - it returns
an absolute path *inside the container's own filesystem* instead of a URL, and the old HTTP
download route simply stops working in this mode (confirmed by hand: it 404s). `docker cp` sidesteps
needing that HTTP route at all, which is also why the volume below is a named volume rather than a
bind-mount straight to this folder: `telegram-bot-api` names each bot's data directory after the
literal bot token, which contains a colon - not a valid character in a Windows path, so a host
bind-mount here actually crashes the server outright the moment it tries to create that directory
(confirmed by hand). A named volume lives inside Docker's own Linux VM instead, where that's a
non-issue.

If this repo gets moved, or the container is ever renamed in `docker-compose.yml`, update
`"local_bot_api_container_name"` in `config.json` too (defaults to `link-collector-bot-api`,
matching the compose file). If Docker Desktop is installed somewhere other than the default
`C:\Program Files\Docker\Docker\Docker Desktop.exe`, set `"docker_desktop_exe"` to the real path -
that's what gets launched when the daemon isn't already reachable.

## Turning it off

Delete (or just blank out) `local_bot_api_url` in `config.json` - everything reverts to talking to
`api.telegram.org` directly. `docker compose down` stops the server; `docker compose down -v` also
removes its data volume (every file the bot has ever sent/received through it) if you want a clean
slate, which is otherwise harmless to leave sitting there.

## Checking it's actually working

Since `collect.py` now starts/stops it itself, `docker compose ps` showing it **Exited** between
runs is normal, not a problem - it should only show **Up** while a `collect.py` run is actually in
progress. To watch a run use it live:
```
docker compose ps        # Up only while collect.py is actively running
docker compose logs -f   # live server logs
```

Verified for real, not just in theory: a full real run processed 4 genuinely-oversized voice notes
(up to 58MB) through this exact setup - downloaded via `getFile` + `docker cp`, transcribed, filed
normally - while `config.json`'s `last_update_id` tracked correctly against the real API's numbers
throughout, confirming the `getUpdates`/`getFile` split above actually holds up end to end.
