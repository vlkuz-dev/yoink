# CLAUDE.md

Working notes for AI assistants editing this repository.

## What this is

`yoink` is an aiogram-based Telegram bot. Users paste media URLs (Instagram
in MVP), the bot downloads via `gallery-dl` / `yt-dlp` subprocesses and
re-uploads the media inline. Single process, stateless apart from a SQLite
`file_id` cache.

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
- Aiogram 3.x dependency injection: shared services (`pipeline`, `rate_limiter`,
  `settings`, `cache`) live on `Dispatcher.workflow_data`. Handlers take them
  as kwargs by name — do not import singletons.
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

## Where things live

- `src/yoink/core/pipeline.py` — orchestration entry, worker loop, retry helper.
- `src/yoink/core/registry.py` — provider autodiscovery and domain dispatch.
- `src/yoink/cache/store.py` — `aiosqlite` `file_id` cache, WAL mode, lock-guarded writes.
- `src/yoink/downloader/safety.py` — URL validation, filename sanitization.
- `src/yoink/uploader/telegram.py` — single / album / chunked uploads, retry-after handling.
- `src/yoink/middleware.py` — correlation id + rate-limit middleware.
- `src/yoink/admin/commands.py` — `/ping`, `/stats`, `/flush_cache` (admin-gated, silent for non-admins).
