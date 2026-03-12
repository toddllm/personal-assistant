"""Discord slash commands for controlling the voice bot."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from discord_bot.bot import DiscordVoiceBot

logger = logging.getLogger(__name__)


def register_commands(voice_bot: DiscordVoiceBot) -> None:
    """Register slash commands on the Discord bot."""
    bot = voice_bot.bot
    guild_id = voice_bot._settings.default_guild_id

    # If a default guild is set, commands are registered instantly for that guild.
    # Otherwise they register globally (can take up to 1hr to propagate).
    guild_ids = [guild_id] if guild_id else None

    @bot.slash_command(name="join", description="Join your voice channel", guild_ids=guild_ids)
    async def join(ctx: discord.ApplicationContext):
        if voice_bot.is_connected:
            await ctx.respond("Already in a voice channel. Use /leave first.", ephemeral=True)
            return

        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.respond("You need to be in a voice channel first.", ephemeral=True)
            return

        channel = ctx.author.voice.channel
        await ctx.defer()
        try:
            await voice_bot.join_channel(ctx.guild.id, channel.id)
            await ctx.followup.send(f"Joined **{channel.name}**!")
        except Exception as e:
            logger.exception("Failed to join voice channel")
            await ctx.followup.send(f"Failed to join: {e}")

    @bot.slash_command(name="leave", description="Leave the voice channel", guild_ids=guild_ids)
    async def leave(ctx: discord.ApplicationContext):
        if not voice_bot.is_connected:
            await ctx.respond("Not in a voice channel.", ephemeral=True)
            return

        await ctx.defer()
        try:
            await voice_bot.leave_channel()
            await ctx.followup.send("Left the voice channel.")
        except Exception as e:
            logger.exception("Failed to leave voice channel")
            await ctx.followup.send(f"Failed to leave: {e}")

    @bot.slash_command(name="status", description="Show bot status", guild_ids=guild_ids)
    async def status(ctx: discord.ApplicationContext):
        info = voice_bot.status_info
        lines = [f"**{k}:** {v}" for k, v in info.items()]
        await ctx.respond("\n".join(lines), ephemeral=True)

    @bot.slash_command(name="prompt", description="Change the system prompt", guild_ids=guild_ids)
    async def prompt(ctx: discord.ApplicationContext, text: str):
        voice_bot._settings.system_prompt = text
        await ctx.respond(f"System prompt updated to: {text[:100]}...", ephemeral=True)
