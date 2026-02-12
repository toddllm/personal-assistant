from __future__ import annotations

import uvicorn

from audio_assist.logging_setup import configure_service_logging
from speaker_service.config import settings
from speaker_service.service import create_app


def main() -> None:
    configure_service_logging(
        service_name="speaker_service",
        level=settings.log_level,
        json_logs=settings.log_json,
        log_file_path=settings.log_file_path,
        log_file_max_bytes=settings.log_file_max_bytes,
        log_file_backup_count=settings.log_file_backup_count,
    )
    app = create_app(settings)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        access_log=settings.access_log,
        log_config=None,
    )


if __name__ == "__main__":
    main()
