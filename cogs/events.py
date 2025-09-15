# cogs/events.py
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Any, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from shared import (
    TZ, TZ_NAME,
    EVENT_CHANNEL_ID, CREATE_FROM_CHANNEL_ID,
    INTEREST_TAGS, pick_color_id, norm_tag, display_dt,
    gcal_insert_event, gcal_list,
    db_exec, db_fetchone,
)
def _part_of_day_tag(dt) -> str:
    if 11 <= dt.hour < 14:
        return "mid-day"
    return "am" if dt.hour < 12 else "pm"


class RSVPView(discord.ui.View):
    def __init__(self, event_id: str):
        super().__init__(timeout=None)
        self.event_id = event_id

    async def _set_status(self, interaction: discord.Interaction, status: str):
        await db_exec(
            "INSERT OR REPLACE INTO rsvps(event_id, user_id, status) VALUES(?,?,?)",
            (self.event_id, interaction.user.id, status),
        )
        try:
            row = await db_fetchone(
                "SELECT thread_id FROM events_map WHERE event_id=?",
                (self.event_id,),
            )
            if row and row[0]:
                thread = interaction.client.get_channel(row[0]) or await interaction.client.fetch_channel(row[0])
                await thread.add_user(interaction.user)
        except Exception:
            pass
        await interaction.response.send_message(f"Your RSVP is **{status}**", ephemeral=True)

    @discord.ui.button(label="Going", style=discord.ButtonStyle.success, emoji="✅")
    async def going(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set_status(interaction, "going")

    @discord.ui.button(label="Maybe", style=discord.ButtonStyle.primary, emoji="❔")
    async def maybe(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set_status(interaction, "maybe")

    @discord.ui.button(label="Not Going", style=discord.ButtonStyle.danger, emoji="❌")
    async def notgoing(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set_status(interaction, "not going")


async def post_event_embed(channel: discord.abc.GuildChannel, event: dict) -> Tuple[discord.Message | None, discord.Thread | None]:
    start_iso = event["start"].get("dateTime") or event["start"].get("date")
    end_iso = event["end"].get("dateTime") or event["end"].get("date")

    title = event.get("summary", "Event")
    desc = event.get("description") or ""
    url = event.get("htmlLink")

    embed = discord.Embed(title=title, description=desc)
    if start_iso:
        embed.add_field(name="Starts", value=display_dt(start_iso), inline=True)
    if end_iso:
        embed.add_field(name="Ends", value=display_dt(end_iso), inline=True)
    if url:
        embed.add_field(name="Calendar", value=url, inline=False)

    view = RSVPView(event_id=event["id"])

    msg: discord.Message | None = None
    thread: discord.Thread | None = None

    if isinstance(channel, discord.TextChannel):
        msg = await channel.send(embed=embed, view=view)
        try:
            thread = await msg.create_thread(
                name=f"{title} chat",
                type=discord.ChannelType.private_thread,
                auto_archive_duration=10080,
            )
            if desc:
                await thread.send("Event details:\n" + desc)
        except Exception:
            pass
    elif isinstance(channel, discord.ForumChannel):
        thread = await channel.create_thread(
            name=title,
            content=desc or "Event discussion",
            embed=embed,
            view=view,
        )
        try:
            msg = await thread.send("Use the buttons above to RSVP.")
        except Exception:
            msg = None
    elif isinstance(channel, discord.Thread):
        msg = await channel.send(embed=embed, view=view)
        thread = channel
    else:
        raise TypeError(f"Unsupported channel type: {type(channel).__name__}")

    await db_exec(
        "INSERT OR REPLACE INTO events_map(discord_message_id, event_id, channel_id, thread_id) VALUES(?,?,?,?)",
        (msg.id if msg else None, event["id"], getattr(channel, 'id', None), thread.id if thread else None),
    )
    return msg, thread


class Events(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(description="Create a calendar event and post it")
    @app_commands.describe(
        title="Event title",
        date="Start date, YYYY-MM-DD (local)",
        start_time="Start time, HH:MM 24h (local)",
        duration_minutes="Duration in minutes",
        location="Where is it?",
        details="Description",
        tags="Comma-separated topic tags (e.g., Games, Fitness)",
    )
    async def event_create(
        self,
        interaction: discord.Interaction,
        title: str,
        date: str,
        start_time: str,
        duration_minutes: int = 60,
        location: str = "",
        details: str = "",
        tags: str = "",
    ):
        allowed_sources = {c for c in [EVENT_CHANNEL_ID, CREATE_FROM_CHANNEL_ID] if c}
        if allowed_sources and interaction.channel_id not in allowed_sources:
            return await interaction.response.send_message(
                "Use this in the events or create channel only.", ephemeral=True
            )

        target_channel = interaction.guild.get_channel(EVENT_CHANNEL_ID) if EVENT_CHANNEL_ID else interaction.channel
        if not target_channel:
            return await interaction.response.send_message(
                "I can't find the target events channel. Check EVENT_CHANNEL_ID.", ephemeral=True
            )

        try:
            start_local = datetime.strptime(f"{date} {start_time}", "%Y-%m-%d %H:%M")
            start_local = __import__("pytz").timezone(TZ_NAME).localize(start_local)
            end_local = start_local + timedelta(minutes=int(duration_minutes))
            pod_tag = _part_of_day_tag(start_local)
        except Exception as e:
            return await interaction.response.send_message(f"Time parse error: {e}", ephemeral=True)

        tag_list = [norm_tag(t) for t in tags.split(",") if t.strip()] if tags else []
        color_id = pick_color_id(tag_list)

        body = {
            "summary": title,
            "location": location,
            "description": details,
            "start": {"dateTime": start_local.isoformat()},
            "end": {"dateTime": end_local.isoformat()},
        }
        if color_id:
            body["colorId"] = color_id

        await interaction.response.send_message("Creating event...", ephemeral=True)
        try:
            event = await gcal_insert_event(body)
            for t in tag_list:
                await db_exec("INSERT OR REPLACE INTO event_tags(event_id, tag) VALUES(?,?)", (event["id"], t))

            msg, thread = await post_event_embed(target_channel, event)
            await interaction.followup.send(f"Created **{title}**", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Calendar error: {e}", ephemeral=True)

    @app_commands.command(description="List upcoming events")
    @app_commands.describe(days="How many days ahead to list")
    async def event_list(self, interaction: discord.Interaction, days: int = 14):
        now = datetime.now(TZ)
        time_min = now.isoformat()
        time_max = (now + timedelta(days=days)).isoformat()
        try:
            resp = await gcal_list(time_min, time_max, max_items=25)
            items = resp.get("items", [])
            if not items:
                return await interaction.response.send_message("No upcoming events.", ephemeral=True)
            lines = []
            for ev in items:
                start_iso = ev["start"].get("dateTime") or ev["start"].get("date")
                when = display_dt(start_iso) if start_iso else ""
                url = ev.get("htmlLink")
                lines.append(f"• **{ev.get('summary','Event')}** — {when}  <{url}>")
            await interaction.response.send_message("\n".join(lines[:15]), ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Calendar error: {e}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Events(bot))
