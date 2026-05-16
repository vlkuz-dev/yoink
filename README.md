# yoink

Telegram bot that detects media URLs in chat messages, downloads the media
from the source platform, and re-uploads it inline. Instagram is the MVP
provider; additional platforms drop in as single-file modules.

## Quick start (dev)

```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e ".[dev]"
cp .env.example .env  # fill YOINK_BOT_TOKEN
python -m yoink
```

System binaries required at runtime: `gallery-dl`, `yt-dlp`, and `ffmpeg`
(provides `ffprobe`). Install via your package manager or pip:

```bash
pip install gallery-dl yt-dlp
# macOS: brew install ffmpeg
# Debian/Ubuntu: apt-get install ffmpeg
```

## Run via Docker

```bash
cp .env.example .env  # fill YOINK_BOT_TOKEN
docker compose up -d
docker compose logs -f yoink
```

The compose stack mounts `./data` for the SQLite cache and a named
volume for the worker scratch directory. The container's healthcheck
inspects the worker heartbeat file (`/tmp/yoink/.heartbeat`); a stale
heartbeat marks the container unhealthy.

## Tests

```bash
pytest -q
ruff check
mypy src
```

## Configuration

All environment variables are prefixed `YOINK_`. See `.env.example` for the
complete list.

| Variable | Default | Notes |
|---|---|---|
| `YOINK_BOT_TOKEN` | _(required)_ | Telegram bot token from @BotFather |
| `YOINK_LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL` |
| `YOINK_LOG_FORMAT` | `json` | `json` for prod, `console` for dev |
| `YOINK_WORKERS` | `4` | Worker pool size |
| `YOINK_QUEUE_MAXSIZE` | `64` | In-process job queue cap |
| `YOINK_DOWNLOAD_TIMEOUT_S` | `90` | Per-fetch subprocess timeout |
| `YOINK_MAX_FILE_MB` | `50` | Telegram bot API upload cap |
| `YOINK_RATE_PER_CHAT_PER_MIN` | `10` | Token-bucket refill per chat |
| `YOINK_ALLOWLIST_MODE` | `false` | If `true`, drop URLs whose host is not in the registered provider domain set |
| `YOINK_ADMIN_IDS` | _(empty)_ | Comma-separated user IDs for `/stats`, `/flush_cache` |
| `YOINK_CACHE_DB` | `/data/yoink.sqlite` | SQLite file path |
| `YOINK_WORKDIR` | `/tmp/yoink` | Per-job scratch dir parent |
| `YOINK_IG_COOKIES_FILE` | _(empty)_ | gallery-dl cookie file for private/age-gated IG |

## Adding a new provider

A provider is a single file under `src/yoink/providers/` that exports a
module-level `provider` instance satisfying `providers.base.Provider`.
The registry auto-discovers any module in that package at startup. No
core changes required.

```python
# src/yoink/providers/tiktok.py
from __future__ import annotations

import re
from pathlib import Path

from yoink.core.models import MediaPackage


class TikTokProvider:
    name = "tiktok"
    domains = frozenset({"tiktok.com", "www.tiktok.com", "vm.tiktok.com"})
    _RE = re.compile(r"/video/\d+", re.I)

    def can_handle(self, url: str) -> bool:
        return bool(self._RE.search(url))

    async def fetch(self, url: str, workdir: Path) -> MediaPackage:
        # invoke yt-dlp / gallery-dl, collect files, return MediaPackage
        ...


provider = TikTokProvider()
```

Drop a test under `tests/unit/test_tiktok_provider.py` mocking
`run_subprocess`. The pipeline picks it up on next restart.

## Risks & known gaps

- **IG anti-scraping.** Instagram aggressively rate-limits scrapers and
  gates posts behind login. Supply a logged-in cookie file via
  `YOINK_IG_COOKIES_FILE` for stories and age-gated content. Keep
  `gallery-dl` / `yt-dlp` current — both ship frequent fixes.
- **TG 50 MB upload cap.** Hard limit of the public Bot API. `YOINK_MAX_FILE_MB`
  enforces it pre-flight; oversized files are skipped. A local Bot API
  server raises the ceiling to 2 GB (post-MVP).
- **Source-side rate limiting.** Providers raise `ProviderTransientError`
  on 429-style failures; the pipeline retries with backoff (1s, 4s).
  Permanent errors (404, geo-block) are not retried.
- **Extractor drift.** `gallery-dl` and `yt-dlp` track moving targets.
  Pin versions in the Dockerfile and rebuild regularly; structured logs
  surface tool versions on failure.
- **SSRF via redirects.** The initial URL is validated against private
  IP ranges and (optionally) an allowlist, but redirects inside
  `gallery-dl` / `yt-dlp` are not intercepted — neither tool exposes
  per-request IP pinning. Enable `YOINK_ALLOWLIST_MODE=true` to bound
  reachable hosts.
- **In-process queue.** Jobs live in `asyncio.Queue`; a hard kill drops
  in-flight links. Users can repost. Swap to `arq` + Redis if horizontal
  scale or durability is needed (queue boundary is stable).
- **Orphan workdirs.** Per-job scratch dirs under `YOINK_WORKDIR` are
  removed in `finally`, but a SIGKILL leaves them behind. Startup sweeps
  all job subdirs and preserves `.heartbeat`.
- **Stale `file_id` cache.** Telegram may invalidate cached `file_id`s
  over long horizons. Currently accepted as residual risk; future work
  is to purge on upload failure and re-fetch.
- **NSFW / illegal content.** Out of scope. The bot mirrors whatever the
  source returns; operate on chats you control.
- **Token / cookie leakage.** Structured-log processors strip
  `bot_token` and `cookies` keys; avoid passing them through `extra=`
  paths that bypass the processor chain.
