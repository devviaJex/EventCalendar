# cogs/yardsale_event.py

import os, discord
from discord import app_commands
from discord.ext import commands
from shared import YARDSALE_CHANNEL_ID
from shared_event_utils import EventCfg, EventModal

GUILD_OBJ = discord.Object(id=int(os.getenv("GUILD_ID","0")))

class YardSaleEvent(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._added: list[app_commands.Command] = []

    async def cog_load(self):
        async def _cb(i: discord.Interaction):
            await i.response.send_modal(EventModal(EventCfg(
                channel_id=YARDSALE_CHANNEL_ID,
                modal_title="Schedule Yard/Estate/Auction sale",
                tag_prompt="Choose up to 5 tags for this sale:",
            )))
        cmd = app_commands.Command(
            name="event_yardsale_create",
            description="Create Yard/Estate/Auction event",
            callback=_cb,  # only 'interaction' param
        )
        self.bot.tree.add_command(cmd, guild=GUILD_OBJ)
        self._added.append(cmd)
        print("[yardsale_event] registered /event_yardsale_create")

    async def cog_unload(self):
        for cmd in self._added:
            try:
                self.bot.tree.remove_command(cmd.name, guild=GUILD_OBJ)
            except Exception:
                pass
        self._added.clear()

async def setup(bot: commands.Bot):
    await bot.add_cog(YardSaleEvent(bot))
