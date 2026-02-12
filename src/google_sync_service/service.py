from __future__ import annotations

from datetime import UTC, datetime
import logging

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from google_sync_service.auth import AuthError, GoogleAuthManager
from google_sync_service.calendar import CalendarSyncManager
from google_sync_service.config import Settings
from google_sync_service.gmail import GmailSyncManager
from google_sync_service.schemas import (
    AuthStartRequest,
    AuthStartResponse,
    AuthStatusResponse,
    CalendarEvent,
    CalendarSyncRequest,
    CalendarSyncResponse,
    GmailSyncRequest,
    GmailSyncResponse,
    SignalsSummaryResponse,
)

logger = logging.getLogger(__name__)


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="google-sync-service", version="0.1.0")
    auth_manager = GoogleAuthManager(settings)
    gmail_manager = GmailSyncManager(settings, auth_manager)
    calendar_manager = CalendarSyncManager(settings, auth_manager)

    @app.get("/health")
    def health() -> dict[str, object]:
        status = auth_manager.status()
        return {
            "status": "ok",
            "service": "google-sync-service",
            "connected": status.connected,
            "configured": status.configured,
            "now": datetime.now(UTC).isoformat(),
        }

    @app.get("/", response_class=HTMLResponse)
    def root() -> str:
        return (
            "<html><body style='font-family: -apple-system, sans-serif; padding: 20px;'>"
            "<h2>Google Sync Service</h2>"
            "<p>Read-only Gmail + Calendar sync for personal assistant signals.</p>"
            "<p><a href='/connect'>Connect Google Account</a></p>"
            "</body></html>"
        )

    @app.get("/connect")
    def connect_page() -> RedirectResponse:
        try:
            auth = auth_manager.start_authorization(open_browser=False)
        except AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(auth.auth_url)

    @app.get("/v1/auth/status", response_model=AuthStatusResponse)
    def auth_status() -> AuthStatusResponse:
        return auth_manager.status()

    @app.post("/v1/auth/start", response_model=AuthStartResponse)
    def auth_start(request: AuthStartRequest) -> AuthStartResponse:
        try:
            return auth_manager.start_authorization(open_browser=request.open_browser)
        except AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/v1/auth/callback", response_class=HTMLResponse)
    def auth_callback(
        state: str = Query(...),
        code: str | None = Query(default=None),
        error: str | None = Query(default=None),
    ) -> str:
        try:
            auth_manager.complete_authorization(state=state, code=code, error=error)
        except AuthError as exc:
            logger.warning("OAuth callback failed: %s", exc)
            return (
                "<html><body style='font-family: -apple-system, sans-serif; padding: 20px;'>"
                "<h3>Google connect failed</h3>"
                f"<p>{str(exc)}</p>"
                "<p>You can close this tab and retry.</p>"
                "</body></html>"
            )

        return (
            "<html><body style='font-family: -apple-system, sans-serif; padding: 20px;'>"
            "<h3>Google account connected</h3>"
            "<p>Authorization complete. You can close this tab.</p>"
            "<script>setTimeout(function(){ window.close(); }, 1500);</script>"
            "</body></html>"
        )

    @app.post("/v1/gmail/sync", response_model=GmailSyncResponse)
    def gmail_sync(request: GmailSyncRequest) -> GmailSyncResponse:
        try:
            return gmail_manager.sync(request)
        except AuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Gmail sync failed: {exc}") from exc

    @app.get("/v1/gmail/latest", response_model=GmailSyncResponse)
    def gmail_latest() -> GmailSyncResponse:
        payload = gmail_manager.load_cache()
        if payload is None:
            raise HTTPException(status_code=404, detail="No Gmail cache yet. Run /v1/gmail/sync first.")
        return payload

    @app.get("/v1/signals/summary", response_model=SignalsSummaryResponse)
    def signals_summary(window_hours: int = 24) -> SignalsSummaryResponse:
        return gmail_manager.summary(window_hours=window_hours)

    @app.post("/v1/calendar/sync", response_model=CalendarSyncResponse)
    def calendar_sync(request: CalendarSyncRequest) -> CalendarSyncResponse:
        try:
            return calendar_manager.sync(request)
        except AuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Calendar sync failed: {exc}") from exc

    @app.get("/v1/calendar/latest", response_model=CalendarSyncResponse)
    def calendar_latest() -> CalendarSyncResponse:
        payload = calendar_manager.load_cache()
        if payload is None:
            raise HTTPException(status_code=404, detail="No calendar cache yet. Run /v1/calendar/sync first.")
        return payload

    @app.get("/v1/calendar/events", response_model=list[CalendarEvent])
    def calendar_events(
        time_min: datetime | None = Query(default=None),
        time_max: datetime | None = Query(default=None),
        calendar_id: str | None = Query(default=None),
        max_results: int | None = Query(default=None, ge=1, le=2000),
        query: str | None = Query(default=None),
        use_cache: bool = Query(default=True),
    ) -> list[CalendarEvent]:
        try:
            return calendar_manager.events(
                time_min=time_min,
                time_max=time_max,
                calendar_id=calendar_id,
                max_results=max_results,
                query=query,
                use_cache=use_cache,
            )
        except AuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Calendar event fetch failed: {exc}") from exc

    return app
