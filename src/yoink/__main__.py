from __future__ import annotations

import sys

from yoink.config import Settings
from yoink.log import configure_logging, get_logger


def main() -> int:
    settings = Settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)
    log = get_logger("yoink")
    log.info(
        "yoink starting",
        workers=settings.workers,
        queue_maxsize=settings.queue_maxsize,
        log_format=settings.log_format,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
