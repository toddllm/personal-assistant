"""Discord voice bot — manages voice connections and AI pipeline lifecycle."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import discord

from discord_bot.audio_sink import PipelineSink
from discord_bot.audio_source import TTSAudioSource, resample_32k_to_48k
from zoom_bot.audio_pipeline import AudioPipeline

if TYPE_CHECKING:
    from discord_bot.config import Settings
    from zoom_bot.llm_providers import LLMProvider
    from zoom_bot.stt_providers import STTProvider
    from zoom_bot.tts_providers import TTSProvider

logger = logging.getLogger(__name__)


class DiscordVoiceBot:
    """Manages a Discord bot with voice AI capabilities.

    Handles joining/leaving voice channels, wiring up the AudioPipeline
    with provider-pluggable STT/LLM/TTS, and audio I/O.
    """

    def __init__(
        self,
        settings: Settings,
        stt: STTProvider,
        llm: LLMProvider,
        tts: TTSProvider,
    ):
        self._settings = settings
        self._stt = stt
        self._llm = llm
        self._tts = tts

        intents = discord.Intents.default()
        intents.voice_states = True

        self.bot = discord.Bot(intents=intents)
        self._voice_client: discord.VoiceClient | None = None
        self._pipeline: AudioPipeline | None = None
        self._tts_source: TTSAudioSource | None = None
        self._sink: PipelineSink | None = None
        self._join_time: float | None = None
        self._guild_id: int | None = None
        self._channel_id: int | None = None

    @property
    def pipeline(self):
        """Return the active AudioPipeline, or None."""
        return self._pipeline

    @property
    def is_connected(self) -> bool:
        return self._voice_client is not None and self._voice_client.is_connected()

    @property
    def status_info(self) -> dict:
        info: dict = {
            "connected": self.is_connected,
            "guild_id": self._guild_id,
            "channel_id": self._channel_id,
            "stt_provider": self._settings.stt_provider,
            "llm_provider": self._settings.llm_provider,
            "tts_provider": self._settings.tts_provider,
        }
        if self._join_time and self.is_connected:
            info["uptime_seconds"] = round(time.time() - self._join_time, 1)
        if self._sink:
            info["active_speakers"] = len(self._sink.active_speakers)
        return info

    async def join_channel(self, guild_id: int, channel_id: int) -> None:
        """Join a Discord voice channel and start the AI pipeline."""
        if self.is_connected:
            raise RuntimeError("Already connected to a voice channel")

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            raise ValueError(f"Guild {guild_id} not found")

        channel = guild.get_channel(channel_id)
        if channel is None:
            raise ValueError(f"Channel {channel_id} not found in guild {guild_id}")
        if not isinstance(channel, discord.VoiceChannel):
            raise ValueError(f"Channel {channel_id} is not a voice channel")

        # Clear any stale voice state
        if guild.voice_client is not None:
            logger.info("Clearing stale voice connection in guild %d", guild_id)
            try:
                await guild.voice_client.disconnect(force=True)
            except Exception:
                pass
            await asyncio.sleep(2)

        # Connect to voice channel
        logger.info("Connecting to voice channel %d...", channel_id)
        self._voice_client = await channel.connect(timeout=60.0)
        self._guild_id = guild_id
        self._channel_id = channel_id
        self._join_time = time.time()

        # Wait for voice WebSocket to fully connect
        connected = False
        for i in range(60):
            if self._voice_client.is_connected():
                connected = True
                logger.info("Voice connected after %.1fs", i * 0.5)
                break
            await asyncio.sleep(0.5)

        if not connected:
            logger.error("Voice WebSocket did not connect in 30s")
            try:
                await self._voice_client.disconnect(force=True)
            except Exception:
                pass
            self._voice_client = None
            self._guild_id = None
            self._channel_id = None
            raise ConnectionError("Voice WebSocket failed to connect")

        # Create TTS audio source
        self._tts_source = TTSAudioSource()

        # Create and start pipeline
        self._pipeline = AudioPipeline(
            stt=self._stt,
            llm=self._llm,
            tts=self._tts,
            system_prompt=self._settings.system_prompt,
            on_tts_audio=self._on_tts_audio,
            debounce_seconds=self._settings.debounce_seconds,
            max_conversation_turns=self._settings.max_conversation_turns,
            llm_max_tokens=self._settings.llm_max_tokens,
            llm_temperature=self._settings.llm_temperature,
        )
        await self._pipeline.start()

        # Start recording audio from the voice channel
        self._sink = PipelineSink(self._pipeline, self.bot.user.id)
        self._voice_client.start_recording(self._sink, self._on_recording_done)

        logger.info("Joined voice channel %d in guild %d", channel_id, guild_id)

        # Send a greeting
        await asyncio.sleep(1)
        await self._pipeline.send_tts("Hello! I've joined the voice channel.")

    async def leave_channel(self) -> None:
        """Leave the current voice channel and stop the pipeline."""
        if self._voice_client and self._voice_client.is_connected():
            self._voice_client.stop_recording()
            if self._voice_client.is_playing():
                self._voice_client.stop()
            await self._voice_client.disconnect()

        if self._pipeline:
            await self._pipeline.stop()
            self._pipeline = None

        if self._tts_source:
            self._tts_source.cleanup()
            self._tts_source = None

        if self._sink:
            self._sink.cleanup()
            self._sink = None

        self._voice_client = None
        self._guild_id = None
        self._channel_id = None
        self._join_time = None

        logger.info("Left voice channel")

    def _on_tts_audio(self, pcm_32k: bytes) -> None:
        """Callback from pipeline — resample and push to Discord playback."""
        if not self._tts_source or not self._voice_client:
            return
        pcm_48k = resample_32k_to_48k(pcm_32k)
        self._tts_source.push_audio(pcm_48k)
        self._try_play()

    def _try_play(self) -> None:
        """Start playback if not already playing."""
        if not self._voice_client or not self._tts_source:
            return
        try:
            if not self._voice_client.is_playing():
                self._voice_client.play(self._tts_source, after=self._on_playback_done)
        except Exception as e:
            logger.debug("play() failed: %s", e)

    def _on_playback_done(self, error: Exception | None) -> None:
        """Called when playback finishes. Re-check for buffered audio."""
        if error:
            logger.error("Playback error: %s", error)
        # If more audio arrived while playing, restart
        if self._tts_source and self._tts_source.has_audio:
            self._try_play()

    async def _on_recording_done(self, sink: discord.sinks.Sink, *args) -> None:
        """Called when recording stops (e.g., on disconnect)."""
        logger.info("Recording stopped")

    async def start(self) -> None:
        """Start the Discord bot (blocking)."""
        if not self._settings.bot_token:
            raise ValueError("DISCORD_BOT_BOT_TOKEN is required")
        await self.bot.start(self._settings.bot_token)

    async def close(self) -> None:
        """Clean up and close the bot."""
        if self.is_connected:
            await self.leave_channel()
        await self.bot.close()
