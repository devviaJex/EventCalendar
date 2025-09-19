import os
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
GUILD_ID = int(os.getenv("GUILD_ID"))
@bot.event
async def on_ready():
    if not getattr(bot, "_cogs_loaded", False):
        try:
            await bot.load_extension("cogs.events")
            await bot.load_extension("cogs.subscriptions")
            await bot.load_extension("cogs.reminders")
            await bot.load_extension("cogs.sync_members")
            await bot.tree.sync(guild=discord.Object(GUILD_ID)) 
            bot._cogs_loaded = True
        except Exception as e:
            print("Cog load error:", e)
    try:
        await bot.tree.sync()
    except Exception as e:
        print("Sync error:", e)
    print(f"Logged in as {bot.user}")

@bot.tree.command(description="Where is the bot running?")
async def whereami(interaction: discord.Interaction):
    platform = "Unknown"
    if os.environ.get("P_SERVER_UUID") or os.environ.get("P_SERVER_ALLOCATION_ID"):
        platform = "Pterodactyl (bot-hosting.net)"
    elif os.environ.get("REPL_ID"):
        platform = "Replit"
    elif os.environ.get("RAILWAY_PROJECT_ID"):
        platform = "Railway"
    await interaction.response.send_message(
        f"Running on **{platform}**.", ephemeral=True
    )

if __name__ == "__main__":
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN is required in environment or .env")
    bot.run(token)

# added for sync roles function
async def setup_extensions():
    await bot.load_extension("cogs.roles_sync")

async def main():
    async with bot:
        await setup_extensions()
        await bot.start(os.getenv("DISCORD_BOT_TOKEN"))

import asyncio
asyncio.run(main())
