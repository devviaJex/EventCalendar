# cogs/yardsale_event.py

from __future__ import annotations
from typing import List
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

from shared import (
    YARDSALE_CHANNEL_ID,   # int, set in .env and loaded in shared.py
    TZ_NAME,
    gcal_insert_event,
)

# ---------- helpers

def _parse_date(s: str) -> tuple[int, int, int]:
    y, m, d = map(int, s.strip().split("-"))
    return y, m, d

def _parse_time(s: str) -> tuple[int, int]:
    hh, mm = map(int, re.split("[: ]", s.strip())[:2])
    return hh, mm

def _daterange(d0: datetime, d1: datetime):
    cur = d0
    while cur.date() <= d1.date():
        yield cur
        cur += timedelta(days=1)

def _resolve_forum_tags(channel: discord.ForumChannel, names: list[str]) -> tuple[list[discord.ForumTag], list[str]]:
    by_name = {t.name.strip().lower(): t for t in channel.available_tags}
    found: list[discord.ForumTag] = []
    missing: list[str] = []
    for n in names:
        key = n.strip().lower()
        t = by_name.get(key)
        if t and t not in found:
            found.append(t)
        elif not t:
            missing.append(n)
    return found, missing

# ---------- tag picker using existing forum tags

class ForumTagSelect(discord.ui.Select):
    def __init__(self, channel: discord.ForumChannel):
        opts = [discord.SelectOption(label=t.name[:100]) for t in channel.available_tags[:25]]
        super().__init__(
            placeholder="Select at least one tag",
            min_values=1,
            max_values=min(5, len(opts)) or 1,
            options=opts or [discord.SelectOption(label="No tags configured")],
        )

    async def callback(self, interaction: discord.Interaction):
        view: ForumTagView = self.view  # type: ignore
        view.selected = list(self.values)
        await interaction.response.edit_message(
            content=f"Chosen tags: {', '.join(view.selected)}",
            view=view,
        )

class ForumTagView(discord.ui.View):
    def __init__(self, channel: discord.ForumChannel):
        super().__init__(timeout=300)
        self.selected: List[str] = []
        self.add_item(ForumTagSelect(channel))
        self.confirm = discord.ui.Button(style=discord.ButtonStyle.primary, label="Confirm")
        self.cancel = discord.ui.Button(style=discord.ButtonStyle.secondary, label="Cancel")
        self.confirm.callback = self._confirm  # type: ignore
        self.cancel.callback = self._cancel   # type: ignore
        self.add_item(self.confirm); self.add_item(self.cancel)

    async def _confirm(self, interaction: discord.Interaction):
        if not self.selected:
            await interaction.response.send_message("Pick at least one tag.", ephemeral=True)
            return
        self.stop()
        await interaction.edit_original_response(view=None)

    async def _cancel(self, interaction: discord.Interaction):
        self.selected = []
        self.stop()
        await interaction.edit_original_response(content="Canceled.", view=None)

# ---------- modal

class YardSaleEventModal(discord.ui.Modal, title="Schedule Yard Sale Event"):
    def __init__(self, channel_id: int):
        super().__init__()
        self.channel_id = channel_id
        self.title_in = discord.ui.TextInput(label="Title", max_length=120, required=True)
        self.start_date_in = discord.ui.TextInput(label="Start Date (YYYY-MM-DD)", required=True)
        self.end_date_in   = discord.ui.TextInput(label="End Date (YYYY-MM-DD)", required=True)
        self.start_time_in = discord.ui.TextInput(label="Start Time (HH:MM 24h)", required=True)
        self.end_time_in   = discord.ui.TextInput(label="End Time (HH:MM 24h)", required=True)
        self.loc_in  = discord.ui.TextInput(label="Location", required=False)
        self.desc_in = discord.ui.TextInput(label="Details", style=discord.TextStyle.paragraph, required=False, max_length=1000)
        for x in (self.title_in, self.start_date_in, self.end_date_in, self.start_time_in, self.end_time_in, self.loc_in, self.desc_in):
            self.add_item(x)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        # parse inputs
        try:
            sy, sm, sd = _parse_date(self.start_date_in.value)
            ey, em, ed = _parse_date(self.end_date_in.value)
            sh, smin = _parse_time(self.start_time_in.value)
            eh, emin = _parse_time(self.end_time_in.value)
        except Exception as e:
            await interaction.followup.send(f"Invalid date/time: {e}", ephemeral=True)
            return

        tz = ZoneInfo(TZ_NAME)
        start_day = datetime(sy, sm, sd, tzinfo=tz)
        end_day   = datetime(ey, em, ed, tzinfo=tz)
        if end_day < start_day:
            await interaction.followup.send("End date is before start date.", ephemeral=True)
            return

        # pick tags from target forum channel
        channel = interaction.client.get_channel(self.channel_id)
        if not isinstance(channel, discord.ForumChannel):
            await interaction.followup.send("Target channel is not a Forum channel.", ephemeral=True)
            return

        tag_view = ForumTagView(channel)
        await interaction.followup.send("Choose tags for this event:", view=tag_view, ephemeral=True)
        await tag_view.wait()
        chosen_names = tag_view.selected
        if not chosen_names:
            return  # canceled or no selection

        # create per-day calendar events
        calendar_links: list[str] = []
        for day in _daterange(start_day, end_day):
            start_dt = day.replace(hour=sh, minute=smin)
            end_dt   = day.replace(hour=eh, minute=emin)
            if end_dt <= start_dt:
                end_dt += timedelta(days=1)  # overnight window

            try:
                ev = await gcal_insert_event(
                    self.title_in.value,
                    start_dt, end_dt,
                    self.loc_in.value or None,
                    self.desc_in.value or None,
                )
                link = ev.get("htmlLink") if ev else None
                if link:
                    calendar_links.append(link)
            except Exception as e:
                await interaction.followup.send(f"Calendar error on {day.date()}: {e}", ephemeral=True)

        # resolve forum tags
        tags, missing = _resolve_forum_tags(channel, chosen_names)
        if missing:
            await interaction.followup.send(
                f"Missing forum tags in this channel: {', '.join(missing)}",
                ephemeral=True,
            )
        if not tags:
            await interaction.followup.send("No valid tags selected. Aborting post.", ephemeral=True)
            return

        # embed
        when_lines = []
        for day in _daterange(start_day, end_day):
            when_lines.append(day.strftime("%a %b %d") + f"  {self.start_time_in.value}–{self.end_time_in.value}")
        embed = discord.Embed(title=self.title_in.value, description=self.desc_in.value or "")
        embed.add_field(name="When", value="\n".join(when_lines), inline=False)
        if self.loc_in.value:
            embed.add_field(name="Where", value=self.loc_in.value, inline=False)
        embed.add_field(name="Tags", value=", ".join(chosen_names), inline=False)

        if calendar_links:
            shown = "\n".join(calendar_links[:3])
            more = f"\n(+{len(calendar_links)-3} more)" if len(calendar_links) > 3 else ""
            embed.add_field(name="Calendar", value=shown + more, inline=False)

        # post thread
        await channel.create_thread(
            name=self.title_in.value,
            content=None,
            embed=embed,
            applied_tags=tags[:5],
        )

        await interaction.followup.send("Yard sale event posted.", ephemeral=True)

# ---------- cog

class YardSaleEvents(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.checks.has_role("eventhost")
    @app_commands.command(name="event_yardsale_create", description="Create a yard sale event in the yard sale forum")
    async def event_yardsale_create(self, i: discord.Interaction):
        await i.response.send_modal(YardSaleEventModal(YARDSALE_CHANNEL_ID))

async def setup(bot: commands.Bot):
    await bot.add_cog(YardSaleEvents(bot))

