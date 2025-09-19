# cogs/subscriptions.py
import discord
from discord import app_commands
from discord.ext import commands
from shared import list_interest_roles

async def _autocomplete_interest_tags(interaction: discord.Interaction, current: str):
    names = await list_interest_roles()              # list[str]
    q = (current or "").lower()
    return [app_commands.Choice(name=n, value=n) for n in names if q in n.lower()][:25]

class Subscriptions(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="subscribe", description="Subscribe to an interest")
    @app_commands.describe(tag="Pick a tag to subscribe to")
    @app_commands.autocomplete(tag=_autocomplete_interest_tags)
    async def subscribe(self, interaction: discord.Interaction, tag: str):
        role = discord.utils.get(interaction.guild.roles, name=tag)
        if not role:
            role = await interaction.guild.create_role(name=tag, mentionable=False, reason="interest tag")
        await interaction.user.add_roles(role, reason=f"subscribe {tag}")
        await interaction.response.send_message(f"Subscribed to **{tag}**", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Subscriptions(bot))
