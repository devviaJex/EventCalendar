# cogs/event_wizard.py

from typing import List, Tuple
import re
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

from shared import EVENT_CHANNEL_ID, get_tags, gcal_insert_event, TZ_NAME

TAG_TYPES = ["Area", "Activity Type", "Interest"]


def _part_of_day_tag(dt_: datetime) -> str:
    return "mid-day" if 11 <= dt_.hour < 14 else ("am" if dt_.hour < 12 else "pm")


def resolve_forum_tags(
    channel: discord.ForumChannel, names: list[str]
) -> tuple[list[discord.ForumTag], list[str]]:
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


class TagSelect(discord.ui.Select):
    def __init__(self, tag_type: str, options: List[Tuple[str, str]]):
        opts = [
            discord.SelectOption(
                label=name[:100], description=(desc[:95] if desc else None)
            )
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
        for x in (self.title_in, self.date_in, self.time_in, self.loc_in, self.desc_in):
            self.add_item(x)

async def on_submit(self, interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        # 1) parse date/time
        y, m, d = map(int, self.date_in.value.strip().split("-"))
        hh, mm = map(int, re.split("[: ]", self.time_in.value.strip())[:2])
        start = datetime(y, m, d, hh, mm, tzinfo=ZoneInfo(TZ_NAME))
        end = start + timedelta(hours=2)
        part_tag = _part_of_day_tag(start)

        # 2) tag picker
        try:
            options = await get_tags(self.tag_type)
        except Exception as e:
            options = []
            await interaction.followup.send(f"Tag load failed: {e}", ephemeral=True)
        view = TagSelectView(self.tag_type, options)
        await interaction.followup.send(
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
        chosen: list[str] = view.selected or []

        # 3) calendar
        gcal_link = None
        try:
            g = await gcal_insert_event(
                self.title_in.value, start, end,
                self.loc_in.value or None, self.desc_in.value or None
            )
            if g:
                gcal_link = g.get("htmlLink")
            else:
                await interaction.followup.send(
                    "Calendar insert returned no event. Check CALENDAR_ID and sharing.",
                    ephemeral=True,
                )
        except Exception as e:
            await interaction.followup.send(f"Google Calendar error: {e}", ephemeral=True)

        # 4) embed
        embed = discord.Embed(title=self.title_in.value, description=self.desc_in.value or "")
        embed.add_field(name="When", value=start.strftime("%a %b %d, %I:%M %p"))
        if self.loc_in.value:
            embed.add_field(name="Where", value=self.loc_in.value, inline=False)
        if chosen:
            embed.add_field(name=f"{self.tag_type}", value=", ".join(chosen), inline=False)
        embed.set_footer(text=part_tag)

        # 5) post to channel
        channel = interaction.client.get_channel(EVENT_CHANNEL_ID)
        if not channel:
            await interaction.followup.send("Event channel not found.", ephemeral=True)
            return

        if isinstance(channel, discord.ForumChannel):
            wanted = [part_tag] + chosen
            tags, missing = resolve_forum_tags(channel, wanted)
            if missing:
                await interaction.followup.send(
                    f"Missing forum tags: {', '.join(missing)}", ephemeral=True
                )
            await channel.create_thread(
                name=self.title_in.value,
                content=(gcal_link or None),
                embed=embed,
                applied_tags=tags[:5],  # list[discord.ForumTag]
            )
        else:
            await channel.send(content=(gcal_link or None), embed=embed)

        await interaction.followup.send("Event created.", ephemeral=True)

    except Exception as e:
        import traceback; traceback.print_exc()
        try:
            await interaction.followup.send(f"Error: {e}", ephemeral=True)
        except Exception:
            pass



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
