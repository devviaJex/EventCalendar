# cogs/subscriptions.py
import discord
from discord import app_commands
from discord.ext import commands

from shared import INTEREST_TAGS, EVENT_CHANNEL_ID, CREATE_FROM_CHANNEL_ID, _GCAL

class Subscriptions(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(description="Subscribe to an event interest tag")
    @app_commands.describe(tag="Pick a tag to subscribe to")
    @app_commands.choices(tag=[app_commands.Choice(name=t, value=t) for t in INTEREST_TAGS])
    async def notify_subscribe(self, interaction: discord.Interaction, tag: app_commands.Choice[str]):
        role = discord.utils.get(interaction.guild.roles, name=tag.value)
        if not role:
            try:
                role = await interaction.guild.create_role(name=tag.value, mentionable=False, reason="Interest role")
            except Exception as e:
                return await interaction.response.send_message(f"Could not create role: {e}", ephemeral=True)
        try:
            await interaction.user.add_roles(role, reason="Interest subscribe")
            await interaction.response.send_message(f"Subscribed to **{tag.value}**", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Could not add role: {e}", ephemeral=True)

    @app_commands.command(description="Unsubscribe from an event interest tag")
    @app_commands.describe(tag="Pick a tag to leave")
    @app_commands.choices(tag=[app_commands.Choice(name=t, value=t) for t in INTEREST_TAGS])
    async def notify_unsubscribe(self, interaction: discord.Interaction, tag: app_commands.Choice[str]):
        role = discord.utils.get(interaction.guild.roles, name=tag.value)
        if not role:
            return await interaction.response.send_message("You are not subscribed.", ephemeral=True)
        try:
            await interaction.user.remove_roles(role, reason="Interest unsubscribe")
            await interaction.response.send_message(f"Unsubscribed from **{tag.value}**", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Could not remove role: {e}", ephemeral=True)

    @app_commands.command(description="Show this channel's ID")
    async def channelid(self, interaction: discord.Interaction):
        ch = interaction.channel
        gid = interaction.guild_id
        await interaction.response.send_message(
            f"Guild ID: `{gid}`\nChannel ID: `{ch.id}`\nName: {ch.name}",
            ephemeral=True,
        )

    @app_commands.command(description="Show Google Calendar event colors")
    async def event_colors(self, interaction: discord.Interaction):
        pal = await interaction.client.loop.run_in_executor(None, lambda: _GCAL.colors().get().execute())
        lines = []
        for cid, spec in pal.get("event", {}).items():
            lines.append(f"{cid}: {spec['background']} on {spec['foreground']}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(description="Subscribe panel (creates roles if missing)")
    async def notify_panel(self, interaction: discord.Interaction):
        class PanelView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=None)
                options = [discord.SelectOption(label=t, value=t) for t in INTEREST_TAGS]
                self.select = discord.ui.Select(placeholder="Pick interests to toggle", min_values=0, max_values=len(options), options=options)
                self.add_item(self.select)

            @discord.ui.select()
            async def on_select(self, interaction: discord.Interaction, select: discord.ui.Select):
                added, removed = [], []
                for t in select.values:
                    role = discord.utils.get(interaction.guild.roles, name=t)
                    if not role:
                        role = await interaction.guild.create_role(name=t, mentionable=False, reason="Interest role")
                    if role in interaction.user.roles:
                        await interaction.user.remove_roles(role, reason="Interest toggle off")
                        removed.append(t)
                    else:
                        await interaction.user.add_roles(role, reason="Interest toggle on")
                        added.append(t)
                msg = []
                if added: msg.append("Added: " + ", ".join(added))
                if removed: msg.append("Removed: " + ", ".join(removed))
                await interaction.response.send_message("; ".join(msg) or "No changes.", ephemeral=True)

        await interaction.response.send_message("Pick any interests to subscribe/unsubscribe:", view=PanelView(), ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Subscriptions(bot))
