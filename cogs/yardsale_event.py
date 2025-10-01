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
        has = len(opts) > 0
        super().__init__(
            placeholder="Select at least one tag",
            min_values=1 if has else 0,
            max_values=min(5, len(opts)) or 1,
            options=opts or [discord.SelectOption(label="No tags configured")],
        )

    async def callback(self, interaction: discord.Interaction):
        view: ForumTagView = self.view  # type: ignore
        view.selected = list(self.values)
        await interaction.response.edit_message(
            content=f"Chosen tags: {', '.join(view.selected) if view.selected else 'none'}",
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
        self.add_item(self.confirm)
        self.add_item(self.cancel)

    async def _confirm(self, interaction: discord.Interaction):
        if not self.selected:
            await interaction.response.send_message("Pick at least one tag.", ephemeral=True)
            return
        self.stop()
        await interaction.response.edit_message(view=None)

    async def _cancel(self, interaction: discord.Interaction):
        self.selected = []
        self.stop()
        await interaction.response.edit_message(content="Canceled.", view=None)

# ---------- modal (max 5 inputs)

class YardSaleEventModal(discord.ui.Modal, title="Schedule Yard Sale Event"):
    def __init__(self, channel_id: int):
        super().__init__()
        self.channel_id = channel_id
        self.title_in    = discord.ui.TextInput(label="Title", max_length=120, required=True)
        self.start_dt_in = discord.ui.TextInput(label="Start (YYYY-MM-DD HH:MM)", required=True)
        self.end_dt_in   = discord.ui.TextInput(label="End (YYYY-MM-DD HH:MM)", required=True)
        self.loc_in      = discord.ui.TextInput(label="Location (optional)", required=False)
        self.desc_in     = discord.ui.TextInput(label="Details (optional)", style=discord.TextStyle.paragraph, required=False, max_length=1000)
        for x in (self.title_in, self.start_dt_in, self.end_dt_in, self.loc_in, self.desc_in):
            self.add_item(x)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        # parse start and end datetimes
        try:
            tz = ZoneInfo(TZ_NAME)
            start_full = datetime.strptime(self.start_dt_in.value.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=tz)
            end_full   = datetime.strptime(self.end_dt_in.value.strip(),   "%Y-%m-%d %H:%M").replace(tzinfo=tz)
            if end_full.date() < start_full.date():
                await interaction.followup.send("End date is before start date.", ephemeral=True)
                return
        except Exception as e:
            await interaction.followup.send(f"Invalid datetime format: {e}", ephemeral=True)
            return

        sh, smin = start_full.hour, start_full.minute
        eh, emin = end_full.hour,   end_full.minute

        # pick tags from forum channel
        # resolve the forum channel robustly
            chan = None
            if interaction.guild:  # prefer guild cache first
                chan = interaction.guild.get_channel(YARDSALE_CHANNEL_ID)

            if chan is None:
                try:
                    chan = await interaction.client.fetch_channel(YARDSALE_CHANNEL_ID)  # API fetch
                except discord.Forbidden:
                    await interaction.followup.send("Bot lacks permission to view that channel.", ephemeral=True)
                    return
                except discord.NotFound:
                    await interaction.followup.send("Channel ID not found. Check YARDSALE_CHANNEL_ID.", ephemeral=True)
                    return
                except Exception as e:
                    await interaction.followup.send(f"Fetch error: {e}", ephemeral=True)
                    return

if not isinstance(chan, discord.ForumChannel):
    await interaction.followup.send(f"Channel type is {getattr(chan, 'type', '?')}. Need a Forum channel.", ephemeral=True)
    return


        tag_view = ForumTagView(channel)
        await interaction.followup.send("Choose tags for this event:", view=tag_view, ephemeral=True)
        await tag_view.wait()
        chosen_names = tag_view.selected
        if not chosen_names:
            return  # canceled or none

        # create a Google Calendar event per day
        calendar_links: list[str] = []
        start_day = start_full.replace(hour=0, minute=0, second=0, microsecond=0)
        end_day   = end_full.replace(hour=0, minute=0, second=0, microsecond=0)

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
            await interaction.followup.send(f"Missing forum tags in this channel: {', '.join(missing)}", ephemeral=True)
        if not tags:
            await interaction.followup.send("No valid tags selected. Aborting post.", ephemeral=True)
            return

        # embed
        when_lines = []
        for day in _daterange(start_day, end_day):
            when_lines.append(day.strftime("%a %b %d") + f"  {start_full.strftime('%H:%M')}-{end_full.strftime('%H:%M')}")
        embed = discord.Embed(title=self.title_in.value, description=self.desc_in.value or "")
        embed.add_field(name="When", value="\n".join(when_lines), inline=False)
        if self.loc_in.value:
            embed.add_field(name="Where", value=self.loc_in.value, inline=False)
        embed.add_field(name="Tags", value=", ".join(chosen_names), inline=False)
        if calendar_links:
            shown = "\n".join(calendar_links[:3])
            more = f"\n(+{len(calendar_links)-3} more)" if len(calendar_links) > 3 else ""
            embed.add_field(name="Calendar", value=shown + more, inline=False)

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

    #@app_commands.checks.has_role("eventhost")
    @app_commands.command(name="event_yardsale_create", description="Create a yard sale event in the yard sale forum")
    async def event_yardsale_create(self, i: discord.Interaction):
        await i.response.send_modal(YardSaleEventModal(YARDSALE_CHANNEL_ID))

async def setup(bot: commands.Bot):
    await bot.add_cog(YardSaleEvents(bot))
