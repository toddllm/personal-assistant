#!/usr/bin/env python3
"""Twilio test caller for audio pipeline validation.

Two modes:
  --message TEXT        Call and speak a TTS phrase (pipeline test)
  --status SID          Check call status

Works with direct phone numbers or Zoom dial-in (--zoom-number + --meeting-id).

For interactive AI conversations, use scripts/ai-call.py instead.

Credentials loaded from env vars, then falls back to ~/mark-sebast/apps/phone-agent/.env
"""

import argparse
import base64
import json
import os
import ssl
import sys
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# Credential loading
# ---------------------------------------------------------------------------

_ENV_FILE = os.path.expanduser("~/mark-sebast/apps/phone-agent/.env")


def _load_env_file(path: str) -> dict[str, str]:
    """Parse a .env file into a dict."""
    result = {}
    if not os.path.isfile(path):
        return result
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip().strip("'\"")
    return result


def _get_cred(name: str, env_fallback: dict[str, str] | None = None) -> str | None:
    """Get a credential from env var, then fallback dict."""
    val = os.environ.get(name)
    if val:
        return val
    if env_fallback:
        return env_fallback.get(name)
    return None


def load_credentials() -> dict[str, str]:
    """Load all credentials, env vars first, then .env file fallback."""
    fallback = _load_env_file(_ENV_FILE)
    creds = {}
    for key in (
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "TWILIO_PHONE_NUMBER",
    ):
        val = _get_cred(key, fallback)
        if val:
            creds[key] = val
    return creds


# ---------------------------------------------------------------------------
# Twilio REST API (raw HTTP, no dependencies)
# ---------------------------------------------------------------------------

_SSL_CTX = ssl.create_default_context()


def twilio_request(
    method: str,
    path: str,
    account_sid: str,
    auth_token: str,
    data: dict | None = None,
) -> dict:
    """Make a request to the Twilio REST API."""
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/{path}"
    auth = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()
    headers = {"Authorization": f"Basic {auth}"}

    body = None
    if data:
        body = urllib.parse.urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, context=_SSL_CTX) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# TwiML builders
# ---------------------------------------------------------------------------


def build_message_twiml(message: str) -> str:
    """Build TwiML that speaks a message and hangs up."""
    # Escape XML special characters
    safe = message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        '<Pause length="1"/>'
        f'<Say voice="Polly.Matthew">{safe}</Say>'
        '<Pause length="2"/>'
        "<Hangup/>"
        "</Response>"
    )


def build_zoom_twiml(message: str) -> str:
    """Build TwiML for Zoom — longer initial pause for the IVR."""
    safe = message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        '<Pause length="3"/>'
        f'<Say voice="Polly.Matthew">{safe}</Say>'
        '<Pause length="2"/>'
        "<Hangup/>"
        "</Response>"
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

DEFAULT_MESSAGE = (
    "Hello, this is an automated test of the audio capture pipeline. "
    "The quick brown fox jumps over the lazy dog. "
    "One two three four five six seven eight nine ten. "
    "Test complete."
)


def cmd_message(args, creds: dict) -> None:
    """Make a call with a TTS message."""
    sid = creds["TWILIO_ACCOUNT_SID"]
    token = creds["TWILIO_AUTH_TOKEN"]
    from_number = creds["TWILIO_PHONE_NUMBER"]
    message = args.message or DEFAULT_MESSAGE

    # Determine target and params
    params: dict[str, str] = {
        "From": from_number,
    }

    if args.zoom_number:
        params["To"] = args.zoom_number
        params["Twiml"] = build_zoom_twiml(message)
        # DTMF: waits, then meeting ID + #, wait, then passcode if provided
        meeting_id = args.meeting_id.replace(" ", "").replace("-", "")
        if args.passcode:
            passcode = args.passcode.replace(" ", "").replace("-", "")
            params["SendDigits"] = f"wwww{meeting_id}#wwwwww*{passcode}#"
        else:
            params["SendDigits"] = f"wwww{meeting_id}#ww#"
        print(f"Calling Zoom: {args.zoom_number} (meeting {meeting_id})")
    else:
        params["To"] = args.phone
        params["Twiml"] = build_message_twiml(message)
        print(f"Calling: {args.phone}")

    print(f"Message: {message[:80]}{'...' if len(message) > 80 else ''}")

    result = twilio_request("POST", "Calls.json", sid, token, params)
    call_sid = result.get("sid", "unknown")
    status = result.get("status", "unknown")
    print(f"Call SID: {call_sid}")
    print(f"Status: {status}")
    print(f"\nCheck status: scripts/test-call.py --status {call_sid}")


def cmd_status(args, creds: dict) -> None:
    """Check call status."""
    sid = creds["TWILIO_ACCOUNT_SID"]
    token = creds["TWILIO_AUTH_TOKEN"]
    call_sid = args.status

    result = twilio_request("GET", f"Calls/{call_sid}.json", sid, token)
    print(f"SID:       {result.get('sid')}")
    print(f"Status:    {result.get('status')}")
    print(f"Direction: {result.get('direction')}")
    print(f"From:      {result.get('from_formatted')}")
    print(f"To:        {result.get('to_formatted')}")
    print(f"Duration:  {result.get('duration', '?')}s")
    print(f"Start:     {result.get('start_time', '?')}")
    print(f"End:       {result.get('end_time', '?')}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Twilio test caller for audio pipeline validation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Speak a test message to a phone number
  %(prog)s --phone "+15551234567" --message "Testing one two three"

  # Call into a Zoom meeting with a test message
  %(prog)s --zoom-number "+16465588656" --meeting-id "1234567890"

  # Check call status
  %(prog)s --status "CA..."

For interactive AI conversations, use scripts/ai-call.py instead.
""",
    )
    parser.add_argument("--phone", help="Phone number to call (E.164 format)")
    parser.add_argument("--zoom-number", help="Zoom dial-in number")
    parser.add_argument("--meeting-id", help="Zoom meeting ID")
    parser.add_argument("--passcode", help="Zoom meeting passcode")
    parser.add_argument("--message", help="TTS message to speak (default: test phrase)")
    parser.add_argument("--status", help="Check status of a call by SID", metavar="CALL_SID")

    args = parser.parse_args()

    # Validate args
    if not args.status and not args.phone and not args.zoom_number:
        parser.print_help()
        sys.exit(1)

    if args.zoom_number and not args.meeting_id:
        print("Error: --meeting-id required with --zoom-number")
        sys.exit(1)

    # Load credentials
    creds = load_credentials()
    missing = []
    for key in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER"):
        if key not in creds:
            missing.append(key)
    if missing:
        print(f"Error: Missing credentials: {', '.join(missing)}")
        print(f"Set as env vars or ensure {_ENV_FILE} exists")
        sys.exit(1)

    # Dispatch
    if args.status:
        cmd_status(args, creds)
    else:
        cmd_message(args, creds)


if __name__ == "__main__":
    main()
