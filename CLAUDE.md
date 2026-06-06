# CLAUDE.md

Working notes for AI assistants editing this repository.

## What this is

`yoink` is an aiogram-based Telegram bot. Users paste media URLs (Instagram
and TikTok supported), the bot downloads via `gallery-dl` / `yt-dlp`
subprocesses and re-uploads the media inline. Single process, stateless apart
from a SQLite `file_id` cache.

## Codebase conventions

- Python 3.11+, `src/`-layout, package `yoink`.
- Lint with `ruff` (line length 120, target `py311`, rules
  `E,F,I,B,UP,SIM,N,RUF`). Type-check with `mypy --strict` against `src/`.
- Dataclasses use `slots=True`. Public APIs are typed end-to-end.
- Logging is `structlog`. Never `print` outside `__main__` startup output.
- Never call `subprocess` with `shell=True`. Use `downloader.runner.run_subprocess`
  which wraps `asyncio.create_subprocess_exec`.
- New external URLs that the bot fetches must pass `downloader.safety.validate_url`
  (rejects private IP ranges, non-`http(s)`, userinfo, off-list ports).
- Aiogram 3.x dependency injection: shared services (`pipeline`, `settings`,
  `cache`) live on `Dispatcher.workflow_data`. Handlers take them as kwargs
  by name — do not import singletons. Rate limiting is enforced inside
  `Pipeline.submit`, not as a middleware.
- Tests live under `tests/unit` and `tests/integration`. `pytest-asyncio` is in
  `asyncio_mode = "auto"`. `filterwarnings = ["error"]` — warnings break the build.

## How to run locally

```bash
uv venv --python 3.11
source .venv/bin/activate
uv pip install -e ".[dev]"
cp .env.example .env  # set YOINK_BOT_TOKEN
python -m yoink
```

Runtime requires `gallery-dl`, `yt-dlp`, and `ffmpeg` (for `ffprobe`) on PATH.

Common commands:

```bash
pytest -q                                            # unit + integration
pytest -q --cov=src/yoink --cov-fail-under=80        # with coverage gate
ruff check
mypy src
docker compose up -d                                 # full stack with healthcheck
```

## Instagram cookies

For private or age-gated Instagram posts, export cookies from a logged-in
browser (Netscape format — `gallery-dl --cookies-from-browser` or the Get
cookies.txt extension). Mount the file into the container and point
`YOINK_IG_COOKIES_FILE` at the in-container path. Treat the file as a secret:
it grants session-level access to the source account.

## Provider testing pattern

A provider is a single module under `src/yoink/providers/` exporting a
module-level `provider` instance that satisfies `providers.base.Provider`.
`core.registry.ProviderRegistry.autodiscover()` imports each module via
`pkgutil.iter_modules` and indexes by domain. No core edits to add a new
platform.

When testing a provider:

- Mock `yoink.downloader.runner.run_subprocess` with a `SubprocessResult`
  fixture; do **not** spawn real `gallery-dl` / `yt-dlp` in CI.
- Synthesise the on-disk artifacts the real subprocess would write into the
  `tmp_path` fixture so `fetch()` can resolve them.
- Cover: single-item happy path, multi-item (carousel) order preservation,
  fallback path (primary tool fails or returns zero items), `MediaTooLarge`
  when a synthesised file exceeds `YOINK_MAX_FILE_MB`, and `can_handle()`
  accept/reject cases.
- Mark anything that actually touches the network with `@pytest.mark.live`
  and skip by default.

`tests/unit/test_instagram_provider.py` is the reference pattern.

Tool order is per-provider. Instagram runs `gallery-dl` primary →
`yt-dlp` fallback; the TikTok provider inverts this (`yt-dlp` primary →
`gallery-dl` fallback, since yt-dlp has the stronger TikTok extractor and
resolves `vm.`/`vt.` short links cleanly). TikTok needs no cookies — public
posts only — and supports both standard videos and photo slideshows
("photo mode", collected in numeric-suffix order). See
`tests/unit/test_tiktok_provider.py`.

## Where things live

- `src/yoink/__main__.py` — entrypoint: loads settings, builds services, wires Dispatcher.workflow_data, runs polling.
- `src/yoink/bot.py` — `build_bot(settings)` factory returning the aiogram `Bot` + `Dispatcher` pair.
- `src/yoink/handlers.py` — `register_routers(dp, *, admin_ids, chat_allowlist)`: includes admin router (before message router) and the URL pipeline router; the pipeline router has a chat-id allowlist filter (empty allowlist = deny all).
- `src/yoink/extractor/urls.py` — entity + regex URL extraction, scheme allowlist, tracking-param stripping, dedupe, cap at `_MAX_URLS_PER_MESSAGE`.
- `src/yoink/core/pipeline.py` — orchestration entry, worker loop, retry helper, heartbeat task, workdir sweep.
- `src/yoink/core/registry.py` — provider autodiscovery and domain dispatch.
- `src/yoink/core/errors.py` — canonical exception hierarchy (`ProviderError`, `ProviderTransientError`, `MediaTooLarge`, `SubprocessTimeoutError`). Add new error types here, not in their consumers.
- `src/yoink/cache/store.py` — `aiosqlite` `file_id` cache, WAL mode, lock-guarded writes.
- `src/yoink/downloader/safety.py` — URL validation (SSRF guards, allowlist), filename sanitization.
- `src/yoink/uploader/telegram.py` — single / album / chunked uploads, retry-after handling, stale-file_id and too-big detection.
- `src/yoink/middleware.py` — correlation id middleware (rate limiting lives in `Pipeline.submit`).
- `src/yoink/admin/commands.py` — `/ping`, `/stats`, `/flush_cache` (admin-gated, silent for non-admins).

## Runtime invariants

- `Pipeline.start()` runs `sweep_workdir()` (removes orphan per-job dirs from prior crashes, preserves `.heartbeat`) and spawns a heartbeat task that touches `<YOINK_WORKDIR>/.heartbeat` every 10 s. The docker-compose healthcheck reads `find /tmp/yoink/.heartbeat -mmin -1`. If you walk the workdir tree, always preserve `.heartbeat`.
- Cache lifecycle is owned by `__main__`: `await cache.init()` before `dp.workflow_data["cache"] = cache`, `await cache.close()` after `bot.session.close()`. Preserve this ordering when refactoring shutdown.
- Allowlist when `YOINK_ALLOWLIST_MODE=true` is derived from `ProviderRegistry.known_domains` (union of `provider.domains`, normalized) and passed to `Pipeline(allowlist=...)`. SSRF DNS resolution at submit-time is intentionally off (`resolve_dns=False`); literal-IP, scheme, port, userinfo, and host-allowlist checks still apply. `gallery-dl` / `yt-dlp` resolve hostnames internally.
- Providers needing settings-derived config (cookies, byte caps, timeouts) expose a module-level `configure(...)`; `__main__` calls it before `ProviderRegistry.autodiscover()`. See `instagram_provider.configure(...)` for the pattern.
- `YOINK_CHAT_ALLOWLIST` gates the URL pipeline router at the aiogram filter level. Empty allowlist silently drops every message; admin DM commands still work because the admin router runs first and is gated by user.id only. Polling is also pinned to `allowed_updates=["message"]` so `business_message` / `guest_message` / channel updates are never even requested.
- Two independent token buckets in `Pipeline.submit`: per-chat (`YOINK_RATE_PER_CHAT_PER_MIN`, anti-flood, **does not** charge on cache hits) and per-user (`YOINK_RATE_PER_USER_PER_HOUR`, scroll-budget, **does** charge on cache hits). When the per-user bucket runs out, the pipeline `message.reply()`s `GO_TOUCH_GRASS_TEXT` once per message and silently drops the remaining URLs in that message. The user-limiter is constructed with `idle_gc_seconds=2*3600` so the bucket survives the full hourly window.
