# cogs/event_wizard.py
"""Interactive event creation flow split into its own cog, with select limits respected.
- Day picker splits into two selects if month has >25 days.
- Time picker uses separate Hour and Minute selects (00/30) to stay under 25 options.
"""
from typing import Dict, Any
from datetime import datetime, timedelta
import calendar

import discord
from discord import app_commands
from discord.ext import commands

from shared import (
    TZ, TZ_NAME, EMBED_COLOR,
    EVENT_CHANNEL_ID, CREATE_FROM_CHANNEL_ID,
    INTEREST_TAGS, MODE_CHOICES, norm_tag, pick_color_id,
    gcal_insert_event, db_exec, display_dt,
)

# ----------------- Small helper: post embed + thread -----------------
async def _post_event_embed(channel: discord.TextChannel, event: dict):
    start_iso = event["start"].get("dateTime") or event["start"].get("date")
    end_iso = event["end"].get("dateTime") or event["end"].get("date")

    title = event.get("summary", "Event")
    desc = event.get("description") or ""
    url = event.get("htmlLink")

    embed = discord.Embed(title=title, description=desc, colour=discord.Colour(EMBED_COLOR))
    if start_iso:
        embed.add_field(name="Starts", value=display_dt(start_iso), inline=True)
    if end_iso:
        embed.add_field(name="Ends", value=display_dt(end_iso), inline=True)
    if url:
        embed.add_field(name="Calendar", value=url, inline=False)
    embed.set_footer(text="Filed, tagged, and threaded — Marsh Mellow 🐊")  # optional

    msg = await channel.send(embed=embed)

    thread = None
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

    try:
        await db_exec(
            "INSERT OR REPLACE INTO events_map(discord_message_id, event_id, channel_id, thread_id) VALUES(?,?,?,?)",
            (msg.id, event["id"], channel.id, thread.id if thread else None),
        )
    except Exception:
        pass

# ----------------- Wizard state -----------------
_WIZ_STATE: Dict[int, Dict[str, Any]] = {}

# ----------------- UI Components -----------------
class ModeSelect(discord.ui.Select):
    def __init__(self, user_id: int):
        options = [discord.SelectOption(label=m, value=m) for m in MODE_CHOICES]
        super().__init__(placeholder="Mode (In person / Online)", min_values=0, max_values=1, options=options)
        self.user_id = user_id
    async def callback(self, interaction: discord.Interaction):
        st = _WIZ_STATE.setdefault(self.user_id, {})
        st["mode"] = self.values[0] if self.values else None
        await interaction.response.edit_message(view=self.view)

class TagMultiSelect(discord.ui.Select):
    def __init__(self, user_id: int):
        options = [discord.SelectOption(label=t, value=t) for t in INTEREST_TAGS]
        super().__init__(placeholder="Choose tags (optional)", min_values=0, max_values=min(len(options), 25), options=options)
        self.user_id = user_id
    async def callback(self, interaction: discord.Interaction):
        st = _WIZ_STATE.setdefault(self.user_id, {})
        st["tags"] = list(self.values)
        await interaction.response.edit_message(view=self.view)

class EventDetailsModal(discord.ui.Modal, title="Event details"):
    title_input = discord.ui.TextInput(label="Title", placeholder="Game night at the cafe", max_length=100)
    location_input = discord.ui.TextInput(label="Location (optional)", required=False, max_length=120)
    desc_input = discord.ui.TextInput(label="Description (optional)", style=discord.TextStyle.paragraph, required=False, max_length=1024)
    def __init__(self, user_id: int):
        super().__init__()
        self.user_id = user_id
    async def on_submit(self, interaction: discord.Interaction):
        st = _WIZ_STATE.setdefault(self.user_id, {})
        st["title"] = str(self.title_input.value).strip()
        st["location"] = str(self.location_input.value or "").strip()
        st["details"] = str(self.desc_input.value or "").strip()
        await interaction.response.edit_message(content="Saved details. Continue configuring below:", view=EventWizardPage1(self.user_id))

class YearSelect(discord.ui.Select):
    def __init__(self, user_id: int):
        now = datetime.now(TZ)
        options = [discord.SelectOption(label=str(y), value=str(y)) for y in (now.year, now.year + 1)]
        super().__init__(placeholder="Year", min_values=1, max_values=1, options=options)
        self.user_id = user_id
    async def callback(self, interaction: discord.Interaction):
        st = _WIZ_STATE.setdefault(self.user_id, {})
        st["year"] = int(self.values[0])
        await interaction.response.edit_message(view=self.view.parent.refresh_days())

class MonthSelect(discord.ui.Select):
    def __init__(self, user_id: int):
        options = [discord.SelectOption(label=calendar.month_name[m], value=str(m)) for m in range(1,13)]
        super().__init__(placeholder="Month", min_values=1, max_values=1, options=options)
        self.user_id = user_id
    async def callback(self, interaction: discord.Interaction):
        st = _WIZ_STATE.setdefault(self.user_id, {})
        st["month"] = int(self.values[0])
        await interaction.response.edit_message(view=self.view.parent.refresh_days())

class DaySelect(discord.ui.Select):
    def __init__(self, user_id: int, year: int, month: int, start_day: int, end_day: int, placeholder: str):
        options = [discord.SelectOption(label=str(d), value=str(d)) for d in range(start_day, end_day + 1)]
        super().__init__(placeholder=placeholder, min_values=1, max_values=1, options=options)
        self.user_id = user_id
    async def callback(self, interaction: discord.Interaction):
        st = _WIZ_STATE.setdefault(self.user_id, {})
        st["day"] = int(self.values[0])
        await interaction.response.edit_message(view=self.view)

class HourSelect(discord.ui.Select):
    def __init__(self, user_id: int):
        # hours 8..22 inclusive (local)
        options = [discord.SelectOption(label=f"{h:02d}:00", value=str(h)) for h in range(8, 23)]
        super().__init__(placeholder="Hour (24h local)", min_values=1, max_values=1, options=options)
        self.user_id = user_id
    async def callback(self, interaction: discord.Interaction):
        st = _WIZ_STATE.setdefault(self.user_id, {})
        st["hour"] = int(self.values[0])
        await interaction.response.edit_message(view=self.view)

class MinuteSelect(discord.ui.Select):
    def __init__(self, user_id: int):
        options = [discord.SelectOption(label=m, value=m) for m in ("00", "30")]
        super().__init__(placeholder="Minutes", min_values=1, max_values=1, options=options)
        self.user_id = user_id
    async def callback(self, interaction: discord.Interaction):
        st = _WIZ_STATE.setdefault(self.user_id, {})
        st["minute"] = int(self.values[0])
        await interaction.response.edit_message(view=self.view)

class EventWizardPage1(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=600)
        self.user_id = user_id
        self.add_item(ModeSelect(user_id))
        self.add_item(TagMultiSelect(user_id))

    @discord.ui.button(label="Enter/Update Details", style=discord.ButtonStyle.secondary)
    async def details(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(EventDetailsModal(self.user_id))

    @discord.ui.button(label="Next ➡️", style=discord.ButtonStyle.primary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Pick date and time:", view=EventWizardPage2(self.user_id))

class EventWizardPage2(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=600)
        self.user_id = user_id
        now = datetime.now(TZ)
        st = _WIZ_STATE.setdefault(user_id, {})
        st.setdefault("year", now.year)
        st.setdefault("month", now.month)
        st.setdefault("day", now.day)

        # top row: year / month
        self.year = YearSelect(user_id)
        self.month = MonthSelect(user_id)
        self.add_item(self.year)
        self.add_item(self.month)

        # day rows (split if needed)
        self.build_day_selects()

        # time rows
        self.hour = HourSelect(user_id)
        self.minute = MinuteSelect(user_id)
        self.add_item(self.hour)
        self.add_item(self.minute)

    def build_day_selects(self):
        # remove any existing DaySelects
        for child in list(self.children):
            if isinstance(child, DaySelect):
                self.remove_item(child)

        st = _WIZ_STATE.setdefault(self.user_id, {})
        y = st.get("year", datetime.now(TZ).year)
        m = st.get("month", datetime.now(TZ).month)
        _, last_day = calendar.monthrange(y, m)

        # Discord limit: max 25 options per select. Split days into two selects if needed.
        if last_day <= 25:
            self.add_item(DaySelect(self.user_id, y, m, 1, last_day, "Day"))
        else:
            # first 1..16 (16 options) and 17..last (<=15 options)
            self.add_item(DaySelect(self.user_id, y, m, 1, 16, "Day 1–16"))
            self.add_item(DaySelect(self.user_id, y, m, 17, last_day, f"Day 17–{last_day}"))

    def refresh_days(self):
        self.build_day_selects()
        return self

    @discord.ui.button(label="⬅️ Back", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Configure mode/tags and details:", view=EventWizardPage1(self.user_id))

    @discord.ui.button(label="Create Event", style=discord.ButtonStyle.success)
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button):
        st = _WIZ_STATE.get(self.user_id, {})
        title = st.get("title")
        if not title:
            return await interaction.response.send_message("Please click **Enter/Update Details** and add a title first.", ephemeral=True)
        try:
            y = int(st.get("year")); m = int(st.get("month")); d = int(st.get("day"))
            hh = int(st.get("hour", 18)); mm = int(st.get("minute", 0))
            from pytz import timezone
            start_local = timezone(TZ_NAME).localize(datetime(y, m, d, hh, mm))
            end_local = start_local + timedelta(minutes=60)
        except Exception as e:
            return await interaction.response.send_message(f"Date/time error: {e}", ephemeral=True)

        tags = [norm_tag(t) for t in st.get("tags", [])]
        mode = st.get("mode")
        if mode and mode not in tags:
            tags.insert(0, mode)
        color_id = pick_color_id(tags)
        body = {
            "summary": title,
            "location": st.get("location", ""),
            "description": st.get("details", ""),
            "start": {"dateTime": start_local.isoformat()},
            "end": {"dateTime": end_local.isoformat()},
        }
        if color_id:
            body["colorId"] = color_id

        target_channel = interaction.guild.get_channel(EVENT_CHANNEL_ID) if EVENT_CHANNEL_ID else interaction.channel
        if not target_channel:
            return await interaction.response.send_message("I can't find the target events channel. Check EVENT_CHANNEL_ID.", ephemeral=True)

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            event = await gcal_insert_event(body)
            for t in tags:
                await db_exec("INSERT OR REPLACE INTO event_tags(event_id, tag) VALUES(?,?)", (event["id"], t))
            await _post_event_embed(target_channel, event)

            roles_to_ping = []
            if mode:
                r = discord.utils.get(interaction.guild.roles, name=mode)
                if r:
                    roles_to_ping.append(r)
            for t in tags:
                if mode and t == mode:
                    continue
                r = discord.utils.get(interaction.guild.roles, name=t)
                if r:
                    roles_to_ping.append(r)
                    break
            if roles_to_ping:
                allowed = discord.AllowedMentions(roles=True)
                await target_channel.send(
                    "New event for " + " ".join(r.mention for r in roles_to_ping),
                    allowed_mentions=allowed,
                )
            await interaction.followup.send("Event created!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Calendar error: {e}", ephemeral=True)

# ----------------- Cog -----------------
class EventWizard(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="event_wizard", description="Interactive event creator with date & time pickers (≤25 options per menu)")
    async def event_wizard(self, interaction: discord.Interaction):
        allowed_sources = {c for c in [EVENT_CHANNEL_ID, CREATE_FROM_CHANNEL_ID] if c}
        if allowed_sources and interaction.channel_id not in allowed_sources:
            return await interaction.response.send_message(
                "Start the wizard in the events or create channel only.", ephemeral=True
            )
        _WIZ_STATE[interaction.user.id] = {}
        await interaction.response.send_message(
            "Configure mode/tags and details:", view=EventWizardPage1(interaction.user.id), ephemeral=True
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(EventWizard(bot))
