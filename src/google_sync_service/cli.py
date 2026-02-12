from __future__ import annotations

import argparse
import sys
import time

import httpx

from google_sync_service.config import settings


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="google-sync-connect",
        description="Start browser OAuth flow for google-sync-service and wait for completion.",
    )
    parser.add_argument(
        "--base-url",
        default=f"http://{settings.host}:{settings.port}",
        help="Base URL for google-sync-service.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=180,
        help="Max seconds to wait for completed authorization.",
    )
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    timeout = max(15, args.timeout_seconds)

    try:
        with httpx.Client(timeout=10.0) as http:
            health = http.get(f"{base_url}/health")
            health.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        print(
            f"google-sync-service is not reachable at {base_url}: {exc}\n"
            "Start it first with: google-sync-service",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc

    with httpx.Client(timeout=20.0) as http:
        response = http.post(f"{base_url}/v1/auth/start", json={"open_browser": True})
        response.raise_for_status()
        payload = response.json()

        auth_url = str(payload.get("auth_url") or "")
        opened = bool(payload.get("opened_browser"))
        if opened:
            print("Opened browser for Google consent.")
        else:
            print("Browser did not open automatically. Visit this URL:")
            print(auth_url)

        deadline = time.time() + timeout
        while time.time() < deadline:
            status_resp = http.get(f"{base_url}/v1/auth/status")
            status_resp.raise_for_status()
            status = status_resp.json()
            if bool(status.get("connected")):
                print("Google authorization complete.")
                return
            time.sleep(2)

    print(
        "Authorization timed out. Re-run google-sync-connect or visit /connect in your browser.",
        file=sys.stderr,
    )
    raise SystemExit(2)


if __name__ == "__main__":
    main()
