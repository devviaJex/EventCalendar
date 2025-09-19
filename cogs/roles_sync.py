# cogs/roles_sync.py
import discord
from discord import app_commands
from discord.ext import commands
from shared import list_interest_roles

class RolesSync(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    roles = app_commands.Group(name="roles", description="Role utilities")

    @roles.command(name="sync", description="Create missing roles from the Google Sheet")
    @app_commands.describe(prune_missing="Delete roles not in the sheet (only if no members). Default: false")
    @app_commands.default_permissions(manage_roles=True)
    async def sync(
        self,
        interaction: discord.Interaction,
        prune_missing: bool = False,
    ):
        if not interaction.user.guild_permissions.manage_roles:
            return await interaction.response.send_message(
                "Need Manage Roles permission.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        sheet_names = await list_interest_roles()              # list[str]
        sheet_set = {n for n in (s.strip() for s in sheet_names) if n}

        created = []
        for name in sheet_set:
            if not discord.utils.get(guild.roles, name=name):
                r = await guild.create_role(name=name, mentionable=False, reason="sync from sheet")
                created.append(r.name)

        pruned = []
        if prune_missing:
            keep = sheet_set
            for r in list(guild.roles):
                if r.is_default() or r.managed:
                    continue
                if r.name not in keep and len(r.members) == 0:
                    await r.delete(reason="prune: not in sheet and no members")
                    pruned.append(r.name)

        msg = f"Created: {len(created)} | Pruned: {len(pruned)} | Total sheet roles: {len(sheet_set)}"
        if created:
            msg += "\nNew: " + ", ".join(created[:20]) + (" ..." if len(created) > 20 else "")
        if pruned:
            msg += "\nDeleted: " + ", ".join(pruned[:20]) + (" ..." if len(pruned) > 20 else "")
        await interaction.followup.send(msg, ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(RolesSync(bot))

