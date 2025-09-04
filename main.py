import os
import asyncio
import aiosqlite
from datetime import datetime, timedelta
from dateutil import parser as du_parser
import pytz
from dotenv import load_dotenv

import discord
from discord import app_commands
from discord.ext import commands, tasks

# Google Calendar
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ---------------------------
# Config and clients
# ---------------------------
load_dotenv()

TZ_NAME = os.getenv("TZ", "America/Chicago")
TZ = pytz.timezone(TZ_NAME)
CAL_ID = os.getenv("CALENDAR_ID")
KEY_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "creds/service-account.json")
EVENT_CHANNEL_ID = int(os.getenv("EVENT_CHANNEL_ID", "0"))  # optional, restrict posting

if not CAL_ID:
    raise RuntimeError("CALENDAR_ID env var is required.")

if not os.path.exists(KEY_PATH):
    raise RuntimeError(f"Service account key not found at {KEY_PATH}. Set GOOGLE_APPLICATION_CREDENTIALS or upload the file.")

creds = Credentials.from_service_account_file(
    KEY_PATH, scopes=["https://www.googleapis.com/auth/calendar"]
)
_gcal = build("calendar", "v3", credentials=creds)

DB_PATH = os.getenv("DB_PATH", "data/bot.db")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------------------------
# Utilities
# ---------------------------
async def db_exec(query: str, params: tuple = ()):  # simple helper
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(query, params)
        await db.commit()

async def db_fetchone(query: str, params: tuple = ()): 
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(query, params)
        row = await cur.fetchone()
        await cur.close()
        return row

async def db_fetchall(query: str, params: tuple = ()): 
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(query, params)
        rows = await cur.fetchall()
        await cur.close()
        return rows

async def ensure_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    # Base tables
    await db_exec("""
        CREATE TABLE IF NOT EXISTS events_map (
            discord_message_id INTEGER PRIMARY KEY,
            event_id TEXT NOT NULL,
            channel_id INTEGER NOT NULL,
            thread_id INTEGER
        )
    """)
    await db_exec("""
        CREATE TABLE IF NOT EXISTS rsvps (
            event_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            PRIMARY KEY (event_id, user_id)
        )
    """)
    await db_exec("""
        CREATE TABLE IF NOT EXISTS event_tags (
            event_id TEXT NOT NULL,
            tag TEXT NOT NULL,
            PRIMARY KEY (event_id, tag)
        )
    """)

    # If events_map already existed from the first version, add thread_id if missing
    try:
        cols = [r[1] for r in await db_fetchall("PRAGMA table_info(events_map)")]
        if "thread_id" not in cols:
            await db_exec("ALTER TABLE events_map ADD COLUMN thread_id INTEGER")
    except Exception:
        pass

        """
    )

# Google helpers run off-thread to avoid blocking the event loop
async def gcal_insert_event(body: dict):
    return await asyncio.to_thread(lambda: _gcal.events().insert(calendarId=CAL_ID, body=body).execute())

async def gcal_list(time_min_iso: str, time_max_iso: str, max_items: int = 25):
    return await asyncio.to_thread(
        lambda: _gcal.events()
        .list(calendarId=CAL_ID, timeMin=time_min_iso, timeMax=time_max_iso, singleEvents=True, orderBy="startTime", maxResults=max_items)
        .execute()
    )

# Time helpers

def to_local_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = TZ.localize(dt)
    else:
        dt = dt.astimezone(TZ)
    return dt.isoformat()

def display_dt(dt_iso: str | None) -> str:
    if not dt_iso:
        return ""
    dt = du_parser.isoparse(dt_iso)
    local = dt.astimezone(TZ)
    return local.strftime("%a, %b %d at %I:%M %p %Z")

# ---------------------------
# Interest tags, modes, and colors
# ---------------------------
INTEREST_TAGS = [
    "In person Meet up", "Games", "Online", "Yard sale",
    "Family Friendly", "Pop up market", "Last Minute meetup",
    "Fitness", "21+"
]

MODE_CHOICES = ["In person Meet up", "Online"]

# Map topic tags to Google Calendar colorId (string IDs from /event_colors)
TAG_TO_COLOR = {
    "Games": "3",
    "Yard sale": "10",
    "Pop up market": "1",
    "Fitness": "2",
    "Family Friendly": "2",
    "21+": "11",
    "Last Minute meetup": "4",
}

def norm_tag(t: str) -> str:
    return t.strip()

def pick_color_id(tags: list[str]) -> str | None:
    for t in tags:
        t = norm_tag(t)
        if t in TAG_TO_COLOR:
            return TAG_TO_COLOR[t]
    return None


# ---------------------------
# RSVP buttons
# ---------------------------
class RSVPView(discord.ui.View):
    def __init__(self, event_id: str):
        super().__init__(timeout=None)
        self.event_id = event_id

    async def _set_status(self, interaction: discord.Interaction, status: str):
        await db_exec(
            "INSERT OR REPLACE INTO rsvps(event_id, user_id, status) VALUES(?,?,?)",
            (self.event_id, interaction.user.id, status),
        )
        # Try to add the user to the event's private thread
        try:
            row = await db_fetchone("SELECT thread_id FROM events_map WHERE event_id=?", (self.event_id,))
            if row and row[0]:
                thread = interaction.client.get_channel(row[0]) or await interaction.client.fetch_channel(row[0])
                await thread.add_user(interaction.user)
        except Exception:
            pass

        await interaction.response.send_message(f"Your RSVP is **{status}**", ephemeral=True)

    @discord.ui.button(label="Going", style=discord.ButtonStyle.success, emoji="✅")
    async def going(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set_status(interaction, "going")

    @discord.ui.button(label="Maybe", style=discord.ButtonStyle.primary, emoji="❔")
    async def maybe(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set_status(interaction, "maybe")

    @discord.ui.button(label="Not Going", style=discord.ButtonStyle.danger, emoji="❌")
    async def notgoing(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set_status(interaction, "not going")


# ---------------------------
# Embeds
# ---------------------------
async def post_event_embed(channel: discord.TextChannel, event: dict):
    start_iso = event["start"].get("dateTime") or event["start"].get("date")
    end_iso = event["end"].get("dateTime") or event["end"].get("date")

    title = event.get("summary", "Event")
    desc = event.get("description") or ""
    url = event.get("htmlLink")

    embed = discord.Embed(title=title, description=desc)
    if start_iso:
        embed.add_field(name="Starts", value=display_dt(start_iso), inline=True)
    if end_iso:
        embed.add_field(name="Ends", value=display_dt(end_iso), inline=True)
    if url:
        embed.add_field(name="Calendar", value=url, inline=False)

    view = RSVPView(event_id=event["id"])
    msg = await channel.send(embed=embed, view=view)

    await db_exec(
        "INSERT OR REPLACE INTO events_map(discord_message_id, event_id, channel_id) VALUES(?,?,?)",
        (msg.id, event["id"], channel.id),
    )
    try:
        thread = await msg.create_thread(name=f"{title} chat")
        if desc:
            await thread.send("Event details:\n" + desc)
    except Exception:
        pass
    return msg

# ---------------------------
# Slash commands
# ---------------------------
@bot.tree.command(description="Create a calendar event and post it here")
@app_commands.describe(
    title="Event title",
    date="Start date, YYYY-MM-DD (local)",
    start_time="Start time, HH:MM 24h (local)",
    duration_minutes="Duration in minutes",
    location="Where is it?",
    details="Description"
)
async def event_create(
    interaction: discord.Interaction,
    title: str,
    date: str,
    start_time: str,
    duration_minutes: app_commands.Range[int, 5, 10080] = 60,
    location: str = "",
    details: str = "",
):
    if EVENT_CHANNEL_ID and interaction.channel_id != EVENT_CHANNEL_ID:
        return await interaction.response.send_message("Please use the designated events channel.", ephemeral=True)

    # Parse local time
    try:
        start_local = datetime.strptime(f"{date} {start_time}", "%Y-%m-%d %H:%M")
        start_local = TZ.localize(start_local)
        end_local = start_local + timedelta(minutes=duration_minutes)
    except Exception as e:
        return await interaction.response.send_message(f"Time parse error: {e}", ephemeral=True)

    body = {
        "summary": title,
        "location": location,
        "description": details,
        "start": {"dateTime": start_local.isoformat()},
        "end": {"dateTime": end_local.isoformat()},
    }

    await interaction.response.send_message("Creating event...", ephemeral=True)
    try:
        event = await gcal_insert_event(body)
        await post_event_embed(interaction.channel, event)
        await interaction.followup.send(f"Created **{title}**", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Calendar error: {e}", ephemeral=True)

@bot.tree.command(description="List upcoming events from the shared calendar")
@app_commands.describe(days="How many days ahead to list")
async def event_list(interaction: discord.Interaction, days: app_commands.Range[int, 1, 60] = 14):
    now = datetime.now(TZ)
    time_min = now.isoformat()
    time_max = (now + timedelta(days=days)).isoformat()

    try:
        resp = await gcal_list(time_min, time_max, max_items=25)
        items = resp.get("items", [])
        if not items:
            return await interaction.response.send_message("No upcoming events.", ephemeral=True)

        lines = []
        for ev in items:
            start_iso = ev["start"].get("dateTime") or ev["start"].get("date")
            when = display_dt(start_iso) if start_iso else ""
            url = ev.get("htmlLink")
            lines.append(f"• **{ev.get('summary','Event')}** — {when}  <{url}>")
        await interaction.response.send_message("\n".join(lines[:15]), ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Calendar error: {e}", ephemeral=True)

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

# ---------------------------
# Reminders loop (optional, simple)
# ---------------------------
@tasks.loop(minutes=1)
async def reminders():
    try:
        now = datetime.now(TZ)
        soon = now + timedelta(minutes=61)
        resp = await gcal_list(now.isoformat(), soon.isoformat(), max_items=50)
        items = resp.get("items", [])
        for ev in items:
            # Example: you could post a 60-minute reminder into the thread or channel.
            # This is a stub. Implement dedupe tracking in DB before enabling.
            pass
    except Exception:
        pass

@reminders.before_loop
async def before_reminders():
    await bot.wait_until_ready()

# ---------------------------
# Startup
# ---------------------------
@bot.event
async def on_ready():
    await ensure_db()
    try:
        await bot.tree.sync()
    except Exception:
        pass
    print(f"Logged in as {bot.user} | TZ={TZ_NAME} | Calendar={CAL_ID}")
    if not reminders.is_running():
        reminders.start()

if __name__ == "__main__":
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN is required in environment or .env")
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    bot.run(token)
