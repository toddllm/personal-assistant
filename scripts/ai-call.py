#!/usr/bin/env python3
"""Interactive AI caller — joins a Zoom meeting or calls a phone number.

Architecture (Zoom mode — Conference bridge):

    Zoom Meeting
        ↕  (PSTN + DTMF)
    [Twilio Leg 1]  ──→  Conference "ai-zoom-{id}"  ←──  [Twilio Leg 2]
                           (echo cancellation + mixing)        ↕
                                                         Vapi AI Assistant
                                                       (ElevenLabs + Groq)

  Leg 1: Twilio calls Zoom dial-in, enters meeting via DTMF, joins Conference
  Leg 2: Twilio calls Vapi inbound number, joins same Conference
  Conference handles echo cancellation between the two legs.

For direct calls: Vapi calls the number directly with the AI assistant.

Usage:
  # AI joins a Zoom meeting (max 3 minutes)
  scripts/ai-call.py --zoom-number "+16465588656" --meeting-id "81474349298" --passcode "560109"

  # AI calls a phone number directly
  scripts/ai-call.py --phone "+15551234567"

  # Custom prompt and max duration
  scripts/ai-call.py --phone "+15551234567" --prompt "You are a pirate" --max-duration 60

  # Check call status
  scripts/ai-call.py --status CA1234567890abcdef

Credentials loaded from env vars or ~/mark-sebast/apps/phone-agent/.env
"""

import argparse
import base64
import json
import os
import ssl
import sys
import time
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# Credential loading
# ---------------------------------------------------------------------------

_ENV_FILE = os.path.expanduser("~/mark-sebast/apps/phone-agent/.env")
_SSL_CTX = ssl.create_default_context()


def _load_env_file(path: str) -> dict[str, str]:
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


def load_credentials() -> dict[str, str]:
    fallback = _load_env_file(_ENV_FILE)
    creds = {}
    for key in (
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "TWILIO_PHONE_NUMBER",
        "VAPI_API_KEY",
        "VAPI_PHONE_NUMBER_ID",
    ):
        val = os.environ.get(key) or fallback.get(key)
        if val:
            creds[key] = val
    return creds


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------


def twilio_request(method, path, account_sid, auth_token, data=None):
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


def vapi_request(method, endpoint, api_key, payload=None):
    """Make a request to the Vapi API using subprocess curl.

    urllib sends headers that Vapi rejects with 403, so we use curl
    which matches the working manual invocation exactly.
    """
    import subprocess

    url = f"https://api.vapi.ai{endpoint}"
    cmd = ["curl", "-sS", "-X", method, "-H", f"Authorization: Bearer {api_key}"]

    if payload is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(payload)]

    cmd.append(url)

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        print(f"curl error: {result.stderr}")
        raise RuntimeError(f"curl failed: {result.stderr}")

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"Unexpected response: {result.stdout[:500]}")
        raise


# ---------------------------------------------------------------------------
# Default assistant config
# ---------------------------------------------------------------------------

DEFAULT_PROMPT = (
    "You are a friendly, helpful AI assistant who has joined a call. "
    "Have a natural conversation. Be concise — keep responses to 1-2 sentences. "
    "Listen carefully and respond naturally. "
    "If asked who you are, say you're an AI assistant here to help."
)

# Vapi inbound number — answers with the AI assistant configured on the phone number
VAPI_INBOUND_NUMBER = "+15183189215"
VAPI_INBOUND_PHONE_ID = "79b4be41-be3c-45b3-bfad-aca166214570"
VAPI_INBOUND_ASSISTANT_ID = "f4cac0a6-4dfc-4d18-b82b-162cb030c544"


def build_assistant(system_prompt: str, max_duration: int) -> dict:
    """Build inline Vapi assistant config."""
    return {
        "model": {
            "provider": "groq",
            "model": "llama-3.3-70b-versatile",
            "messages": [{"role": "system", "content": system_prompt}],
            "temperature": 0.5,
        },
        "voice": {
            "provider": "11labs",
            "voiceId": "EXAVITQu4vr4xnSDxMaL",
            "stability": 0.3,
            "similarityBoost": 0.75,
            "style": 0.5,
            "useSpeakerBoost": True,
        },
        "firstMessage": "Hello! I'm an AI assistant joining the call. How can I help?",
        "firstMessageMode": "assistant-speaks-first",
        "backgroundSound": "off",
        "endCallFunctionEnabled": False,
        "maxDurationSeconds": max_duration,
        "silenceTimeoutSeconds": 60,
        "responseDelaySeconds": 0.4,
        "recordingEnabled": True,
        "name": "AI Call Assistant",
    }


def update_vapi_phone_assistant(api_key: str, phone_number_id: str,
                                system_prompt: str, max_duration: int) -> dict:
    """Update the saved assistant used by the Vapi inbound number.

    PATCHes the assistant directly (Vapi ignores inline assistant overrides
    on phone numbers that have an assistantId set).
    """
    return vapi_request("PATCH", f"/assistant/{VAPI_INBOUND_ASSISTANT_ID}", api_key,
                        build_assistant(system_prompt, max_duration))


# ---------------------------------------------------------------------------
# Direct phone call (Vapi outbound)
# ---------------------------------------------------------------------------


def call_phone(phone: str, system_prompt: str, max_duration: int, creds: dict):
    """Vapi calls a phone number directly with AI assistant."""
    print(f"Calling {phone} with AI assistant (max {max_duration}s)...")

    result = vapi_request("POST", "/call/phone", creds["VAPI_API_KEY"], {
        "phoneNumberId": creds["VAPI_PHONE_NUMBER_ID"],
        "customer": {"number": phone},
        "assistant": build_assistant(system_prompt, max_duration),
    })

    call_id = result.get("id", "unknown")
    status = result.get("status", "unknown")
    print(f"Vapi Call ID: {call_id}")
    print(f"Status: {status}")
    print(f"Max duration: {max_duration}s")
    return call_id


# ---------------------------------------------------------------------------
# Zoom call (Twilio DTMF + Vapi via conference bridge)
# ---------------------------------------------------------------------------


def call_zoom(
    zoom_number: str,
    meeting_id: str,
    passcode: str | None,
    system_prompt: str,
    max_duration: int,
    creds: dict,
):
    """Join a Zoom meeting with an AI assistant via Conference bridge.

    1. PATCH Vapi phone number's assistant config (prompt, voice, duration)
    2. Leg 1: Twilio calls Zoom dial-in with SendDigits for DTMF → Conference
    3. Wait 5s, then Leg 2: Twilio calls Vapi inbound → same Conference
    Conference handles echo cancellation; endConferenceOnExit tears down both.
    """
    sid = creds["TWILIO_ACCOUNT_SID"]
    token = creds["TWILIO_AUTH_TOKEN"]
    from_number = creds.get("_from_override") or creds["TWILIO_PHONE_NUMBER"]
    meeting_id_clean = meeting_id.replace(" ", "").replace("-", "")
    conf_name = f"ai-zoom-{meeting_id_clean}"

    # Step 1: Update Vapi inbound number's assistant config
    print("Step 1: Configuring Vapi AI assistant on inbound number...")
    try:
        update_vapi_phone_assistant(
            creds["VAPI_API_KEY"], VAPI_INBOUND_PHONE_ID,
            system_prompt, max_duration,
        )
        print("  Vapi assistant updated on +15183189215.")
    except Exception as exc:
        print(f"  Warning: Failed to update Vapi assistant: {exc}")
        print("  Proceeding — Vapi will use its current config.")

    # Step 2: Leg 1 — Twilio calls Zoom, enters meeting via DTMF, joins Conference
    zoom_twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        '<Pause length="6"/>'
        f'<Dial><Conference endConferenceOnExit="true" '
        f'maxParticipants="2">{conf_name}</Conference></Dial>'
        "</Response>"
    )

    dtmf = f"wwww{meeting_id_clean}#"
    if passcode:
        passcode_clean = passcode.replace(" ", "").replace("-", "")
        dtmf += f"wwwwww*{passcode_clean}#"
    else:
        dtmf += "ww#"

    print(f"\nStep 2: Leg 1 — Twilio calling Zoom {zoom_number} (meeting {meeting_id_clean})...")
    zoom_result = twilio_request("POST", "Calls.json", sid, token, {
        "From": from_number,
        "To": zoom_number,
        "Twiml": zoom_twiml,
        "SendDigits": dtmf,
        "Timeout": "30",
    })
    zoom_sid = zoom_result.get("sid", "unknown")
    print(f"  Zoom call SID: {zoom_sid}")
    print(f"  Status: {zoom_result.get('status')}")

    # Step 3: Leg 2 — wait for Zoom IVR + DTMF to complete before calling Vapi
    print(f"\nStep 3: Waiting 15s for Zoom IVR + DTMF...")
    time.sleep(15)

    vapi_twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f'<Dial><Conference endConferenceOnExit="true" '
        f'maxParticipants="2">{conf_name}</Conference></Dial>'
        "</Response>"
    )

    print(f"  Leg 2 — Twilio calling Vapi inbound ({VAPI_INBOUND_NUMBER})...")
    vapi_result = twilio_request("POST", "Calls.json", sid, token, {
        "From": from_number,
        "To": VAPI_INBOUND_NUMBER,
        "Twiml": vapi_twiml,
        "Timeout": "30",
    })
    vapi_sid = vapi_result.get("sid", "unknown")
    print(f"  Vapi call SID: {vapi_sid}")
    print(f"  Status: {vapi_result.get('status')}")

    print(f"\nConference bridge established: {conf_name}")
    print(f"  Zoom leg:  {zoom_sid}")
    print(f"  Vapi leg:  {vapi_sid}")
    print(f"  Max duration: {max_duration}s")
    print(f"\nCheck status:")
    print(f"  scripts/ai-call.py --status {zoom_sid}")
    print(f"  scripts/ai-call.py --status {vapi_sid}")

    return zoom_sid, vapi_sid


# ---------------------------------------------------------------------------
# Status check
# ---------------------------------------------------------------------------


def check_status(call_sid: str, creds: dict):
    """Check the status of a Twilio call by SID."""
    sid = creds["TWILIO_ACCOUNT_SID"]
    token = creds["TWILIO_AUTH_TOKEN"]

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


def main():
    parser = argparse.ArgumentParser(
        description="Interactive AI caller — Zoom meetings or direct phone calls",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--phone", help="Phone number to call directly (E.164)")
    parser.add_argument("--zoom-number", help="Zoom dial-in number")
    parser.add_argument("--meeting-id", help="Zoom meeting ID")
    parser.add_argument("--passcode", help="Zoom meeting passcode")
    parser.add_argument("--prompt", help="Custom system prompt for the AI")
    parser.add_argument("--max-duration", type=int, default=180,
                        help="Max call duration in seconds (default: 180)")
    parser.add_argument("--from-number", help="Override Twilio caller ID (E.164)")
    parser.add_argument("--status", help="Check call status by SID", metavar="CALL_SID")

    args = parser.parse_args()

    if not args.phone and not args.zoom_number and not args.status:
        parser.print_help()
        sys.exit(1)

    if args.zoom_number and not args.meeting_id:
        print("Error: --meeting-id required with --zoom-number")
        sys.exit(1)

    creds = load_credentials()

    # Status check only needs Twilio creds
    if args.status:
        missing = [k for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN") if k not in creds]
        if missing:
            print(f"Error: Missing credentials: {', '.join(missing)}")
            sys.exit(1)
        check_status(args.status, creds)
        return

    # Check required credentials
    required = ["VAPI_API_KEY", "VAPI_PHONE_NUMBER_ID"]
    if args.zoom_number:
        required += ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER"]
    missing = [k for k in required if k not in creds]
    if missing:
        print(f"Error: Missing credentials: {', '.join(missing)}")
        sys.exit(1)

    if args.from_number:
        creds["_from_override"] = args.from_number

    prompt = args.prompt or DEFAULT_PROMPT

    if args.zoom_number:
        call_zoom(
            args.zoom_number,
            args.meeting_id,
            args.passcode,
            prompt,
            args.max_duration,
            creds,
        )
    else:
        call_phone(args.phone, prompt, args.max_duration, creds)


if __name__ == "__main__":
    main()
