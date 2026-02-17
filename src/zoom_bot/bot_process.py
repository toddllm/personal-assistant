"""Zoom SDK bot process — runs as a standalone subprocess.

The Zoom Meeting SDK requires exclusive use of the main thread with
a GLib event loop.  This module runs as a separate process from the
FastAPI service, communicating via a JSON state file and a FIFO pipe
for commands.

Usage (internal — called by main.py):
    python -m zoom_bot.bot_process --meeting-id 89105007950 --passcode 857908

State file: /tmp/zoom-bot-state.json
Command FIFO: /tmp/zoom-bot-cmd
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import struct
import signal
import sys
import time

logger = logging.getLogger(__name__)

STATE_FILE = "/tmp/zoom-bot-state.json"
CMD_FIFO = "/tmp/zoom-bot-cmd"


def _write_state(state: str, meeting_id: str | None = None, error: str | None = None):
    """Write current state to the state file (atomic)."""
    data = {
        "state": state,
        "meeting_id": meeting_id,
        "pid": os.getpid(),
        "timestamp": time.time(),
        "error": error,
    }
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, STATE_FILE)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser()
    parser.add_argument("--meeting-id", required=True)
    parser.add_argument("--passcode", default="")
    parser.add_argument("--max-duration", type=int, default=180)
    parser.add_argument("--prompt", default="")
    args = parser.parse_args()

    # Load env
    for env_path in ("/opt/zoom-bot/.env", "/opt/zoom-bot/zoom_bot/.env"):
        if os.path.isfile(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key, value = key.strip(), value.strip().strip("'\"")
                    if key and key not in os.environ:
                        os.environ[key] = value

    import zoom_meeting_sdk as sdk
    import gi
    gi.require_version("GLib", "2.0")
    from gi.repository import GLib

    _write_state("joining", args.meeting_id)

    # ---- SDK Init ----
    logger.info("Initializing Zoom SDK...")
    init_param = sdk.InitParam()
    init_param.strWebDomain = "https://zoom.us"
    init_param.strSupportUrl = "https://zoom.us"
    init_param.enableGenerateDump = False
    init_param.emLanguageID = sdk.SDK_LANGUAGE_ID.LANGUAGE_English
    init_param.enableLogByDefault = True

    err = sdk.InitSDK(init_param)
    if err != sdk.SDKERR_SUCCESS:
        _write_state("error", args.meeting_id, f"InitSDK failed: {err}")
        sys.exit(1)

    # ---- State ----
    meeting_service = None
    auth_service = None
    setting_service = None
    audio_helper = None
    audio_raw_data_sender = None
    recording_ctrl = None
    audio_ctrl = None
    start_time = None
    glib_loop = GLib.MainLoop()

    # Keep callback objects alive
    callbacks = {}

    def generate_jwt():
        """Generate a Meeting SDK JWT.

        Matches the exact format from py-zoom-meeting-sdk sample_program/meeting_bot.py.
        """
        import jwt as pyjwt
        from datetime import datetime, timedelta

        client_id = os.environ["ZOOM_APP_CLIENT_ID"]
        client_secret = os.environ["ZOOM_APP_CLIENT_SECRET"]
        iat = datetime.utcnow()
        exp = iat + timedelta(hours=24)

        payload = {
            "iat": iat,
            "exp": exp,
            "appKey": client_id,
            "tokenExp": int(exp.timestamp()),
        }
        token = pyjwt.encode(payload, client_secret, algorithm="HS256")
        logger.info("JWT generated (appKey=%s...)", client_id[:8])
        return token

    # ---- Audio pipeline (Deepgram STT -> Groq -> ElevenLabs TTS) ----
    from zoom_bot.audio_pipeline import AudioPipeline
    import asyncio
    import threading

    pipeline = None
    pipeline_loop = None

    def start_pipeline():
        nonlocal pipeline, pipeline_loop

        pipeline = AudioPipeline(
            deepgram_api_key=os.environ.get("DEEPGRAM_API_KEY", ""),
            groq_api_key=os.environ.get("GROQ_API_KEY", ""),
            elevenlabs_api_key=os.environ.get("ELEVENLABS_API_KEY", ""),
            system_prompt=args.prompt or (
                "You are a friendly, helpful AI assistant who has joined a Zoom meeting. "
                "Have a natural conversation. Be concise — keep responses to 1-2 sentences. "
                "Listen carefully and respond naturally. "
                "If asked who you are, say you're an AI assistant here to help."
            ),
            on_tts_audio=on_tts_audio,
        )

        def run_pipeline():
            nonlocal pipeline_loop
            pipeline_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(pipeline_loop)
            pipeline_loop.run_until_complete(pipeline.start())
            pipeline_loop.run_forever()

        t = threading.Thread(target=run_pipeline, daemon=True)
        t.start()
        logger.info("Audio pipeline started on background thread")

    # PulseAudio pipe for TTS audio output (fallback if SDK send() fails)
    PA_PIPE = "/tmp/tts_audio_pipe"
    pa_pipe_fd = [None]

    def _get_pipe_fd():
        """Get or open the pipe file descriptor (kept open for reuse)."""
        if pa_pipe_fd[0] is None:
            try:
                pa_pipe_fd[0] = open(PA_PIPE, 'wb', buffering=0)
                logger.info("Opened PulseAudio pipe for writing")
            except Exception:
                logger.debug("PulseAudio pipe not available (not critical)")
        return pa_pipe_fd[0]

    def _sdk_send_audio(pcm_data: bytes):
        """Send audio via SDK virtual mic sender (3-arg signature)."""
        if audio_raw_data_sender is None:
            return False
        try:
            audio_raw_data_sender.send(
                pcm_data,
                32000,
                sdk.ZoomSDKAudioChannel_Mono,
            )
            return True
        except Exception:
            logger.exception("SDK send() failed")
            return False

    def on_tts_audio(pcm_data: bytes):
        """Send TTS audio — try SDK sender first, fall back to PA pipe."""
        logger.info("Sending %d bytes TTS audio", len(pcm_data))

        # Primary: SDK virtual mic sender (3-arg call)
        if _sdk_send_audio(pcm_data):
            logger.info("TTS audio sent via SDK sender")
            return

        # Fallback: PulseAudio pipe
        logger.info("SDK sender unavailable, writing to PulseAudio pipe")
        try:
            fd = _get_pipe_fd()
            if fd:
                fd.write(pcm_data)
                fd.flush()
        except BrokenPipeError:
            logger.warning("Pipe broken, reopening...")
            pa_pipe_fd[0] = None
        except Exception:
            logger.exception("Failed to write to PulseAudio pipe")

    def on_mixed_audio(raw_data):
        """Feed meeting audio into the pipeline."""
        if pipeline:
            try:
                buf = raw_data.GetBuffer()
                if buf:
                    pipeline.feed_audio(bytes(buf))
            except Exception:
                pass

    # ---- SDK Callbacks ----

    def on_auth_return(result):
        nonlocal meeting_service
        logger.info("Auth result: %s", result)
        if result != sdk.AUTHRET_SUCCESS:
            _write_state("error", args.meeting_id, f"Auth failed: {result}")
            glib_loop.quit()
            return

        logger.info("Auth successful, joining meeting %s...", args.meeting_id)

        join_param = sdk.JoinParam()
        join_param.userType = sdk.SDKUserType.SDK_UT_WITHOUT_LOGIN

        param = join_param.param
        param.meetingNumber = int(args.meeting_id)
        param.userName = "AI Assistant"
        param.psw = args.passcode
        param.isVideoOff = True
        param.isAudioOff = False
        param.isAudioRawDataStereo = False
        param.isMyVoiceInMix = False
        param.eAudioRawdataSamplingRate = sdk.AudioRawdataSamplingRate.AudioRawdataSamplingRate_32K

        err = meeting_service.Join(join_param)
        if err != sdk.SDKERR_SUCCESS:
            _write_state("error", args.meeting_id, f"Join failed: {err}")
            glib_loop.quit()
            return

        audio_settings = setting_service.GetAudioSettings()
        if audio_settings:
            audio_settings.EnableAutoJoinAudio(True)

    def on_reminder_notify(content, handler):
        if handler:
            handler.Accept()

    def on_mic_initialize(sender):
        """Called when SDK virtual mic is ready — store the sender."""
        nonlocal audio_raw_data_sender
        logger.info("Virtual mic initialized — sender acquired")
        audio_raw_data_sender = sender

    def on_mic_start_send():
        """Called when SDK is ready to receive audio via send()."""
        logger.info("Virtual mic: start send — sending 3s test tone via SDK")
        sample_rate = 32000
        n = sample_rate * 3  # 3 seconds
        tone = struct.pack(f"<{n}h", *[
            int(16000 * math.sin(2 * math.pi * 440 * i / sample_rate))
            for i in range(n)
        ])
        if _sdk_send_audio(tone):
            logger.info("Test tone sent via SDK sender (%d bytes)", len(tone))
        else:
            logger.warning("SDK sender not ready in on_mic_start_send, trying PA pipe")
            on_tts_audio(tone)

    def on_mic_stop_send():
        logger.info("Virtual mic: stop send")

    def on_mic_uninit():
        nonlocal audio_raw_data_sender
        logger.info("Virtual mic: uninitialized")
        audio_raw_data_sender = None

    def setup_virtual_mic():
        """Set up SDK virtual mic BEFORE starting raw recording."""
        nonlocal audio_helper

        audio_helper = sdk.GetAudioRawdataHelper()
        if audio_helper is None:
            logger.warning("GetAudioRawdataHelper returned None")
            return

        # Register virtual mic callbacks and set external audio source
        callbacks["virtual_mic_event"] = sdk.ZoomSDKVirtualAudioMicEventCallbacks(
            onMicInitializeCallback=on_mic_initialize,
            onMicStartSendCallback=on_mic_start_send,
            onMicStopSendCallback=on_mic_stop_send,
            onMicUninitializedCallback=on_mic_uninit,
        )
        err = audio_helper.setExternalAudioSource(callbacks["virtual_mic_event"])
        logger.info("setExternalAudioSource result: %s", err)

    def start_raw_recording():
        """Start raw recording + subscribe to incoming audio."""
        nonlocal recording_ctrl

        recording_ctrl = meeting_service.GetMeetingRecordingController()

        def on_recording_privilege_changed(can_rec):
            logger.info("Recording privilege changed: %s", can_rec)
            if can_rec:
                GLib.timeout_add_seconds(1, start_raw_recording)

        callbacks["recording_event"] = sdk.MeetingRecordingCtrlEventCallbacks(
            onRecordPrivilegeChangedCallback=on_recording_privilege_changed,
        )
        recording_ctrl.SetEvent(callbacks["recording_event"])

        can_start = recording_ctrl.CanStartRawRecording()
        if can_start != sdk.SDKERR_SUCCESS:
            logger.info("Requesting recording privilege...")
            recording_ctrl.RequestLocalRecordingPrivilege()
            return False

        err = recording_ctrl.StartRawRecording()
        if err != sdk.SDKERR_SUCCESS:
            logger.warning("StartRawRecording failed: %s", err)
            return False

        logger.info("Raw recording started")

        # Subscribe to incoming audio (for STT) using the same audio_helper
        if audio_helper is not None:
            callbacks["audio_delegate"] = sdk.ZoomSDKAudioRawDataDelegateCallbacks(
                onMixedAudioRawDataReceivedCallback=on_mixed_audio,
            )
            err = audio_helper.subscribe(callbacks["audio_delegate"], False)
            logger.info("Audio subscribe result: %s", err)
        else:
            logger.warning("audio_helper is None — cannot subscribe to audio")

        return False

    def on_meeting_status_changed(status, iResult=0):
        nonlocal start_time, audio_ctrl
        logger.info("Meeting status: %s (result=%s)", status, iResult)

        if status == sdk.MEETING_STATUS_INMEETING:
            start_time = time.time()
            _write_state("in_meeting", args.meeting_id)

            # Accept reminders
            callbacks["reminder_event"] = sdk.MeetingReminderEventCallbacks(
                onReminderNotifyCallback=on_reminder_notify,
            )
            reminder_ctrl = meeting_service.GetMeetingReminderController()
            reminder_ctrl.SetEvent(callbacks["reminder_event"])

            # Audio controller + event callbacks
            audio_ctrl = meeting_service.GetMeetingAudioController()

            def on_host_request_start_audio(handler):
                logger.info("Host requested start audio — accepting")
                handler.Accept()

            callbacks["audio_ctrl_event"] = sdk.MeetingAudioCtrlEventCallbacks(
                onHostRequestStartAudioCallback=on_host_request_start_audio,
            )
            audio_ctrl.SetEvent(callbacks["audio_ctrl_event"])

            # Join VoIP
            audio_ctrl.JoinVoip()

            audio_settings = setting_service.GetAudioSettings()
            if audio_settings:
                audio_settings.EnableAutoJoinAudio(True)
                # Disable noise suppression to let synthesized audio through
                try:
                    audio_settings.DisableEchoCancellation(True)
                except Exception:
                    pass
                try:
                    audio_settings.SetSuppressBackgroundNoiseLevel(
                        sdk.Suppress_BGNoise_Level.Suppress_BGNoise_Level_None
                    )
                except Exception:
                    pass
                try:
                    audio_settings.EnableMicOriginalInput(True)
                except Exception:
                    pass
                try:
                    audio_settings.EnableAutoAdjustMicVolume(False)
                except Exception:
                    pass
                logger.info("Audio settings: noise suppression disabled, original input enabled")

            # Periodically unmute self
            unmute_counter = [0]

            def _periodic_unmute():
                unmute_counter[0] += 1
                if unmute_counter[0] % 50 == 1:  # Every ~50 seconds
                    participants_ctrl = meeting_service.GetMeetingParticipantsController()
                    me = participants_ctrl.GetMySelfUser()
                    if me:
                        my_id = me.GetUserID()
                        is_muted = me.IsAudioMuted()
                        if unmute_counter[0] == 1:
                            logger.info("Bot user ID: %s, audio muted: %s", my_id, is_muted)
                        if is_muted:
                            can_unmute = audio_ctrl.CanUnMuteBySelf()
                            logger.info("Attempting unmute (canUnmuteBySelf=%s)", can_unmute)
                            result = audio_ctrl.UnMuteAudio(my_id)
                            logger.info("UnMuteAudio result: %s", result)
                return True

            GLib.timeout_add_seconds(1, _periodic_unmute)

            # Step 4: Set up SDK virtual mic (BEFORE raw recording)
            setup_virtual_mic()

            # Step 5: Start audio pipeline
            start_pipeline()

            # Step 6: Start raw recording (for incoming audio) after delay
            GLib.timeout_add_seconds(2, start_raw_recording)

        elif status == sdk.MEETING_STATUS_FAILED:
            _write_state("error", args.meeting_id, f"Meeting failed: {iResult}")
            glib_loop.quit()

        elif status == sdk.MEETING_STATUS_ENDED:
            _write_state("idle")
            glib_loop.quit()

    # ---- Create services ----
    meeting_service = sdk.CreateMeetingService()
    setting_service = sdk.CreateSettingService()
    auth_service = sdk.CreateAuthService()

    callbacks["meeting_event"] = sdk.MeetingServiceEventCallbacks(
        onMeetingStatusChangedCallback=on_meeting_status_changed,
    )
    meeting_service.SetEvent(callbacks["meeting_event"])

    callbacks["auth_event"] = sdk.AuthServiceEventCallbacks(
        onAuthenticationReturnCallback=on_auth_return,
    )
    auth_service.SetEvent(callbacks["auth_event"])

    auth_ctx = sdk.AuthContext()
    auth_ctx.jwt_token = generate_jwt()

    err = auth_service.SDKAuth(auth_ctx)
    if err != sdk.SDKERR_SUCCESS:
        _write_state("error", args.meeting_id, f"SDKAuth failed: {err}")
        sys.exit(1)

    logger.info("SDK auth request submitted")

    # ---- Auto-leave timer ----
    def check_duration():
        if start_time and (time.time() - start_time) > args.max_duration:
            logger.info("Max duration %ds reached, leaving", args.max_duration)
            do_leave()
            return False
        return True

    GLib.timeout_add_seconds(5, check_duration)

    # ---- Leave handler ----
    def do_leave():
        _write_state("leaving", args.meeting_id)

        if pipeline and pipeline_loop:
            pipeline_loop.call_soon_threadsafe(pipeline_loop.stop)

        if meeting_service:
            try:
                status = meeting_service.GetMeetingStatus()
                if status != sdk.MEETING_STATUS_IDLE:
                    meeting_service.Leave(sdk.LEAVE_MEETING)
            except Exception:
                pass

        GLib.timeout_add_seconds(2, _cleanup_and_quit)

    def _cleanup_and_quit():
        if audio_helper:
            try:
                audio_helper.unSubscribe()
            except Exception:
                pass
        try:
            sdk.DestroyMeetingService(meeting_service)
        except Exception:
            pass
        try:
            sdk.DestroySettingService(setting_service)
        except Exception:
            pass
        try:
            sdk.DestroyAuthService(auth_service)
        except Exception:
            pass
        try:
            sdk.CleanUPSDK()
        except Exception:
            pass

        _write_state("idle")
        glib_loop.quit()
        return False

    # ---- Signal handling ----
    def on_signal(signum, frame):
        logger.info("Received signal %s", signum)
        GLib.timeout_add(100, do_leave)

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    # ---- Run main loop ----
    logger.info("Starting GLib main loop")
    try:
        glib_loop.run()
    except KeyboardInterrupt:
        do_leave()

    logger.info("Bot process exiting")


if __name__ == "__main__":
    main()
