# Contributing

Thanks for your interest in `yoink`. This project is small enough that the
contribution loop is short — fork, branch, PR — and we keep tooling minimal.

## Dev setup

```bash
uv venv --python 3.11
source .venv/bin/activate
uv pip install -e ".[dev]"
cp .env.example .env  # fill YOINK_BOT_TOKEN if you want to run the bot
```

Runtime requires `gallery-dl`, `yt-dlp`, and `ffmpeg` (for `ffprobe`) on
PATH for live runs; tests stub the subprocess layer.

## Workflow

1. Open or comment on an issue describing what you want to change.
2. Cut a branch from `main`.
3. Make the change with tests.
4. Run the full local gate before pushing:

   ```bash
   ruff check
   mypy src
   pytest -q --cov=src/yoink --cov-fail-under=80
   ```

5. Open a PR. Keep it focused — one logical change per PR. Mixing
   refactors with behaviour changes makes review harder and gets pushed
   back.

## Coding conventions

- Python 3.11+, `src/`-layout, package `yoink`.
- `ruff` (line length 120, target `py311`, rules `E,F,I,B,UP,SIM,N,RUF`)
  and `mypy --strict` against `src/` are the floor. CI mirrors local.
- Dataclasses use `slots=True`. Public APIs are typed end-to-end.
- Logging is `structlog`; never `print` outside `__main__` startup output.
- Never call `subprocess` with `shell=True`. Use
  `downloader.runner.run_subprocess` (`asyncio.create_subprocess_exec`
  under the hood).
- New external URLs the bot fetches must pass
  `downloader.safety.validate_url` (rejects private IP ranges, non-HTTPS,
  userinfo, off-list ports). Adding a new fetch path without that check
  is a security bug.
- aiogram 3 dependency injection: shared services (`pipeline`, `settings`,
  `cache`) live on `Dispatcher.workflow_data`. Handlers take them as
  kwargs by name; do not import singletons.
- Tests live under `tests/unit` and `tests/integration`. `pytest-asyncio`
  is in `asyncio_mode = "auto"`. `filterwarnings = ["error"]` — warnings
  break the build.

## Adding a provider

Drop a single module under `src/yoink/providers/` exporting a module-level
`provider` instance that satisfies `providers.base.Provider`.
`core.registry.ProviderRegistry.autodiscover()` finds it via
`pkgutil.iter_modules` and indexes by domain — no core edits needed.

When testing:

- Mock `yoink.downloader.runner.run_subprocess` with a `SubprocessResult`
  fixture; do **not** spawn real `gallery-dl` / `yt-dlp` in CI.
- Synthesise the on-disk artifacts the real subprocess would write into
  the `tmp_path` fixture so `fetch()` can resolve them.
- Cover: single-item happy path, multi-item (carousel) order preservation,
  fallback path (primary tool fails or returns zero items), `MediaTooLarge`
  when a synthesised file exceeds `YOINK_MAX_FILE_MB`, and `can_handle()`
  accept/reject cases.
- Mark anything that actually touches the network with
  `@pytest.mark.live` and skip by default.

`tests/unit/test_instagram_provider.py` is the reference pattern.

## Security

Report vulnerabilities privately — see `SECURITY.md`. Do not file public
issues for unpatched issues.
