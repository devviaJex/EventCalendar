# cogs/sync_hosts.py
import discord
from discord import app_commands
from discord.ext import commands
from typing import List

from shared import open_ws, GUILD_ID  # your helper that returns a gspread worksheet

         # your server
HOST_ROLE_NAMES = {"Event Host"}       # match by role name (case-sensitive)
MEMBERS_TAB = "Members"                # sheet tab to write

class SyncHosts(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="sync_event_hosts", description="Update Members sheet with users who have the Event Host role.")
    @app_commands.checks.has_permissions(administrator=True)
    async def sync_event_hosts(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)

        guild = self.bot.get_guild(GUILD_ID)
        if guild is None:
            guild = await self.bot.fetch_guild(GUILD_ID)

        # Ensure member cache is populated
        # Requires intents.members = True
        members: List[discord.Member] = []
        async for m in guild.fetch_members(limit=None):
            members.append(m)

        # Filter by role name
        def has_host(m: discord.Member) -> bool:
            return any(r.name in HOST_ROLE_NAMES for r in m.roles if r is not None)

        hosts = [m for m in members if has_host(m)]

        # Build rows
        header = ["Display Name", "Username", "User ID", "Roles"]
        rows = [header]
        for m in hosts:
            roles = ", ".join(sorted(r.name for r in m.roles if r is not None and r.name != "@everyone"))
            rows.append([m.display_name, f"{m.name}#{m.discriminator}", str(m.id), roles])

        # Write to Google Sheet
        ws = open_ws(MEMBERS_TAB)  # uses your SPREADSHEET_ID inside shared.py
        ws.clear()
        ws.update(f"A1:D{len(rows)}", rows, value_input_option="RAW")

        await interaction.followup.send(f"Synced {len(hosts)} event host(s) to '{MEMBERS_TAB}'.", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(SyncHosts(bot))

