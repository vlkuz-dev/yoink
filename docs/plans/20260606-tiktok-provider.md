# Add TikTok video + photo-slideshow provider

## Overview
- Add a new `tiktok` provider so users can paste TikTok links and the bot
  downloads and re-uploads the media inline, exactly like the existing
  Instagram flow.
- Solves: TikTok is currently unsupported; links are silently ignored
  (no provider claims the domain, so the pipeline skips them).
- Integration: follows the zero-core-edit provider pattern. A single new
  module `src/yoink/providers/tiktok.py` exports a module-level `provider`
  instance; `ProviderRegistry.autodiscover()` indexes it by domain
  automatically. The only edits outside the new module/test file are a
  `configure(...)` call in `__main__.py` and documentation updates.
- Scope (confirmed with user):
  - **Media**: standard videos **and** photo slideshows (TikTok "photo mode").
  - **Tool order**: `yt-dlp` primary → `gallery-dl` fallback (inverts
    Instagram's order; yt-dlp has the stronger TikTok extractor and resolves
    short links cleanly).
  - **Cookies**: none. Public posts only. No new secret to manage.
  - **Testing**: TDD — tests written first per task, then implement to green.

## Context (from discovery)
- Files/components involved:
  - NEW `src/yoink/providers/tiktok.py` — the provider.
  - NEW `tests/unit/test_tiktok_provider.py` — mirror of the Instagram suite.
  - EDIT `src/yoink/__main__.py` — add `tiktok_provider.configure(...)` before
    `ProviderRegistry.autodiscover()` (lines ~35-42, next to the existing
    `instagram_provider.configure(...)`).
  - EDIT `README.md`, `CLAUDE.md` — note TikTok is now supported.
  - NO edits to `core/registry.py`, `core/errors.py`, `core/models.py`,
    `providers/base.py`, `config.py` (reuses existing `YOINK_MAX_FILE_MB` /
    `YOINK_DOWNLOAD_TIMEOUT_S`; no new settings).
- Reference implementation: `src/yoink/providers/instagram.py`.
  - `Provider` protocol (`providers/base.py:1-17`):
    ```python
    @runtime_checkable
    class Provider(Protocol):
        name: str
        domains: frozenset[str]
        def can_handle(self, url: str) -> bool: ...
        async def fetch(self, url: str, workdir: Path) -> MediaPackage: ...
    ```
  - `can_handle` pattern (`instagram.py:296-306`): `urlsplit` → normalize host
    (lowercase, strip `www.`) → host-set membership → path regex.
  - `configure(...)` pattern (`instagram.py:520-546`): applies runtime settings
    to the import-time singleton; called from `__main__` because
    pydantic-settings does not export `.env` into `os.environ`.
  - `fetch` flow: `mkdir` → purge stale artifacts → run primary tool →
    `_collect_items` → on failure purge + run fallback → `_enforce_size` →
    return `MediaPackage`.
  - `_collect_items` (`instagram.py:464-510`): glob `workdir/**/*` for media
    extensions, sort by `(depth, trailing-numeric-suffix, lexicographic)` so
    `_10` sorts after `_2`, read `.json`/`.info.json` sidecars, `ffprobe`
    video dims when missing.
- Return models (`core/models.py:10-25`):
  ```python
  @dataclass(slots=True, kw_only=True)
  class MediaItem:
      path: Path
      kind: MediaKind  # Literal["photo","video","animation","document"]
      width: int | None = None
      height: int | None = None
      duration_s: int | None = None
      mime: str | None = None

  @dataclass(slots=True, kw_only=True)
  class MediaPackage:
      source_url: str
      provider: str
      items: list[MediaItem]
      caption: str | None = None
  ```
- Error hierarchy (`core/errors.py`): `ProviderError` (permanent, skipped),
  `ProviderTransientError` (retry-eligible), `MediaTooLarge.from_size(...)`.
  No new error types needed (no cookies → no `CookiesExpired` analogue).
- Subprocess contract (`downloader/runner.py`):
  ```python
  async def run_subprocess(cmd, *, cwd, timeout_s, env=None,
                           stderr_cap_bytes=64*1024) -> SubprocessResult
  # SubprocessResult(returncode:int, stdout:bytes, stderr:str, duration_s:float)
  # raises SubprocessTimeoutError on wall-clock timeout (returncode=-1)
  ```
- Test pattern (`tests/unit/test_instagram_provider.py`): a `_Recorder`
  mock of `run_subprocess` records `cmd`s and runs a `side_effect` that
  synthesises on-disk artifacts into `tmp_path`; helpers `_make_file`,
  `_write_sidecar`. Cases: purge, can_handle accept/reject, single item,
  carousel order (3 and 10 items), video duration, fallback (rc!=0 and
  zero-items), both-fail classification, transient markers, MediaTooLarge,
  security flags, sidecar skipping, configure(), env-var fallback.

### TikTok-specific notes
- Hostnames to claim (each listed explicitly so the registry indexes it; the
  registry only strips a leading `www.`, it does not collapse other
  subdomains): `tiktok.com`, `www.tiktok.com`, `m.tiktok.com`,
  `vm.tiktok.com`, `vt.tiktok.com`. These also become `known_domains` →
  added to the SSRF allowlist when `YOINK_ALLOWLIST_MODE=true`.
- URL shapes:
  - Full video: `https://www.tiktok.com/@user/video/<id>`
  - Photo slideshow: `https://www.tiktok.com/@user/photo/<id>`
  - Short links (path is an opaque token, NOT `/video/`):
    `https://vm.tiktok.com/<token>/`, `https://vt.tiktok.com/<token>/`,
    `https://www.tiktok.com/t/<token>/`
  - `can_handle` therefore accepts: (a) any short-link host
    (`vm.`/`vt.tiktok.com`) regardless of path, (b) `tiktok.com`/`m.tiktok.com`
    when path matches `^/(@[^/]+/(video|photo)/\d+|t/|v/\d+)` .
  - `can_handle` must REJECT bare profile/tag/discover paths
    (`/@user`, `/tag/...`, `/discover/...`, `/foryou`).
  - Short-link resolution itself is delegated to yt-dlp / gallery-dl (they
    follow the redirect internally); `fetch` passes the URL through unchanged.
- yt-dlp downloads a photo slideshow as multiple image files (+ possibly an
  audio track / mp4). `_collect_items` already orders by numeric suffix; the
  provider classifies images as `kind="photo"` and `.mp4` as `kind="video"`.
- No cookie-dead markers (no auth). Reuse the same transient markers as
  Instagram (rate-limit `429`, `connection reset/refused/aborted`, `timeout`,
  `http error 5xx/408/425`). Anything else from both tools → permanent
  `ProviderError`.

## Development Approach
- **Testing approach**: TDD — write the failing test(s) first in each task,
  then implement until green.
- Complete each task fully before moving to the next.
- Make small, focused changes.
- **CRITICAL: every task MUST include new/updated tests** for code changes in
  that task. Tests are a required checklist item, not optional. Cover both
  success and error/edge scenarios.
- **CRITICAL: all tests must pass before starting the next task** — no
  exceptions. `pytest` runs with `filterwarnings = ["error"]`, so warnings
  fail the build too.
- **CRITICAL: update this plan file when scope changes during implementation.**
- Maintain backward compatibility (the Instagram provider and pipeline must be
  untouched in behaviour).
- Reuse existing helpers — do NOT call `subprocess` directly; go through
  `downloader.runner.run_subprocess`. New fetched URLs are validated by the
  pipeline's existing `validate_url`; the provider does not re-validate.

## Testing Strategy
- **Unit tests**: required for every task. Mock `run_subprocess` with a
  `_Recorder`; synthesise on-disk artifacts in `tmp_path`. Do NOT spawn real
  `yt-dlp` / `gallery-dl` in CI. Anything touching the network → mark
  `@pytest.mark.live` and skip by default.
- **Integration test**: one test asserting `ProviderRegistry.autodiscover()`
  registers the TikTok provider, that `known_domains` contains the TikTok
  hosts, and that `registry.find(<tiktok url>)` returns the TikTok provider
  (lives under `tests/integration/`).
- No e2e/UI tests in this project.

## Progress Tracking
- Mark completed items with `[x]` immediately when done.
- Add newly discovered tasks with ➕ prefix.
- Document issues/blockers with ⚠️ prefix.
- Keep the plan in sync with actual work done.

## What Goes Where
- **Implementation Steps** (`[ ]`): code, tests, docs achievable in this repo.
- **Post-Completion** (no checkboxes): manual/live verification with a real
  TikTok URL, deployment notes.

## Implementation Steps

### Task 1: Scaffold provider module + URL matching (`can_handle`)
- [x] create `src/yoink/providers/tiktok.py` with `TikTokProvider` class:
      `name = "tiktok"`, `domains = frozenset({"tiktok.com","www.tiktok.com",
      "m.tiktok.com","vm.tiktok.com","vt.tiktok.com"})`, and a module-level
      `provider = TikTokProvider()` export
- [x] implement `can_handle(url)`: `urlsplit` (guard `ValueError`), normalize
      host (lowercase, strip leading `www.`), accept short-link hosts
      (`vm.tiktok.com`/`vt.tiktok.com`) by host alone; for
      `tiktok.com`/`m.tiktok.com` require path regex
      `^/(@[^/]+/(video|photo)/\d+|t/|v/\d+)`; reject all other hosts/paths
- [x] write tests: `can_handle` ACCEPT — `/@user/video/<id>`,
      `/@user/photo/<id>`, `vm.tiktok.com/<tok>/`, `vt.tiktok.com/<tok>/`,
      `www.tiktok.com/t/<tok>/`, with/without `www.`, `m.tiktok.com`
- [x] write tests: `can_handle` REJECT — bare `/@user`, `/tag/x`,
      `/discover/x`, `/foryou`, instagram.com URL, malformed URL
- [x] run `pytest -q tests/unit/test_tiktok_provider.py` — must pass before Task 2

### Task 2: yt-dlp command builder (primary tool) + security flags
- [x] implement `_run_yt_dlp(url, workdir)`: build argv `["yt-dlp",
      "--ignore-config", "-o", f"{workdir}/%(id)s.%(ext)s", "--no-progress",
      "--no-warnings", "--write-info-json", "--", url]`; invoke via
      `run_subprocess(cmd, cwd=workdir, timeout_s=self._effective_timeout())`
- [x] write tests: assert the emitted argv contains the security flags
      (`--ignore-config`, `--` arg-injection guard) and the URL is the last arg
- [x] write tests: `SubprocessTimeoutError` from the runner is caught and
      surfaced as a transient failure (no crash)
- [x] run tests — must pass before Task 3

### Task 3: Artifact collection, ordering, sidecars, size cap
- [x] implement `_collect_items(workdir)`: glob `workdir/**/*` for media
      extensions (images → `kind="photo"`, video → `kind="video"`), skip
      `.json`/`.info.json` sidecars and dotfiles, sort by
      `(depth, trailing-numeric-suffix, lexicographic)`; read sidecar metadata;
      `ffprobe` video dimensions/duration when absent (reuse the Instagram
      probe helper or a shared util)
- [x] implement `_enforce_size(items)`: raise `MediaTooLarge.from_size(...)`
      when a file exceeds `self._effective_max_bytes()`
- [x] implement `_purge_workdir(workdir)`: remove stale artifacts (preserve
      `.heartbeat` if the workdir ever coincides — per-job dirs won't, but keep
      the guard for safety)
- [x] write tests: single video → one `video` item with probed dims;
      single photo-post image → one `photo` item
- [x] write tests: skips `.info.json` sidecars and dotfiles; `MediaTooLarge`
      raised when a synthesised file exceeds `YOINK_MAX_FILE_MB`
- [x] run tests — must pass before Task 4

### Task 4: `fetch()` orchestration (yt-dlp primary, happy path)
- [x] implement `async fetch(url, workdir)`: `mkdir` → `_purge_workdir` →
      `_run_yt_dlp` → `_collect_items` → `_enforce_size` → return
      `MediaPackage(source_url=url, provider=self.name, items=..., caption=None)`
- [x] write tests: single-video happy path returns a 1-item package in order
- [x] write tests: photo-slideshow happy path with 3 images → 3 `photo` items
      in stable order; 10 images → numeric order (`_2` before `_10`)
- [x] write tests: `_purge_workdir` runs before the tool (stale leftover file
      from a prior attempt is NOT included in results)
- [x] run tests — must pass before Task 5

### Task 5: gallery-dl fallback + error classification
- [ ] implement `_run_gallery_dl(url, workdir)`: argv `["gallery-dl",
      "--config-ignore", "-D", str(workdir), "--no-part", "--no-skip",
      "-o","output.mode=null", "-o","output.shorten=false",
      "--write-metadata", "--", url]`
- [ ] extend `fetch`: when yt-dlp returns `rc != 0` OR zero items (or times
      out), `_purge_workdir` then run gallery-dl + `_collect_items`
- [ ] implement transient/permanent classification: stderr matching the shared
      transient markers → `ProviderTransientError`; both tools failing with no
      transient marker → `ProviderError`; if EITHER tool's failure is transient,
      the combined result is transient
- [ ] write tests: yt-dlp `rc=1` triggers gallery-dl fallback (assert both argv
      recorded, gallery-dl result returned)
- [ ] write tests: yt-dlp zero-items triggers gallery-dl fallback
- [ ] write tests: both fail (non-transient) → `ProviderError`; rate-limit /
      5xx stderr → `ProviderTransientError`; one-transient-one-permanent →
      transient; assert gallery-dl security flags present
- [ ] run tests — must pass before Task 6

### Task 6: `configure()` + `__main__` wiring + env-var fallback
- [ ] add `configure(*, max_file_bytes=None, download_timeout_s=None)` to
      `TikTokProvider` mirroring the Instagram pattern (NO cookies/cookie_health
      params); add `_effective_max_bytes()` / `_effective_timeout()` helpers
      that fall back to `YOINK_MAX_FILE_MB` / `YOINK_DOWNLOAD_TIMEOUT_S`
      env vars then to sane defaults
- [ ] edit `src/yoink/__main__.py`: import `provider as tiktok_provider`; add
      `tiktok_provider.configure(max_file_bytes=settings.max_file_mb*1024*1024,
      download_timeout_s=float(settings.download_timeout_s))` immediately
      before `ProviderRegistry.autodiscover()`
- [ ] write tests: `configure(...)` applies runtime max-bytes/timeout to the
      singleton; env-var fallback for timeout when unset; explicit ctor/config
      value overrides the env var
- [ ] run tests — must pass before Task 7

### Task 7: Registry + allowlist integration test
- [ ] add `tests/integration/test_tiktok_registry.py`: `autodiscover()`
      registers the `tiktok` provider; `known_domains` contains all 5 TikTok
      hosts; `registry.find("https://www.tiktok.com/@u/video/1")` returns the
      TikTok provider and `find` for a short link returns it too
- [ ] write test: a TikTok host passes `validate_url` when present in the
      allowlist derived from `known_domains`
- [ ] run full unit + integration suite — must pass before Task 8

### Task 8: Verify acceptance criteria
- [ ] verify all Overview requirements implemented (video + slideshow, yt-dlp
      primary → gallery-dl fallback, no cookies, autodiscovered, no core edits
      beyond `__main__`/docs)
- [ ] verify edge cases handled (short links, numeric slideshow order,
      MediaTooLarge, transient vs permanent)
- [ ] run full test suite: `pytest -q`
- [ ] run `ruff check` — all issues fixed
- [ ] run `mypy src` — clean under `--strict`
- [ ] verify coverage gate: `pytest -q --cov=src/yoink --cov-fail-under=80`

### Task 9: [Final] Update documentation
- [ ] update `README.md` — list TikTok alongside Instagram as a supported
      platform (and note slideshow support, no-cookie/public-only caveat)
- [ ] update `CLAUDE.md` — change the "(Instagram in MVP)" line to mention
      TikTok; note the inverted tool order for the TikTok provider in the
      provider section
- [ ] update `.env.example` if it enumerates supported platforms (no new env
      vars are introduced)

*Note: ralphex automatically moves completed plans to `docs/plans/completed/`.*

## Technical Details
- **Module shape** (`src/yoink/providers/tiktok.py`), mirroring Instagram but
  with yt-dlp first:
  ```python
  class TikTokProvider:
      name = "tiktok"
      domains = frozenset({
          "tiktok.com", "www.tiktok.com", "m.tiktok.com",
          "vm.tiktok.com", "vt.tiktok.com",
      })
      def can_handle(self, url: str) -> bool: ...
      async def fetch(self, url: str, workdir: Path) -> MediaPackage:
          # mkdir → purge → yt-dlp → collect → (on fail: purge → gallery-dl)
          #   → enforce_size → MediaPackage
      def configure(self, *, max_file_bytes=None, download_timeout_s=None): ...

  provider = TikTokProvider()
  ```
- **Host normalization**: lowercase + strip a single leading `www.` only, to
  match `core/registry.py`'s normalization (so `find()` dispatch and
  `can_handle()` agree on what counts as a known host).
- **MediaKind mapping**: image extensions (`.jpg/.jpeg/.png/.webp/.heic`) →
  `"photo"`; `.mp4/.mov/.webm` → `"video"`. Reuse the Instagram extension sets
  / probe helper if it is module-private — extract a shared util only if it is
  cleaner than duplicating; default to duplication to avoid coupling two
  providers (decide during Task 3, note here if extracted).
- **Timeouts/limits**: reuse `YOINK_DOWNLOAD_TIMEOUT_S` and `YOINK_MAX_FILE_MB`
  via `configure(...)` + env fallback. No new pydantic settings.
- **Security**: `--ignore-config` (yt-dlp) / `--config-ignore` (gallery-dl),
  `--` argument-injection guard, `--no-part` / `--no-progress`. The provider
  never builds a shell string and never calls `subprocess` directly.

## Post-Completion
*Items requiring manual intervention or external systems — informational only*

**Manual / live verification**:
- Run the bot locally (`python -m yoink`) and paste real URLs: a normal
  `/@user/video/<id>`, a `vm.tiktok.com` short link, and a `/@user/photo/<id>`
  slideshow; confirm correct re-upload, slideshow order, and that an
  oversized video is rejected gracefully.
- Confirm a region-locked/private TikTok link fails with a clean
  `ProviderError` (logged + skipped, not a crash) — expected, since no cookies.

**Deployment**:
- No new env vars or secrets. Ensure the deployed image still has `yt-dlp`,
  `gallery-dl`, and `ffmpeg` on PATH (already required by the Dockerfile).
- Consider pinning/bumping `yt-dlp` in the image — TikTok extractors break
  often upstream; a stale `yt-dlp` is the most likely future failure mode.
