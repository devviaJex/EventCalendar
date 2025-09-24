# cogs/event_wizard.py — modal + per-tag select flow

import asyncio
from typing import List

import discord
from discord import app_commands
from discord.ext import commands

from shared import (
    EVENT_CHANNEL_ID,
    get_tags,
)

TAG_TYPES = ["Area", "Activity Type", "Interest"]

class TagSelect(discord.ui.Select):
    def __init__(self, tag_type: str, options: List[tuple[str, str]]):
        # discord.Select limited to 25 options
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
        await interaction.response.edit_message(content=f"Chosen {self.tag_type}: {', '.join(view.selected) if view.selected else 'none'}", view=view)

class TagSelectView(discord.ui.View):
    def __init__(self, tag_type: str, options: List[tuple[str, str]]):
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
        self.desc_in = discord.ui.TextInput(label="Details", style=discord.TextStyle.paragraph, required=False, max_length=1000)
        self.add_item(self.title_in)
        self.add_item(self.date_in)
        self.add_item(self.time_in)
        self.add_item(self.loc_in)
        self.add_item(self.desc_in)

    async def on_submit(self, interaction: discord.Interaction):
        # After basic fields, present a select for the chosen tag_type
        options = await get_tags(self.tag_type)
        view = TagSelectView(self.tag_type, options)
        await interaction.response.send_message(
            content=(
                f"Title: {self.title_in.value}\n"
                f"Date: {self.date_in.value} {self.time_in.value}\n"
                f"Location: {self.loc_in.value or '-'}\n"
                f"Add {self.tag_type} tags:"
            ),
            view=view,
            ephemeral=True,
        )
        await view.wait()
        if not view.selected:
            # proceed without tags
            pass
        # Here you would persist the event draft with view.selected
        # or create the event thread in EVENT_CHANNEL_ID.

class EventWizard(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
@app_commands.command(name="event_create", description="Open event creator with a tag mode")
@app_commands.describe(tag_type="Which tag category to use")
@app_commands.choices(tag_type=[app_commands.Choice(name=t, value=t) for t in TAG_TYPES])
async def event_create(self, interaction: discord.Interaction, tag_type: app_commands.Choice[str]):
    await interaction.response.send_modal(EventCreateModal(tag_type.value))

# optional fixed-mode shortcuts
@app_commands.command(name="event_create_interest", description="Create event with Interest tags")
async def event_create_interest(self, i: discord.Interaction):
    await i.response.send_modal(EventCreateModal("Interest"))

@app_commands.command(name="event_create_area", description="Create event with Area tags")
async def event_create_area(self, i: discord.Interaction):
    await i.response.send_modal(EventCreateModal("Area"))

@app_commands.command(name="event_create_activity", description="Create event with Activity Type tags")
async def event_create_activity(self, i: discord.Interaction):
    await i.response.send_modal(EventCreateModal("Activity Type"))
    
    @app_commands.command(name="event_wizard", description="Open event wizard with a tag mode")
    @app_commands.describe(tag_type="Which tag category to use")
    @app_commands.choices(tag_type=[app_commands.Choice(name=t, value=t) for t in TAG_TYPES])
    async def event_create(self, interaction: discord.Interaction, tag_type: app_commands.Choice[str]):
        await interaction.response.send_modal(EventCreateModal(tag_type.value))

async def setup(bot: commands.Bot):
    await bot.add_cog(EventWizard(bot))
