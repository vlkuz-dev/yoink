# yoink

Telegram bot that detects media URLs in chat messages, downloads the media, and re-uploads it inline. Instagram is the MVP provider; additional platforms drop in as single-file modules.

> **Status:** Bootstrapping. See `docs/plans/20260516-yoink-telegram-media-bot.md` for the full implementation plan.

## Quick start (dev)

```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e ".[dev]"
cp .env.example .env  # fill YOINK_BOT_TOKEN
python -m yoink
```

## Tests

```bash
pytest -q
ruff check
mypy src
```

## Configuration

All environment variables are prefixed `YOINK_`. See `.env.example` for the full list.
