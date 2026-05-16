# Yoink — Telegram Media Relay Bot (MVP)

## Context

Greenfield project. Build a Telegram bot that detects media URLs in chat messages, downloads the media from the source platform, and re-uploads it back into the same chat. Instagram is the MVP provider; the design must let TikTok, YouTube Shorts, X/Twitter, etc. be added as drop-in modules with zero core changes.

**Why this matters:** Telegram does not unfurl IG/TikTok/etc. media inline. Users currently paste links and others must leave the chat to view content. Yoink turns the chat into a self-contained feed: paste link → media appears inline → conversation continues.

**Intended outcome:** A single-process, stateless, async Python 3.11+ bot, Docker-deployable, that survives weeks of real usage without manual intervention and where adding a new platform is a single-file PR.

## Decisions (locked in via planning Q&A)

| Choice | Selected | Rationale |
|---|---|---|
| Telegram lib | **aiogram 3.x** | Async-first, clean middleware, DI, fits media pipeline |
| Queue | **In-process `asyncio.Queue` + worker pool** | I/O-bound, single instance fine for MVP; Redis migration path preserved |
| IG extraction | **`gallery-dl` primary, `yt-dlp` fallback** | Best coverage (carousels, reels, stories); both maintained; both reusable for TikTok/YT/X later |
| Caching | **Telegram `file_id` cache only** (SQLite via `aiosqlite`) | TG is the CDN; re-sends are instant; no blob storage to manage |
| Optional features in MVP | Admin commands, per-chat rate limit, domain allowlist mode | Skip Prometheus for v0.1 (add post-MVP) |
| Testing | Regular (code → tests per task, all must pass before next task) | I/O-heavy, mocking-heavy code; pragmatic for greenfield |

## High-Level Architecture

```
                 ┌─────────────────────────────────────────────────────┐
                 │                  aiogram Dispatcher                 │
                 │  message → middleware (rate limit, logging) → router│
                 └──────────────────────────┬──────────────────────────┘
                                            │
                       ┌────────────────────▼─────────────────────┐
                       │           core.pipeline.handle()         │
                       │  1. extractor.extract_urls(message)      │
                       │  2. for url:                             │
                       │       provider = registry.find(url)      │
                       │       if None: skip (silent)             │
                       │       if cache.has(url): send file_id    │
                       │       else: queue.put(Job(url, chat_id)) │
                       └────────────────────┬─────────────────────┘
                                            │
                       ┌────────────────────▼─────────────────────┐
                       │       Worker pool (N asyncio tasks)      │
                       │  Job → provider.fetch → uploader.send    │
                       │   → cache.save(url, file_ids)            │
                       │   → cleanup workdir                      │
                       └─────┬──────────────────────────┬─────────┘
                             │                          │
              ┌──────────────▼────────────┐   ┌─────────▼─────────┐
              │ providers/instagram.py    │   │ uploader/telegram │
              │  - gallery-dl subprocess  │   │  send_photo /     │
              │  - yt-dlp fallback        │   │  send_video /     │
              │  - SSRF guard before run  │   │  send_media_group │
              └──────────────┬────────────┘   └───────────────────┘
                             │
                       ┌─────▼──────┐
                       │ downloader │ ← SSRF allowlist, filename sanitize
                       └────────────┘
```

External state: **SQLite file** (cache) only. Bot process is stateless apart from in-memory `asyncio.Queue` (which is acceptable — TG redelivers on missed reply? No, but worst case is a single dropped link on crash; user can repost).

## Folder Structure

```
yoink/
├── pyproject.toml                 # uv/pip, deps, ruff, mypy, pytest config
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── .dockerignore
├── README.md
├── src/yoink/
│   ├── __init__.py
│   ├── __main__.py                # entrypoint: python -m yoink
│   ├── config.py                  # pydantic-settings, env vars
│   ├── log.py                     # structlog setup (JSON in prod, console in dev)
│   ├── bot.py                     # aiogram Bot + Dispatcher wiring
│   ├── handlers.py                # message + admin command routers
│   ├── middleware.py              # rate-limit + log-correlation middleware
│   ├── core/
│   │   ├── __init__.py
│   │   ├── models.py              # MediaItem, MediaPackage, Job dataclasses
│   │   ├── pipeline.py            # orchestration entry + worker loop
│   │   ├── registry.py            # provider discovery + dispatch
│   │   └── rate_limiter.py        # in-memory token bucket per chat_id
│   ├── extractor/
│   │   ├── __init__.py
│   │   └── urls.py                # extract URLs from Message (text + entities)
│   ├── providers/
│   │   ├── __init__.py
│   │   ├── base.py                # Provider Protocol + helpers
│   │   └── instagram.py           # gallery-dl + yt-dlp fallback
│   ├── downloader/
│   │   ├── __init__.py
│   │   ├── runner.py              # subprocess wrapper (timeout, output capture)
│   │   └── safety.py              # URL validation, filename sanitization, SSRF
│   ├── uploader/
│   │   ├── __init__.py
│   │   └── telegram.py            # single + album upload, kind detection
│   ├── cache/
│   │   ├── __init__.py
│   │   ├── store.py               # aiosqlite-backed file_id store
│   │   └── schema.sql
│   └── admin/
│       ├── __init__.py
│       └── commands.py            # /stats, /flush_cache, /ping
├── tests/
│   ├── conftest.py
│   ├── unit/
│   │   ├── test_extractor.py
│   │   ├── test_registry.py
│   │   ├── test_rate_limiter.py
│   │   ├── test_safety.py
│   │   ├── test_cache.py
│   │   ├── test_uploader.py
│   │   └── test_instagram_provider.py
│   ├── integration/
│   │   └── test_pipeline.py       # full flow w/ mocked TG + provider
│   └── fixtures/
│       └── ig_gallery_dl_meta.json
└── docs/
    └── plans/
        └── 20260516-yoink-telegram-media-bot.md
```

## Key Interfaces

### `core/models.py`
```python
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

MediaKind = Literal["photo", "video", "animation", "document"]

@dataclass(slots=True)
class MediaItem:
    path: Path
    kind: MediaKind
    width: int | None = None
    height: int | None = None
    duration_s: int | None = None
    mime: str | None = None

@dataclass(slots=True)
class MediaPackage:
    source_url: str
    provider: str
    items: list[MediaItem]
    caption: str | None = None       # title / description / author handle
    nsfw: bool = False

@dataclass(slots=True)
class Job:
    chat_id: int
    reply_to_message_id: int | None
    url: str
    user_id: int
    correlation_id: str
```

### `providers/base.py`
```python
from typing import Protocol, runtime_checkable
from pathlib import Path
from yoink.core.models import MediaPackage

@runtime_checkable
class Provider(Protocol):
    name: str                       # "instagram"
    domains: frozenset[str]         # {"instagram.com", "www.instagram.com"}

    def can_handle(self, url: str) -> bool: ...
    async def fetch(self, url: str, workdir: Path) -> MediaPackage: ...
```

A provider is added by dropping a module in `providers/` that exports a top-level `provider` instance satisfying the Protocol. `core/registry.py` imports every module in `providers/` at startup (`pkgutil.iter_modules`) and indexes them by domain — no core edits to add TikTok later.

### `core/registry.py`
```python
class ProviderRegistry:
    def __init__(self) -> None:
        self._by_domain: dict[str, Provider] = {}

    def register(self, p: Provider) -> None: ...
    def find(self, url: str) -> Provider | None: ...     # domain match + can_handle()
    @classmethod
    def autodiscover(cls) -> "ProviderRegistry": ...     # walks providers/
```

### `cache/store.py`
```python
class FileIdCache:
    async def get(self, url_hash: str) -> list[CachedFile] | None: ...
    async def put(self, url_hash: str, files: list[CachedFile]) -> None: ...
    async def flush(self) -> int: ...                    # admin
    async def stats(self) -> CacheStats: ...

# CachedFile = (telegram_file_id, kind, mime)
```

Key = `sha256(normalized_url)` so query params / tracking junk don't blow the cache.

### `core/pipeline.py`
```python
class Pipeline:
    def __init__(self, registry, cache, rate_limiter, uploader, queue, workers): ...
    async def submit(self, msg: aiogram.types.Message) -> None: ...   # called by handler
    async def _worker(self) -> None: ...                              # N of these
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
```

## Configuration (env vars, `.env.example`)

```
YOINK_BOT_TOKEN=                       # required
YOINK_LOG_LEVEL=INFO
YOINK_LOG_FORMAT=json                  # json | console
YOINK_WORKERS=4
YOINK_QUEUE_MAXSIZE=64
YOINK_DOWNLOAD_TIMEOUT_S=90
YOINK_MAX_FILE_MB=50                   # TG bot API limit is 50MB; abort larger
YOINK_RATE_PER_CHAT_PER_MIN=10
YOINK_ALLOWLIST_MODE=false             # if true, only configured providers
YOINK_ADMIN_IDS=                       # comma-separated user IDs
YOINK_CACHE_DB=/data/yoink.sqlite
YOINK_WORKDIR=/tmp/yoink
YOINK_IG_COOKIES_FILE=                 # optional, for private/age-gated IG
```

## Suggested Libraries

| Purpose | Library | Notes |
|---|---|---|
| Telegram | `aiogram>=3.13` | Modern async; FSM + middleware |
| Settings | `pydantic-settings>=2` | Typed env loading |
| Logging | `structlog>=24` + `orjson` | Structured JSON logs, fast |
| Cache DB | `aiosqlite` | Async SQLite, zero infra |
| HTTP (rare needs) | `httpx` | Used for SSRF-checked downloads if any provider needs raw HTTP |
| Media probe | `ffprobe` via subprocess | For width/height/duration when extractor doesn't report |
| Extractors | `gallery-dl`, `yt-dlp` | Invoked as subprocesses (clean isolation, easy upgrades) |
| Tests | `pytest`, `pytest-asyncio`, `pytest-mock`, `respx` | aiohttp/HTTPX mocking if needed |
| Lint/format/types | `ruff`, `mypy --strict` | |

**Why subprocess over library imports for gallery-dl/yt-dlp?** Both expose Python APIs but those APIs are unstable and not async. Subprocess gives: clean timeouts, isolated CWD per job, predictable JSON output (`--dump-single-json`), and zero blocking of the event loop (via `asyncio.create_subprocess_exec`).

## Development Approach

- **testing approach**: Regular (code first, tests immediately after, all green before next task)
- complete each task fully before moving to the next
- small, focused changes
- **every task includes new/updated tests** — not optional
- **all tests must pass before starting next task**
- update this plan if scope changes mid-implementation
- maintain idempotency at the cache layer (re-running a job is safe)

## Testing Strategy

- **unit**: every module in `src/yoink/` has a corresponding `tests/unit/test_*.py`. Mock subprocesses (`asyncio.create_subprocess_exec`), mock aiogram `Bot` (use `aiogram.test_utils` or hand-rolled `AsyncMock`), use temp dirs via `tmp_path` fixture.
- **integration**: `tests/integration/test_pipeline.py` runs the full pipeline with a fake provider returning fixture files and a mock `Bot`. Verifies extract → dispatch → upload → cache.write happens for valid URLs and that non-media URLs are silently skipped.
- **no live network tests in CI** — they're flaky. Mark any that hit real IG with `@pytest.mark.live` and skip by default.
- run: `pytest -q` per task; final: `pytest -q --cov=src/yoink --cov-fail-under=80`.

## Progress Tracking

- mark completed items with `[x]` immediately when done
- ➕ prefix for newly discovered tasks
- ⚠️ prefix for blockers
- update plan if scope deviates

## What Goes Where

- **Implementation Steps** (`[ ]`): code, tests, config, Dockerfile — all in-repo work
- **Post-Completion** (no checkboxes): BotFather setup, deploy, live IG smoke test (requires real token), private chat permissions config

## Implementation Steps

### Task 1: Project scaffold + tooling

**Files:**
- Create: `pyproject.toml`
- Create: `src/yoink/__init__.py`
- Create: `src/yoink/__main__.py`
- Create: `src/yoink/config.py`
- Create: `src/yoink/log.py`
- Create: `tests/conftest.py`
- Create: `tests/unit/test_config.py`
- Create: `.env.example`
- Create: `.dockerignore`
- Create: `.gitignore`
- Create: `README.md` (stub)

- [x] create `pyproject.toml` with deps (aiogram, aiosqlite, structlog, pydantic-settings, httpx, orjson) and dev deps (pytest, pytest-asyncio, pytest-mock, ruff, mypy)
- [x] configure ruff (line-length 120, target py311, select E/F/I/B/UP/SIM/N/RUF) and mypy strict in `pyproject.toml`
- [x] create `src/yoink/config.py` with `Settings(BaseSettings)` — all env vars from the table above, prefix `YOINK_`
- [x] create `src/yoink/log.py` exposing `configure_logging(level, format)` + `get_logger(name)`
- [x] create `src/yoink/__main__.py` skeleton that loads settings, configures logging, prints "yoink starting" and exits 0
- [x] write tests for `Settings`: defaults applied, required fields raise, comma-separated `ADMIN_IDS` parses to `frozenset[int]`
- [x] run `pytest -q && ruff check && mypy src` — must pass before Task 2

### Task 2: Core models + URL extractor

**Files:**
- Create: `src/yoink/core/__init__.py`
- Create: `src/yoink/core/models.py`
- Create: `src/yoink/extractor/__init__.py`
- Create: `src/yoink/extractor/urls.py`
- Create: `tests/unit/test_extractor.py`

- [x] create `core/models.py` with `MediaItem`, `MediaPackage`, `Job` dataclasses (slots=True, kw_only where appropriate)
- [x] create `extractor/urls.py` with `extract_urls(message: aiogram.types.Message) -> list[str]` — uses `message.entities` first (URL + TEXT_LINK), falls back to regex on plain text only when no entities
- [x] regex fallback strips trailing punctuation (`.,;:!?)]}>"'`) commonly attached to pasted URLs
- [x] add `normalize_url(url: str) -> str` — strips known tracking params (`utm_*`, `igshid`, `si`, `fbclid`), lowercases scheme/host, removes fragment
- [x] write tests: extracts from text-mode entity, text_link entity (uses entity.url not displayed text), plain regex fallback, trailing-punct stripped (`https://x.com/p/abc).` → `https://x.com/p/abc`), ignores `tg://` and `mailto:`, normalizes IG URL strips `?igshid=…`
- [x] run `pytest -q` — must pass before Task 3

### Task 3: Provider interface + registry with autodiscovery

**Files:**
- Create: `src/yoink/providers/__init__.py`
- Create: `src/yoink/providers/base.py`
- Create: `src/yoink/core/registry.py`
- Create: `tests/unit/test_registry.py`
- Create: `tests/fixtures/dummy_provider.py` (test-only fake provider)

- [x] create `providers/base.py` with `Provider` `Protocol` (name, domains, can_handle, fetch)
- [x] create `core/registry.py` with `ProviderRegistry` — `register()`, `find(url)`, `autodiscover()` via `pkgutil.iter_modules(yoink.providers.__path__)` importing each and picking up module-level `provider` attribute matching the Protocol
- [x] `find()` first matches by URL host against `domains`, then calls `can_handle()` for fine-grained check
- [x] write tests: register manual provider then find by URL; autodiscover picks up a fixture provider; unknown URL returns `None`; `find()` is host-normalized (`www.` and case-insensitive)
- [x] run `pytest -q` — must pass before Task 4

### Task 4: Cache layer (SQLite file_id store)

**Files:**
- Create: `src/yoink/cache/__init__.py`
- Create: `src/yoink/cache/store.py`
- Create: `src/yoink/cache/schema.sql`
- Create: `tests/unit/test_cache.py`

- [x] write `schema.sql`: tables `cached_url(url_hash TEXT PK, source_url TEXT, provider TEXT, created_at INTEGER, last_used_at INTEGER)` and `cached_file(url_hash TEXT, position INT, file_id TEXT, kind TEXT, mime TEXT, PRIMARY KEY(url_hash, position))` with FK (`last_used_at` reserved for future LRU eviction — populated by `get()` but no eviction in MVP)
- [x] create `cache/store.py` — `FileIdCache(db_path)` with `init()` (runs schema), `get(url_hash)`, `put(url_hash, source_url, provider, files)`, `flush()`, `stats()`
- [x] use `aiosqlite` with WAL mode, single shared connection guarded by `asyncio.Lock` for writes
- [x] add `hash_url(normalized_url: str) -> str` helper (sha256, hex, 32 chars)
- [x] write tests: init creates schema; put + get round-trips ordered list of files; missing key returns `None`; flush clears; stats returns counts; concurrent puts don't corrupt
- [x] run `pytest -q` — must pass before Task 5

### Task 5: Safety layer (SSRF, filename sanitization, URL validation)

**Files:**
- Create: `src/yoink/downloader/__init__.py`
- Create: `src/yoink/downloader/safety.py`
- Create: `tests/unit/test_safety.py`

- [x] `validate_url(url, allowlist: frozenset[str] | None) -> ValidatedURL` — must be `https?://`, resolve **all** A/AAAA records and reject if **any** falls in private/reserved ranges (`0.0.0.0/8`, `10/8`, `100.64/10` CGNAT, `127/8`, `169.254/16`, `172.16/12`, `192.168/16`, `224/4` multicast; IPv6: `::1`, `fc00::/7` ULA, `fe80::/10` link-local, `ff00::/8` multicast), reject userinfo (`user:pass@`), reject ports outside `{80, 443}` unless explicitly allowed
- [x] if `allowlist` non-None, host must be in allowlist (post-normalization, suffix match for subdomains)
- [x] `sanitize_filename(name: str) -> str` — strip path separators, control chars, leading dots, limit to 200 chars, NFKD normalize
- [x] note: full DNS rebinding protection requires IP pinning into the subprocess, which `gallery-dl`/`yt-dlp` don't support per-request — accept this residual risk; document in README; allowlist mode is the harder mitigation
- [x] write tests: rejects `file://`, `ftp://`, `http://127.0.0.1`, `http://10.0.0.1`, `http://[::1]`, `http://100.64.0.1`, `http://user:pwd@x`; rejects when **any** resolved IP is private (mock `socket.getaddrinfo` returning mixed RRset); accepts `https://www.instagram.com/p/...`; allowlist enforced; sanitize strips `../` and nulls
- [x] run `pytest -q` — must pass before Task 6

### Task 6: Subprocess runner

**Files:**
- Create: `src/yoink/downloader/runner.py`
- Create: `tests/unit/test_runner.py`

- [x] `run_subprocess(cmd: list[str], cwd: Path, timeout_s: int, env: dict) -> SubprocessResult` — uses `asyncio.create_subprocess_exec` with `stdout=PIPE, stderr=PIPE`, enforces timeout via `asyncio.wait_for` + kill on timeout, captures + caps stderr at 64 KB to keep logs manageable
- [x] returns `SubprocessResult(returncode, stdout: bytes, stderr: str, duration_s: float)`
- [x] never passes user input via `shell=True`; args are always a list
- [x] write tests: successful run captures stdout, failed run captures stderr + nonzero rc, timeout kills process and raises `SubprocessTimeout`, no shell injection (test with `cmd=["echo", "; rm -rf /"]` — verifies the literal arg, no shell)
- [x] run `pytest -q` — must pass before Task 7

### Task 7: Instagram provider (gallery-dl primary, yt-dlp fallback)

**Files:**
- Create: `src/yoink/providers/instagram.py`
- Create: `tests/unit/test_instagram_provider.py`
- Create: `tests/fixtures/ig_gallery_dl_meta.json`

- [x] `InstagramProvider` with `name="instagram"`, `domains=frozenset({"instagram.com","www.instagram.com","instagr.am"})`
- [x] `can_handle(url)`: regex match against `/p/`, `/reel/`, `/tv/`, `/stories/` paths
- [x] `fetch(url, workdir)`: call `gallery-dl --dest <workdir> --no-part --no-skip -o output.mode=null -o output.shorten=false --write-metadata=false --dump-json <url>` via `run_subprocess` with `YOINK_IG_COOKIES_FILE` (`--cookies`) if set. `--dump-json` emits a single JSON array on stdout describing all extracted items (one element per file) — this is the parse target, not per-file `.json` sidecars. Files are still downloaded by gallery-dl into `workdir`.
- [x] correlate stdout JSON entries with on-disk files via the `filename`/`extension`/`subcategory` fields gallery-dl emits; resolve each to an absolute path under `workdir`
- [x] detect kind from extension + mime (`.mp4`/`.mov` → video, `.jpg`/`.png`/`.webp` → photo, `.gif` → animation)
- [x] fallback: if gallery-dl returns rc != 0 OR stdout JSON yields zero items OR no files on disk, run `yt-dlp -o '<workdir>/%(id)s.%(ext)s' --print-json --no-progress --no-warnings <url>` — `--print-json` emits one JSON line per downloaded video AND performs the actual download. Parse each line as a separate `MediaItem`.
- [x] use `ffprobe` (when binary available) to populate width/height/duration for videos when extractor metadata is missing
- [x] enforce `YOINK_MAX_FILE_MB` per file (raise `MediaTooLarge` — pipeline logs + skips)
- [x] export module-level `provider = InstagramProvider()` so autodiscovery picks it up
- [x] write tests (mock `run_subprocess` to return canned stdout + simulate files in `tmp_path`): single post returns 1 photo; carousel returns 3 items in stable order; reel returns 1 video; gallery-dl rc=1 triggers yt-dlp fallback and succeeds; gallery-dl rc=0 but zero items also triggers fallback; both fail raises `ProviderError`; `MediaTooLarge` raised when file exceeds limit; `can_handle` matches `/p/`, `/reel/`, rejects `/explore/`
- [x] run `pytest -q` — must pass before Task 8

### Task 8: Telegram uploader

**Files:**
- Create: `src/yoink/uploader/__init__.py`
- Create: `src/yoink/uploader/telegram.py`
- Create: `tests/unit/test_uploader.py`

- [x] `TelegramUploader(bot)` with `send(chat_id, reply_to, package: MediaPackage) -> list[CachedFile]` — returns Telegram `file_id`s in package order
- [x] single-item path: `send_photo` / `send_video` / `send_animation` / `send_document` based on `MediaItem.kind`
- [x] album path (len(items) ≥ 2): build `InputMediaPhoto` / `InputMediaVideo` list and call `send_media_group`; caption goes on first item only
- [x] **Telegram limit: media_group accepts 2–10 items**. For packages >10, chunk into multiple `send_media_group` calls of up to 10 each; caption only on first chunk's first item; preserve order across chunks
- [x] **mixed-kind handling**: `InputMediaPhoto` + `InputMediaVideo` can share a group; `InputMediaAnimation` / `InputMediaDocument` **cannot** be mixed with photo/video groups. Split: send photo+video items as one or more `send_media_group` calls, then send each animation/document individually as separate messages
- [x] handle `TelegramRetryAfter` by waiting `retry_after` + 0.5s then retrying once
- [x] handle `TelegramBadRequest` containing "file is too big" → raise `MediaTooLarge` to surface caller-side skip
- [x] write tests: single photo upload returns one file_id; album of 3 photos calls `send_media_group` once; album of 15 photos calls `send_media_group` twice (10+5) preserving order; mixed group (2 photos + 1 animation) sends one media_group call (photos) + one send_animation call; `RetryAfter` triggers sleep + retry (mock `asyncio.sleep`); too-big propagates
- [x] run `pytest -q` — must pass before Task 9

### Task 9: Rate limiter

**Files:**
- Create: `src/yoink/core/rate_limiter.py`
- Create: `tests/unit/test_rate_limiter.py`

- [x] `TokenBucketLimiter(rate_per_min, burst)` per chat_id; in-memory `dict[int, Bucket]` with monotonic clock; lazy GC of buckets idle >10 min
- [x] `try_acquire(chat_id) -> bool` non-blocking; consumed → True, exhausted → False (caller silently drops)
- [x] write tests: first N requests pass, N+1 fails, refills after time advance (monkeypatch `time.monotonic`), GC removes idle bucket
- [x] run `pytest -q` — must pass before Task 10

### Task 10: Pipeline orchestration + worker pool

**Files:**
- Create: `src/yoink/core/pipeline.py`
- Create: `tests/integration/test_pipeline.py`
- Create: `tests/__init__.py` (if needed)

- [ ] `Pipeline` wires registry + cache + rate_limiter + uploader + `asyncio.Queue(maxsize=...)` + N worker tasks
- [ ] `submit(message)`: extract URLs, normalize, hash, for each: check rate limiter, check cache (hit → upload `file_id` directly via uploader, skipping queue), miss → `queue.put_nowait(Job)` (catch `QueueFull` → log + drop, no user-facing reply)
- [ ] `_worker()`: loop `queue.get` → create unique workdir under `YOINK_WORKDIR/<correlation_id>/` → `provider.fetch` → `uploader.send` → `cache.put` → `shutil.rmtree(workdir)` (in `finally`); catch + log all `ProviderError` / `MediaTooLarge` / `TelegramBadRequest` — never propagate (worker must not die)
- [ ] `start()`: spawn N workers as `asyncio.Task`; `stop()`: cancel + await all, drain queue
- [ ] retry policy: hand-rolled `async def retry_async(fn, *, attempts=3, base=1.0, factor=4.0, retry_on=(ProviderTransientError,))` helper in `core/pipeline.py` (no extra dep). Wrap provider fetch — 2 retries (3 total attempts), backoff 1s then 4s; only `ProviderTransientError` (rate-limited / network) triggers retry; permanent errors (404, geo-blocked, `ProviderError`) re-raise immediately
- [ ] tests for `retry_async`: success on first try (no sleep), success after 2 transient failures (mock `asyncio.sleep`), exhausts retries then re-raises, permanent error not retried
- [ ] integration test: full pipeline with fake provider returning fixture file, mock Bot — sending a known URL twice → second call uses cache (no provider.fetch call); unknown URL silently skipped; provider error logged, worker survives, next job processes
- [ ] run `pytest -q` — must pass before Task 11

### Task 11: aiogram bot wiring + handlers + middleware

**Files:**
- Create: `src/yoink/bot.py`
- Create: `src/yoink/handlers.py`
- Create: `src/yoink/middleware.py`
- Modify: `src/yoink/__main__.py`
- Create: `tests/unit/test_handlers.py`

- [ ] `bot.py`: `build_bot(settings) -> tuple[Bot, Dispatcher]` — creates `Bot(token, default=DefaultBotProperties(parse_mode=None))` and `Dispatcher()`, attaches middleware
- [ ] **DI for handlers (aiogram 3.x pattern)**: store shared services on the dispatcher's workflow data — `dp["pipeline"] = pipeline; dp["rate_limiter"] = rate_limiter; dp["settings"] = settings`. Handlers receive them as kwargs because aiogram 3 injects matching names from `Dispatcher.workflow_data` automatically: `async def handle_message(message: Message, pipeline: Pipeline, settings: Settings) -> None: ...`
- [ ] `middleware.py`: `LoggingMiddleware` attaches `correlation_id` to `data["correlation_id"]`; `RateLimitMiddleware.__call__(handler, event, data)` calls `data["rate_limiter"].try_acquire(event.chat.id)` — **if denied, return `None` (do not call `await handler(event, data)`)** so the chain aborts silently with no reply
- [ ] `handlers.py`: register message handler for any text (`F.text | F.caption`) that calls `pipeline.submit(message)`; register admin commands (Task 12 stub for now)
- [ ] `__main__.py`: build settings, logger, cache (await init), registry (autodiscover), uploader, rate_limiter, pipeline (await start), bot + dp; populate `dp` workflow data; run `dp.start_polling(bot)` with graceful shutdown on SIGTERM/SIGINT (cancel pipeline → close bot session → close cache)
- [ ] write tests: handler calls `pipeline.submit` once per message; middleware returns without invoking handler when limiter denies (assert handler mock not called); middleware injects correlation_id into `data`
- [ ] run `pytest -q` — must pass before Task 12

### Task 12: Admin commands + allowlist mode wiring

**Files:**
- Create: `src/yoink/admin/__init__.py`
- Create: `src/yoink/admin/commands.py`
- Modify: `src/yoink/handlers.py`
- Modify: `src/yoink/core/pipeline.py` (allowlist enforcement)
- Create: `tests/unit/test_admin.py`

- [ ] `is_admin(user_id, settings) -> bool` checks `settings.admin_ids`
- [ ] `/ping`, `/stats` (cache stats + queue depth + worker count), `/flush_cache` — all gated by `is_admin`; non-admin: silent (no reply)
- [ ] register admin routes in `handlers.py` using `aiogram.filters.Command` + `F.from_user.id.in_(admin_ids)`
- [ ] allowlist mode: when `settings.allowlist_mode=true`, `pipeline.submit` filters URLs through `validate_url(url, allowlist=settings.allowed_domains)`; allowed domains derived from union of `provider.domains` across registry
- [ ] write tests: admin `/stats` returns counts; non-admin gets no reply; allowlist mode rejects unknown domain; allowlist off lets everything through to provider lookup
- [ ] run `pytest -q` — must pass before Task 13

### Task 13: Dockerfile + compose + final wiring

**Files:**
- Create: `Dockerfile`
- Create: `docker-compose.yml`
- Modify: `README.md`
- Create: `tests/unit/test_smoke.py`

- [ ] multi-stage `Dockerfile`: builder installs deps via `pip wheel`, runtime image is `python:3.11-slim` + `ffmpeg` (provides `ffprobe`) + `gallery-dl` + `yt-dlp`; non-root user `yoink`; `WORKDIR /app`; `CMD ["python","-m","yoink"]`
- [ ] `docker-compose.yml`: service `yoink` with `env_file: .env`, volume for SQLite (`./data:/data`), volume for workdir (`yoink_tmp:/tmp/yoink`), `restart: unless-stopped`, healthcheck: workers touch `/tmp/yoink/.heartbeat` every 10s in their idle loop; healthcheck checks `find /tmp/yoink/.heartbeat -mmin -1` returns the file — proves the bot is alive AND workers are scheduled, not just that import works
- [ ] startup sweep: on bot start, `shutil.rmtree` any pre-existing job subdirs under `YOINK_WORKDIR` (orphans from previous SIGKILL); preserve `.heartbeat`
- [ ] `.dockerignore`: `.git`, `tests/`, `docs/`, `__pycache__`, `.venv`, `*.pyc`
- [ ] README: install, env vars, run via `docker compose up`, how to add a new provider (one example showing `providers/tiktok.py` stub satisfying `Provider` Protocol)
- [ ] smoke test: import `yoink` and `yoink.__main__` succeeds (catches import-time wiring errors)
- [ ] run `pytest -q` and `docker build -t yoink:test .` — must pass before Task 14

### Task 14: Verify acceptance criteria

- [ ] verify message flow: paste IG `/p/...` → media appears in chat (manual smoke with real token, see Post-Completion)
- [ ] verify silent skip for non-media link (https://example.com)
- [ ] verify rate limit drops 11th request inside 1 min from same chat with no reply
- [ ] verify cache hit path: send same URL twice → second arrives in <1s with no subprocess
- [ ] verify worker survives provider error (force a 404 IG URL, then send valid one — second must work)
- [ ] verify allowlist mode rejects unknown domain when toggled on
- [ ] verify admin gating: non-admin `/stats` → no reply; admin `/stats` → reply
- [ ] verify graceful shutdown: SIGTERM stops polling, drains in-flight job, closes DB
- [ ] run full suite: `pytest -q --cov=src/yoink --cov-fail-under=80`
- [ ] `ruff check && mypy --strict src`

### Task 15: Documentation + plan archive

- [ ] write provider-authoring doc inside README ("Adding a new provider" section, ~30 lines with TikTok skeleton)
- [ ] document risks (Risks section below) in README
- [ ] create `CLAUDE.md` capturing: codebase conventions, how to run locally, where IG cookies go, provider testing pattern
- [ ] move this plan to `docs/plans/completed/20260516-yoink-telegram-media-bot.md`

## Risks & Edge Cases

| Risk | Likelihood | Mitigation |
|---|---|---|
| **IG anti-scraping / login walls** | High — IG actively breaks scrapers | gallery-dl + yt-dlp both update fast; allow cookie file via `YOINK_IG_COOKIES_FILE`; structured logs with version of each tool so we know when to bump |
| **TikTok watermarks / region locks** | Medium (future) | yt-dlp generally handles; region-locked content fails — log + skip |
| **TG 50 MB bot upload cap** | Medium | Enforce `YOINK_MAX_FILE_MB` pre-flight; for larger videos in future, consider local bot API server (raises to 2 GB) — out of MVP scope |
| **Rate-limited by source platform** | High | Provider returns `ProviderTransientError` → pipeline retries w/ backoff; bucket already throttles inbound |
| **gallery-dl/yt-dlp dependency drift** | Medium | Pin versions in Dockerfile; rebuild via `--upgrade` weekly; document in README |
| **SSRF via redirects** | Low-Med | Initial URL validated; redirects happen inside gallery-dl/yt-dlp which we can't intercept — accept residual risk, document, limit by domain allowlist when enabled |
| **Disk exhaustion via concurrent large files** | Low | Per-job workdir cleaned in `finally`; `YOINK_MAX_FILE_MB` bounds size; consider tmpfs mount |
| **Token leak via logs** | Low | structlog processor strips `bot_token` / `cookies` from any log event by key |
| **Crash drops in-memory queue** | Low | Acceptable for MVP — user reposts; if it becomes a problem, swap `asyncio.Queue` for `arq` (Redis), no API change needed at the queue boundary |
| **Orphaned workdirs after SIGKILL** | Low-Med | Job subdirs under `YOINK_WORKDIR` outlive crashes. Mitigation: startup sweep deletes all subdirs (see Task 13). Disk leak bounded by container restart cadence. |
| **TG `file_id` invalidation** | Low | Rare; if upload fails with "file_id invalid", purge cache entry on the fly and re-fetch from source. Track in Task 8 error handling as future work — accept stale-cache reposts for MVP. |
| **NSFW / illegal content** | Real | Out of MVP scope to filter; document that bot mirrors whatever source provides |
| **Telegram media_group ordering** | Low | aiogram preserves list order; tests verify |

## Verification

End-to-end manual smoke (post-deploy):

1. `docker compose up -d` with real `YOINK_BOT_TOKEN`
2. Add bot to a test group / start DM
3. Paste IG single-image post URL → expect single photo reply
4. Paste IG carousel URL → expect album
5. Paste IG Reel URL → expect video
6. Paste same URL again → expect ~instant resend (cache hit)
7. Paste `https://example.com` → expect no reply, no error in logs
8. Paste IG URL 11 times in 1 min → expect 10 deliveries, 1 silent drop
9. As admin, send `/stats` → expect counts; as non-admin → no reply
10. `docker compose logs yoink` → expect JSON logs with `correlation_id` field per request

## Post-Completion

**Manual verification** (requires real Telegram + IG, not in CI):
- Create bot via @BotFather; capture token
- For private/age-gated IG support: export cookies from a logged-in browser via `gallery-dl` cookie helper, mount file into container, set `YOINK_IG_COOKIES_FILE`
- Smoke-test 10 mixed URLs across post types
- Observe disk usage for 24h under typical chat load
- Validate graceful restart under `docker compose restart`

**External system updates / future work** (out of MVP):
- Add `tiktok.py`, `youtube.py`, `twitter.py` providers (each: ~50 LOC + tests, no core changes)
- Add Prometheus `/metrics` endpoint (aiohttp side-server in same process)
- Migrate `asyncio.Queue` → `arq` + Redis when horizontal scale is needed
- Local Bot API server for >50 MB uploads
- Inline `@yoink <url>` query mode (aiogram inline handlers)
