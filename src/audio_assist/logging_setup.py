from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
from typing import Any


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=True)


def _normalize_level(value: str | int | None) -> int:
    if isinstance(value, int):
        return value
    name = str(value or "INFO").strip().upper()
    return int(getattr(logging, name, logging.INFO))


def configure_service_logging(
    *,
    service_name: str,
    level: str | int = "INFO",
    json_logs: bool = False,
    log_file_path: Path | None = None,
    log_file_max_bytes: int = 10 * 1024 * 1024,
    log_file_backup_count: int = 7,
) -> None:
    resolved_level = _normalize_level(level)

    formatter: logging.Formatter
    if json_logs:
        formatter = JsonLogFormatter()
    else:
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    root_logger = logging.getLogger()
    root_logger.setLevel(resolved_level)
    root_logger.handlers.clear()

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(resolved_level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    if log_file_path is not None:
        target = Path(log_file_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            filename=target,
            maxBytes=max(1_000_000, int(log_file_max_bytes)),
            backupCount=max(1, int(log_file_backup_count)),
            encoding="utf-8",
        )
        file_handler.setLevel(resolved_level)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    logging.getLogger(service_name).info(
        "Logging configured (level=%s json=%s file=%s)",
        logging.getLevelName(resolved_level),
        bool(json_logs),
        str(log_file_path) if log_file_path else "disabled",
    )
