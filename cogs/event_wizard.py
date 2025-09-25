from typing import List, Tuple
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

from shared import EVENT_CHANNEL_ID, get_tags, gcal_insert_event, TZ_NAME

TAG_TYPES = ["Area", "Activity Type", "Interest"]

def _part_of_day_tag(dt_: datetime) -> str:
    return "mid-day" if 11 <= dt_.hour < 14 else ("am" if dt_.hour < 12 else "pm")

def _match_forum_tags(channel: discord.ForumChannel, wanted: list[str]) -> list[discord.ForumTag]:
    by_name = {t.name.lower(): t for t in channel.available_tags}
    out = []
    for w in wanted:
        t = by_name.get(w.lower())
        if t:
            out.append(t)
    return out

class TagSelect(discord.ui.Select):
    def __init__(self, tag_type: str, options: List[Tuple[str, str]]):
        opts = [
            discord.SelectOption(label=name[:100], description=(desc[:95] if desc else None))
            for name, desc in options[:25]
        ]
        super().__init__(
            placeholder=f"Select {tag_type} tags",
            min_values=0,
            max_values=min(5, len(opts)) or 1,
            options=opts or [discord.SelectOption(label=f"No {tag_type} tags available")],
        )
        self.tag_type = tag_type

    async def callback(self, interaction: discord.Interaction):
        view: TagSelectView = self.view  # type: ignore
        view.selected = list(self.values)
        await interaction.response.edit_message(
            content=f"Chosen {self.tag_type}: {', '.join(view.selected) if view.selected else 'none'}",
            view=view,
        )

class TagSelectView(discord.ui.View):
    def __init__(self, tag_type: str, options: List[Tuple[str, str]]):
        super().__init__(timeout=300)
        self.selected: List[str] = []
        self.add_item(TagSelect(tag_type, options))
        self.confirm = discord.ui.Button(style=discord.ButtonStyle.primary, label="Confirm")
        self.cancel = discord.ui.Button(style=discord.ButtonStyle.secondary, label="Cancel")
        self.confirm.callback = self._confirm  # type: ignore
        self.cancel.callback = self._cancel  # type: ignore
        self.add_item(self.confirm)
        self.add_item(self.cancel)

    async def _confirm(self, interaction: discord.Interaction):
        self.stop()
        await interaction.response.edit_message(content="Confirmed.", view=None)

    async def _cancel(self, interaction: discord.Interaction):
        self.selected = []
        self.stop()
        await interaction.response.edit_message(content="Canceled.", view=None)

class EventCreateModal(discord.ui.Modal, title="Create Event"):
    def __init__(self, tag_type: str):
        super().__init__()
        self.tag_type = tag_type
        self.title_in = discord.ui.TextInput(label="Title", max_length=120, required=True)
        self.date_in = discord.ui.TextInput(label="Date (YYYY-MM-DD)", required=True)
        self.time_in = discord.ui.TextInput(label="Time (HH:MM 24h)", required=True)
        self.loc_in = discord.ui.TextInput(label="Location", required=False)
        self.desc_in = discord.ui.TextInput(
            label="Details", style=discord.TextStyle.paragraph, required=False, max_length=1000
        )
        self.add_item(self.title_in)
        self.add_item(self.date_in)
        self.add_item(self.time_in)
        self.add_item(self.loc_in)
        self.add_item(self.desc_in)

    async def on_submit(self, interaction: discord.Interaction):
        # parse date/time
        try:
            y, m, d = map(int, self.date_in.value.strip().split("-"))
            hh, mm = map(int, re.split("[: ]", self.time_in.value.strip())[:2])
        except Exception:
            await interaction.response.send_message("Invalid date/time format.", ephemeral=True)
            return

        start = datetime(y, m, d, hh, mm, tzinfo=ZoneInfo(TZ_NAME))
        end = start + timedelta(hours=2)
        part_tag = _part_of_day_tag(start)

        # tag picker
        options = await get_tags(self.tag_type)
        view = TagSelectView(self.tag_type, options)
        await interaction.response.send_message(
            content=(
                f"Title: {self.title_in.value}\n"
                f"When: {start.strftime('%Y-%m-%d %H:%M')}\n"
                f"Where: {self.loc_in.value or '-'}\n"
                f"Add {self.tag_type} tags:"
            ),
            view=view,
            ephemeral=True,
        )
        await view.wait()
        chosen = view.selected or []

        # Google Calendar
        try:
            gcal_event = await gcal_insert_event(
                self.title_in.value, start, end, self.loc_in.value or None, self.desc_in.value or None
            )
            gcal_link = gcal_event.get("htmlLink") if gcal_event else None
        except Exception:
            gcal_link = None

        # Discord post
        channel = interaction.client.get_channel(EVENT_CHANNEL_ID)
        if not channel:
            await interaction.followup.send("Event channel not found.", ephemeral=True)
            return

        embed = discord.Embed(title=self.title_in.value, description=self.desc_in.value or "")
        embed.add_field(name="When", value=start.strftime("%a %b %d, %I:%M %p"))
        if self.loc_in.value:
            embed.add_field(name="Where", value=self.loc_in.value, inline=False)
        if chosen:
            embed.add_field(name=f"{self.tag_type}", value=", ".join(chosen), inline=False)
        embed.set_footer(text=part_tag)

        if isinstance(channel, discord.ForumChannel):
            tags = _match_forum_tags(channel, chosen + [part_tag])
            await channel.create_thread(
                name=self.title_in.value,
                content=(gcal_link or None),
                embed=embed,
                applied_tags=tags[:5] if tags else None,
            )
        else:
            await channel.send(content=(gcal_link or None), embed=embed)

        await interaction.followup.send("Event created.", ephemeral=True)

class EventWizard(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="event_wizard", description="Open event wizard with a tag mode")
    @app_commands.describe(tag_type="Which tag category to use")
    @app_commands.choices(tag_type=[app_commands.Choice(name=t, value=t) for t in TAG_TYPES])
    async def event_wizard_cmd(self, i: discord.Interaction, tag_type: app_commands.Choice[str]):
        await i.response.send_modal(EventCreateModal(tag_type.value))

    @app_commands.command(name="wizard_interest", description="Wizard with Interest tags")
    async def wizard_interest(self, i: discord.Interaction):
        await i.response.send_modal(EventCreateModal("Interest"))

    @app_commands.command(name="wizard_area", description="Wizard with Area tags")
    async def wizard_area(self, i: discord.Interaction):
        await i.response.send_modal(EventCreateModal("Area"))

    @app_commands.command(name="wizard_activity", description="Wizard with Activity Type tags")
    async def wizard_activity(self, i: discord.Interaction):
        await i.response.send_modal(EventCreateModal("Activity Type"))

async def setup(bot: commands.Bot):
    await bot.add_cog(EventWizard(bot))
