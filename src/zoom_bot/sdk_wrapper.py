"""Wrapper around py-zoom-meeting-sdk for joining meetings and raw audio I/O.

The Zoom Meeting SDK only runs on Linux x86_64.  This module is imported
inside the OrbStack amd64 VM where the SDK wheels are installed.

The SDK requires a GLib main loop for event dispatch.  This module runs
the GLib loop on a background thread and uses thread-safe signaling to
coordinate with the async FastAPI service.

Audio format: PCM 16-bit signed, 32 kHz, mono.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)

# Audio constants expected by the Zoom SDK
SAMPLE_RATE = 32000
SAMPLE_WIDTH = 2  # 16-bit
CHANNELS = 1


class ZoomBot:
    """Manages a single Zoom Meeting SDK session.

    All SDK calls happen on the GLib main loop thread.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        on_audio: Callable[[bytes], None] | None = None,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._on_audio = on_audio

        # SDK objects — must stay alive
        self._meeting_service = None
        self._auth_service = None
        self._setting_service = None
        self._audio_helper = None
        self._audio_source_cb = None
        self._audio_delegate_cb = None
        self._mic_event_cb = None
        self._auth_event_cb = None
        self._meeting_event_cb = None
        self._reminder_event_cb = None
        self._audio_ctrl = None
        self._audio_ctrl_event_cb = None
        self._recording_ctrl = None
        self._recording_event_cb = None
        self._audio_raw_data_sender = None

        # GLib main loop
        self._glib_loop = None
        self._glib_thread = None

        # Thread-safe events
        self._joined = threading.Event()
        self._left = threading.Event()
        self._error: str | None = None

        # Meeting params (set before init)
        self._meeting_id: str = ""
        self._passcode: str = ""

    @property
    def is_in_meeting(self) -> bool:
        return self._joined.is_set() and not self._left.is_set()

    def join(self, meeting_id: str, passcode: str = "", timeout: float = 30) -> None:
        """Initialize SDK, authenticate, and join a meeting.

        This is a blocking call — runs on the calling thread but dispatches
        SDK work to the GLib main loop thread.
        """
        import zoom_meeting_sdk as sdk
        import gi
        gi.require_version("GLib", "2.0")
        from gi.repository import GLib

        self._meeting_id = meeting_id.replace(" ", "").replace("-", "")
        self._passcode = passcode

        logger.info("Initializing Zoom SDK...")
        init_param = sdk.InitParam()
        init_param.strWebDomain = "https://zoom.us"
        init_param.strSupportUrl = "https://zoom.us"
        init_param.enableGenerateDump = False
        init_param.emLanguageID = sdk.SDK_LANGUAGE_ID.LANGUAGE_English
        init_param.enableLogByDefault = True

        err = sdk.InitSDK(init_param)
        if err != sdk.SDKERR_SUCCESS:
            raise RuntimeError(f"Zoom SDK init failed: {err}")

        # Create services and authenticate (callbacks will chain to join)
        self._create_services(sdk)

        # Start GLib main loop on background thread
        self._glib_loop = GLib.MainLoop()
        self._glib_thread = threading.Thread(
            target=self._run_glib_loop, daemon=True
        )
        self._glib_thread.start()

        # Wait for join (auth callback → join → meeting status callback)
        if not self._joined.wait(timeout=timeout):
            if self._error:
                raise RuntimeError(f"Failed to join meeting: {self._error}")
            raise RuntimeError("Timeout waiting to join meeting")

        if self._error:
            raise RuntimeError(f"Failed to join meeting: {self._error}")

        logger.info("Successfully joined meeting %s", self._meeting_id)

    def _run_glib_loop(self) -> None:
        """Run GLib main loop (blocking, on background thread)."""
        from gi.repository import GLib

        # Periodic keepalive (required for callback dispatch)
        GLib.timeout_add(100, self._glib_keepalive)

        try:
            self._glib_loop.run()
        except Exception:
            logger.exception("GLib main loop error")

    def _glib_keepalive(self) -> bool:
        """GLib timeout callback — keeps the loop alive."""
        if self._left.is_set():
            if self._glib_loop:
                self._glib_loop.quit()
            return False
        return True

    def _create_services(self, sdk) -> None:
        """Create SDK services and start authentication."""
        self._meeting_service = sdk.CreateMeetingService()
        self._setting_service = sdk.CreateSettingService()

        # Meeting status callback
        self._meeting_event_cb = sdk.MeetingServiceEventCallbacks(
            onMeetingStatusChangedCallback=self._on_meeting_status_changed,
        )
        self._meeting_service.SetEvent(self._meeting_event_cb)

        # Auth callback → triggers join on success
        self._auth_event_cb = sdk.AuthServiceEventCallbacks(
            onAuthenticationReturnCallback=self._on_auth_return,
        )
        self._auth_service = sdk.CreateAuthService()
        self._auth_service.SetEvent(self._auth_event_cb)

        # Authenticate
        auth_ctx = sdk.AuthContext()
        auth_ctx.jwt_token = self._generate_jwt()

        err = self._auth_service.SDKAuth(auth_ctx)
        if err != sdk.SDKERR_SUCCESS:
            raise RuntimeError(f"SDK auth request failed: {err}")
        logger.info("SDK auth request submitted")

    def _on_auth_return(self, result) -> None:
        """Called on GLib thread when auth completes."""
        import zoom_meeting_sdk as sdk

        logger.info("SDK auth result: %s", result)
        if result != sdk.AUTHRET_SUCCESS:
            self._error = f"Auth failed: {result}"
            self._joined.set()  # Unblock waiter
            return

        logger.info("Auth successful, joining meeting %s...", self._meeting_id)
        self._join_meeting(sdk)

    def _join_meeting(self, sdk) -> None:
        """Join the meeting (called from auth callback on GLib thread)."""
        join_param = sdk.JoinParam()
        join_param.userType = sdk.SDKUserType.SDK_UT_WITHOUT_LOGIN

        param = join_param.param
        param.meetingNumber = int(self._meeting_id)
        param.userName = "AI Assistant"
        param.psw = self._passcode
        param.isVideoOff = True
        param.isAudioOff = False
        param.isAudioRawDataStereo = False
        param.isMyVoiceInMix = False
        param.eAudioRawdataSamplingRate = sdk.AudioRawdataSamplingRate.AudioRawdataSamplingRate_32K

        err = self._meeting_service.Join(join_param)
        if err != sdk.SDKERR_SUCCESS:
            self._error = f"Join failed: {err}"
            self._joined.set()
            return

        # Enable auto-join audio
        audio_settings = self._setting_service.GetAudioSettings()
        if audio_settings:
            audio_settings.EnableAutoJoinAudio(True)

    def _on_meeting_status_changed(self, status, iResult=0) -> None:
        """Called on GLib thread when meeting status changes."""
        import zoom_meeting_sdk as sdk

        logger.info("Meeting status: %s (result=%s)", status, iResult)

        if status == sdk.MEETING_STATUS_INMEETING:
            self._on_joined(sdk)
        elif status == sdk.MEETING_STATUS_FAILED:
            self._error = f"Meeting failed: {iResult}"
            self._joined.set()
        elif status == sdk.MEETING_STATUS_ENDED:
            self._left.set()

    def _on_joined(self, sdk) -> None:
        """Set up audio and signal that we've joined."""
        from gi.repository import GLib

        logger.info("In meeting — setting up audio...")

        # Accept any reminders (e.g. recording consent)
        self._reminder_event_cb = sdk.MeetingReminderEventCallbacks(
            onReminderNotifyCallback=self._on_reminder_notify,
        )
        reminder_ctrl = self._meeting_service.GetMeetingReminderController()
        reminder_ctrl.SetEvent(self._reminder_event_cb)

        # Join VoIP (required for raw audio after SDK 6.3.5)
        self._audio_ctrl = self._meeting_service.GetMeetingAudioController()
        self._audio_ctrl.JoinVoip()

        # Start raw recording after a brief delay (need recording privilege)
        GLib.timeout_add_seconds(1, self._start_raw_recording)

        self._joined.set()

    def _start_raw_recording(self) -> bool:
        """Start raw audio recording (called from GLib timeout)."""
        import zoom_meeting_sdk as sdk

        self._recording_ctrl = self._meeting_service.GetMeetingRecordingController()

        def on_recording_privilege_changed(can_rec):
            logger.info("Recording privilege changed: %s", can_rec)
            if can_rec:
                from gi.repository import GLib
                GLib.timeout_add_seconds(1, self._start_raw_recording)

        self._recording_event_cb = sdk.MeetingRecordingCtrlEventCallbacks(
            onRecordPrivilegeChangedCallback=on_recording_privilege_changed,
        )
        self._recording_ctrl.SetEvent(self._recording_event_cb)

        can_start = self._recording_ctrl.CanStartRawRecording()
        if can_start != sdk.SDKERR_SUCCESS:
            logger.info("Requesting recording privilege...")
            self._recording_ctrl.RequestLocalRecordingPrivilege()
            return False  # Don't repeat GLib timeout

        err = self._recording_ctrl.StartRawRecording()
        if err != sdk.SDKERR_SUCCESS:
            logger.warning("StartRawRecording failed: %s", err)
            return False

        logger.info("Raw recording started")

        # Subscribe to audio
        self._audio_helper = sdk.GetAudioRawdataHelper()
        if self._audio_helper is None:
            logger.warning("GetAudioRawdataHelper returned None")
            return False

        if self._on_audio:
            self._audio_delegate_cb = sdk.ZoomSDKAudioRawDataDelegateCallbacks(
                onMixedAudioRawDataReceivedCallback=self._on_mixed_audio,
            )
            err = self._audio_helper.subscribe(self._audio_delegate_cb, False)
            logger.info("Audio subscribe result: %s", err)

        # Set up virtual mic for sending audio
        self._mic_event_cb = sdk.ZoomSDKVirtualAudioMicEventCallbacks(
            onMicInitializeCallback=self._on_mic_initialize,
            onMicStartSendCallback=self._on_mic_start_send,
            onMicStopSendCallback=self._on_mic_stop_send,
            onMicUninitializedCallback=self._on_mic_uninit,
        )
        err = self._audio_helper.setExternalAudioSource(self._mic_event_cb)
        logger.info("Set external audio source result: %s", err)

        return False  # Don't repeat GLib timeout

    def send_audio(self, pcm_data: bytes) -> None:
        """Send raw PCM audio into the meeting (thread-safe).

        Args:
            pcm_data: PCM 16-bit signed LE, 32kHz, mono
        """
        if not self._audio_raw_data_sender:
            return
        try:
            import zoom_meeting_sdk as sdk
            self._audio_raw_data_sender.send(
                pcm_data,
                SAMPLE_RATE,
                sdk.ZoomSDKAudioChannel_Mono,
            )
        except Exception:
            logger.exception("Failed to send audio to meeting")

    def leave(self) -> None:
        """Leave the meeting and clean up (blocking)."""
        import zoom_meeting_sdk as sdk

        if self._meeting_service:
            status = self._meeting_service.GetMeetingStatus()
            if status != sdk.MEETING_STATUS_IDLE:
                self._meeting_service.Leave(sdk.LEAVE_MEETING)
                time.sleep(2)

        self._left.set()

        if self._audio_helper:
            try:
                self._audio_helper.unSubscribe()
            except Exception:
                pass

        if self._meeting_service:
            try:
                sdk.DestroyMeetingService(self._meeting_service)
            except Exception:
                pass
        if self._setting_service:
            try:
                sdk.DestroySettingService(self._setting_service)
            except Exception:
                pass
        if self._auth_service:
            try:
                sdk.DestroyAuthService(self._auth_service)
            except Exception:
                pass

        try:
            sdk.CleanUPSDK()
        except Exception:
            pass

        self._meeting_service = None
        self._auth_service = None
        self._setting_service = None
        self._audio_helper = None
        self._audio_raw_data_sender = None

        logger.info("Left meeting and cleaned up SDK")

    # ------------------------------------------------------------------
    # JWT generation
    # ------------------------------------------------------------------

    def _generate_jwt(self) -> str:
        """Generate a JWT for SDK authentication."""
        import hashlib
        import hmac
        import json as _json
        import base64

        now = int(time.time())
        header = {"alg": "HS256", "typ": "JWT"}
        payload = {
            "appKey": self._client_id,
            "iat": now,
            "exp": now + 86400,
            "tokenExp": now + 86400,
        }

        def b64url(data: bytes) -> str:
            return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

        h = b64url(_json.dumps(header, separators=(",", ":")).encode())
        p = b64url(_json.dumps(payload, separators=(",", ":")).encode())
        sig_input = f"{h}.{p}".encode()
        sig = hmac.new(
            self._client_secret.encode(), sig_input, hashlib.sha256
        ).digest()
        return f"{h}.{p}.{b64url(sig)}"

    # ------------------------------------------------------------------
    # SDK callbacks (called on GLib thread)
    # ------------------------------------------------------------------

    def _on_reminder_notify(self, content, handler) -> None:
        """Auto-accept meeting reminders (e.g. recording consent)."""
        if handler:
            handler.Accept()

    def _on_mixed_audio(self, raw_data) -> None:
        """Called with mixed audio from all participants."""
        try:
            buf = raw_data.GetBuffer()
            if buf and self._on_audio:
                self._on_audio(bytes(buf))
        except Exception:
            pass

    def _on_mic_initialize(self, sender) -> None:
        """Called when virtual mic is ready."""
        logger.info("Virtual mic initialized")
        self._audio_raw_data_sender = sender

    def _on_mic_start_send(self) -> None:
        logger.info("Virtual mic: start send")

    def _on_mic_stop_send(self) -> None:
        logger.info("Virtual mic: stop send")

    def _on_mic_uninit(self) -> None:
        logger.info("Virtual mic: uninitialized")
        self._audio_raw_data_sender = None
