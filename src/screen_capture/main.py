"""Entry point for the screen capture service."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from .config import Settings


def configure_service_logging(settings: Settings) -> None:
    """Configure logging for the screen capture service."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))

    fmt = "%(asctime)s %(levelname)-8s %(name)s  %(message)s"
    formatter = logging.Formatter(fmt)

    # Console handler
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    root.addHandler(console)

    # File handler
    settings.log_file_path.parent.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(
        str(settings.log_file_path),
        maxBytes=settings.log_file_max_bytes,
        backupCount=settings.log_file_backup_count,
    )
    fh.setFormatter(formatter)
    root.addHandler(fh)


def main() -> None:
    settings = Settings()
    configure_service_logging(settings)

    import uvicorn

    from .service import create_app

    app = create_app(settings)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
