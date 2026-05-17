# Security policy

## Reporting a vulnerability

Open a private security advisory on GitHub:
<https://github.com/vlkuz-dev/yoink/security/advisories/new>.

Please include:

- A description of the issue and its impact.
- A reproduction recipe (URL, configuration, steps).
- Any logs or stack traces, with secrets redacted.

We aim to acknowledge reports within 7 days and to ship a fix or a documented
mitigation within 30 days for high-impact findings. Do not file public issues
for unpatched vulnerabilities.

## Threat model and known-sensitive areas

`yoink` runs an aiogram Telegram bot that shells out to `gallery-dl` and
`yt-dlp` to download user-supplied URLs and re-uploads the resulting media
through Telegram. The areas most worth your attention:

- **SSRF / URL handling** — `src/yoink/downloader/safety.py` validates every
  URL before it reaches the subprocess: scheme allowlist, port allowlist,
  userinfo rejection, literal-IP blocklist (private + loopback + link-local
  + ULA + CGNAT + reserved ranges), optional host allowlist. Bypasses here
  are the highest-impact bug class.
- **Subprocess construction** — `src/yoink/downloader/runner.py` is the only
  way the codebase launches external processes; it forbids `shell=True` and
  uses `asyncio.create_subprocess_exec` with explicit argv. Anything that
  reaches `subprocess.run` / `Popen` directly outside this module is a bug.
- **Cookie file handling** — Instagram cookies (`YOINK_IG_COOKIES_FILE`)
  grant session-level access to the account whose browser exported them.
  The file is mounted read-only and never logged. Treat any path that can
  influence it (env handling, container mounts) as security-sensitive.
- **Cache** — the SQLite cache stores Telegram `file_id` values; they are
  not secrets, but corruption could let a stale `file_id` be served for a
  changed URL. The schema is at `src/yoink/cache/schema.sql`.

## What is *not* in scope

- Telegram and Instagram terms of service. You are responsible for using
  this tool within their rules and the rules of whatever chats you deploy it
  to.
- Account bans, rate limits, and CAPTCHAs imposed by upstream services.
- Vulnerabilities in `gallery-dl`, `yt-dlp`, `ffmpeg`, `aiogram`, or any
  other third-party dependency. Report those upstream; we will pick up
  fixed versions in a normal dependency bump.
