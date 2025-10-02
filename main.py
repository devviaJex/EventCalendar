import os
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

intents = discord.Intents.default()
intents.members = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

EXTS = [
    "cogs.events",
    "cogs.subscriptions",
    "cogs.reminders",
    "cogs.event_wizard",
    "cogs.yardsale_event",
    "cogs.dynamic_events",
]

@bot.event
async def on_ready():
    guild = bot.get_guild(GUILD_ID)
    guildname = guild.name if guild else f"id:{GUILD_ID}"

    if not getattr(bot, "_cogs_loaded", False):
        try:
            for ext in EXTS:
                await bot.load_extension(ext)
            await bot.tree.sync(guild=discord.Object(GUILD_ID))
            await bot.tree.sync()
            bot._cogs_loaded = True
        except Exception as e:
            print("Cog or sync error:", e)

    print(f"Logged in as {bot.user} for Guild #{GUILD_ID}/{guildname}")

@bot.tree.command(description="Where is the bot running?")
async def whereami(interaction: discord.Interaction):
    platform = "Unknown"
    if os.environ.get("P_SERVER_UUID") or os.environ.get("P_SERVER_ALLOCATION_ID"):
        platform = "Pterodactyl (bot-hosting.net)"
    elif os.environ.get("REPL_ID"):
        platform = "Replit"
    elif os.environ.get("RAILWAY_PROJECT_ID"):
        platform = "Railway"
    await interaction.response.send_message(f"Running on **{platform}**.", ephemeral=True)

#async def _cb(interaction: discord.Interaction, _cfg=cfg):

    if __name__ == "__main__":
        token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN is required in environment or .env")
    bot.run(token)
