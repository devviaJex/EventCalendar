# cogs/dynamic_events.py

from __future__ import annotations
from dataclasses import dataclass
from typing import List, Callable, Optional
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

from shared import TZ_NAME, gcal_insert_event
# import your channel IDs from shared.py
from shared import YARDSALE_CHANNEL_ID, FOODEATS_CHANNEL_ID

# ---------- config

@dataclass(frozen=True)
class EventChannelConfig:
    key: str
    channel_id: int
    command_name: str
    command_description: str
    modal_title: str
    title_label: str = "Title"
    start_label: str = "Start (MM/DD/YYYY h:mm am/pm)"
    end_label: str = "End (MM/DD/YYYY h:mm am/pm)"
    location_label: str = "Location (optional)"
    details_label: str = "Details (optional)"
    tag_prompt: str = "Choose up to 4 tags for this event:"
    require_role: Optional[str] = None  # e.g. "eventhost"

CONFIGS: list[EventChannelConfig] = [
    EventChannelConfig(
        key="yardsale",
        channel_id=YARDSALE_CHANNEL_ID,
        command_name="new_yea_event",
        command_description="Create an event in the Yard/Estate/Auction forum",
        modal_title="Schedule Yard/Estate/Auction sale",
        tag_prompt="Choose up to 5 tags for this sale:",
        require_role=None,  # set to "eventhost" if needed
    ),
     EventChannelConfig(
        key="fed",
        channel_id=FOODEATS_CHANNEL_ID,
        command_name="new_fed_event",
        command_description="Create an event in the Farmers/Eats/Drinks forum",
        modal_title="Schedule Farmers/Eats/Drink sale",
        tag_prompt="Choose up to 5 tags for this sale:",
        require_role=None,  # set to "eventhost" if needed
    ),
    # Add more channels here:
    # EventChannelConfig(
    #     key="meetup",
    #     channel_id=MEETUP_CHANNEL_ID,
    #     command_name="event_meetup_create",
    #     command_description="Create a meetup event",
    #     modal_title="Schedule Meetup",
    #     require_role="eventhost",
    # ),
]

# ---------- helpers

def _parse_mdy12(s: str) -> datetime:
    s = re.sub(r"\s*(am|pm)\s*$", lambda m: " " + m.group(1).upper(), s.strip(), flags=re.I)
    return datetime.strptime(s, "%m/%d/%Y %I:%M %p")

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

# ---------- tag picker

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
    def __init__(self, channel: discord.ForumChannel, confirm_label: str = "Confirm"):
        super().__init__(timeout=300)
        self.selected: List[str] = []
        self.add_item(ForumTagSelect(channel))
        self.confirm = discord.ui.Button(style=discord.ButtonStyle.primary, label=confirm_label)
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

# ---------- dynamic modal (max 5 inputs)

class DynamicEventModal(discord.ui.Modal):
    def __init__(self, cfg: EventChannelConfig):
        super().__init__(title=cfg.modal_title)
        self.cfg = cfg
        self.title_in    = discord.ui.TextInput(label=cfg.title_label, max_length=120, required=True)
        self.start_dt_in = discord.ui.TextInput(label=cfg.start_label, required=True)
        self.end_dt_in   = discord.ui.TextInput(label=cfg.end_label, required=True)
        self.loc_in      = discord.ui.TextInput(label=cfg.location_label, required=False)
        self.desc_in     = discord.ui.TextInput(label=cfg.details_label, style=discord.TextStyle.paragraph, required=False, max_length=1000)
        for x in (self.title_in, self.start_dt_in, self.end_dt_in, self.loc_in, self.desc_in):
            self.add_item(x)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        # parse datetimes
        try:
            tz = ZoneInfo(TZ_NAME)
            start_full = _parse_mdy12(self.start_dt_in.value).replace(tzinfo=tz)
            end_full   = _parse_mdy12(self.end_dt_in.value).replace(tzinfo=tz)
            if end_full.date() < start_full.date():
                await interaction.followup.send("End date is before start date.", ephemeral=True)
                return
        except Exception:
            await interaction.followup.send("Invalid datetime. Use MM/DD/YYYY h:mm am/pm.", ephemeral=True)
            return

        sh, smin = start_full.hour, start_full.minute
        eh, emin = end_full.hour,   end_full.minute

        # resolve channel
        channel = interaction.guild.get_channel(self.cfg.channel_id) if interaction.guild else None
        if channel is None:
            try:
                channel = await interaction.client.fetch_channel(self.cfg.channel_id)
            except discord.Forbidden:
                await interaction.followup.send("Bot lacks permission to view that channel.", ephemeral=True); return
            except discord.NotFound:
                await interaction.followup.send("Channel ID not found. Check config.", ephemeral=True); return
            except Exception as e:
                await interaction.followup.send(f"Fetch error: {e}", ephemeral=True); return
        if not isinstance(channel, discord.ForumChannel):
            await interaction.followup.send(f"Channel type is {getattr(channel, 'type', '?')}. Need a Forum channel.", ephemeral=True); return

        # tag picker
        tag_view = ForumTagView(channel, confirm_label="Use tags")
        await interaction.followup.send(self.cfg.tag_prompt, view=tag_view, ephemeral=True)
        await tag_view.wait()
        chosen_names = tag_view.selected
        if not chosen_names:
            return  # canceled or none

        # per day calendar events
        calendar_links: list[str] = []
        start_day = start_full.replace(hour=0, minute=0, second=0, microsecond=0)
        end_day   = end_full.replace(hour=0, minute=0, second=0, microsecond=0)
        for day in _daterange(start_day, end_day):
            start_dt = day.replace(hour=sh, minute=smin)
            end_dt   = day.replace(hour=eh, minute=emin)
            if end_dt <= start_dt:
                end_dt += timedelta(days=1)
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
            await interaction.followup.send("No valid tags selected. Aborting post.", ephemeral=True); return

        # embed and thread title
        when_lines = []
        for day in _daterange(start_day, end_day):
            when_lines.append(day.strftime("%a %b %d") + f"  {start_full.strftime('%H:%M')}-{end_full.strftime('%H:%M')}")
        # title prefix MM/DD or MM/DD-MM/DD
        title_prefix = start_day.strftime("%m/%d") if start_day.date() == end_day.date() else f"{start_day.strftime('%m/%d')}-{end_day.strftime('%m/%d')}"
        final_title = f"{title_prefix} {self.title_in.value}"

        embed = discord.Embed(title=final_title, description=self.desc_in.value or "")
        embed.add_field(name="When", value="\n".join(when_lines), inline=False)
        if self.loc_in.value:
            embed.add_field(name="Where", value=self.loc_in.value, inline=False)
        embed.add_field(name="Tags", value=", ".join(chosen_names), inline=False)

        if calendar_links:
            if len(calendar_links) == 1:
                cal_field = f"[Google Calendar]({calendar_links[0]})"
            else:
                labels = []
                idx = 0
                for day in _daterange(start_day, end_day):
                    if idx >= len(calendar_links):
                        break
                    labels.append(f"[{day.strftime('%a %b %d')}]({calendar_links[idx]})")
                    idx += 1
                cal_field = " | ".join(labels[:3])
                if len(calendar_links) > 3:
                    cal_field += f" (+{len(calendar_links)-3} more)"
            embed.add_field(name="Calendar", value=cal_field, inline=False)

        await channel.create_thread(
            name=final_title,
            content=None,
            embed=embed,
            applied_tags=tags[:5],
        )
        await interaction.followup.send("Event posted.", ephemeral=True)

# ---------- cog that auto registers commands from CONFIGS

class DynamicEvents(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._added: list[app_commands.Command] = []

    async def cog_load(self):
        # create one slash command per config
        for cfg in CONFIGS:
            async def _cb(interaction: discord.Interaction, _cfg=cfg):
                # optional role check
                if _cfg.require_role:
                    has = discord.utils.get(getattr(interaction.user, "roles", []), name=_cfg.require_role)
                    if not has:
                        await interaction.response.send_message(f"Requires role: {_cfg.require_role}", ephemeral=True)
                        return
                await interaction.response.send_modal(DynamicEventModal(_cfg))

            cmd = app_commands.Command(
                name=cfg.command_name,
                description=cfg.command_description,
                callback=_cb,
            )
            self.bot.tree.add_command(cmd)
            self._added.append(cmd)

    async def cog_unload(self):
        for cmd in self._added:
            try:
                self.bot.tree.remove_command(cmd.name)
            except Exception:
                pass
        self._added.clear()

async def setup(bot: commands.Bot):
    await bot.add_cog(DynamicEvents(bot))

