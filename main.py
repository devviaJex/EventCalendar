import os, discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

GUILD_ID  = int(os.getenv("GUILD_ID","0"))
GUILD_OBJ = discord.Object(id=GUILD_ID)

intents = discord.Intents.default()
intents.guilds = True
bot = commands.Bot(command_prefix="!", intents=intents)

EXTS = [
    "cogs.events",
    "cogs.subscriptions",
    "cogs.reminders",
    "cogs.yardsale_event",
    "cogs.foodeats_event"
]

async def setup_extensions():
    for ext in EXTS:
        try:
            await bot.load_extension(ext)
            print(f"[cogs] loaded: {ext}")
        except Exception as e:
            print(f"[cogs] FAILED: {ext} -> {e}")

@bot.event
async def setup_hook():
    # 1) load cogs
    await setup_extensions()

    # 2) add a trivial command directly to the guild tree
    async def _ping(i: discord.Interaction):
        await i.response.send_message("pong", ephemeral=True)
    bot.tree.add_command(
        app_commands.Command(name="pingme", description="healthcheck", callback=_ping),
        guild=GUILD_OBJ,
    )

    # 3) sync this guild only
    cmds = await bot.tree.sync(guild=GUILD_OBJ)
    print("[setup_hook] Guild commands:", [c.name for c in cmds])

@bot.event
async def on_ready():
    g = bot.get_guild(GUILD_ID)
    print(f"READY as {bot.user} on {g.name if g else GUILD_ID}")

bot.run(os.getenv("DISCORD_BOT_TOKEN"))
