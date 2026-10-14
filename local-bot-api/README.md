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

That's it - every Telegram call `collect.py` makes (`getUpdates`, `sendMessage`, `getFile`) now
goes through your local server instead of `api.telegram.org`. For large files specifically,
`tg_download_file` pulls them straight out of the container with `docker cp` instead of
downloading over HTTP - that's what `TELEGRAM_LOCAL=1` changes: `getFile` returns an absolute path
*inside the container's own filesystem* instead of a URL, and the old HTTP download route simply
stops working in this mode. `docker cp` sidesteps that regardless of how the container's storage
is set up, which is also why the volume below is a named volume rather than a bind-mount straight
to this folder: `telegram-bot-api` names each bot's data directory after the literal bot token,
which contains a colon - not a valid character in a Windows path, so a host bind-mount here
actually crashes the server outright the moment it tries to create that directory (confirmed by
hand). A named volume lives inside Docker's own Linux VM instead, where that's a non-issue.

If this repo gets moved, or the container is ever renamed in `docker-compose.yml`, update
`"local_bot_api_container_name"` in `config.json` too (defaults to `link-collector-bot-api`,
matching the compose file).

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

A real end-to-end test (send a file through the bot, `getFile`, `docker cp` it out) has been run
against this exact setup and confirmed working.
