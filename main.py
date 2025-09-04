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
DB_PATH = os.getenv("DB_PATH", "data/bot.db")

if not CAL_ID:
    raise RuntimeError("CALENDAR_ID env var is required.")

if not os.path.exists(KEY_PATH):
    raise RuntimeError(
        f"Service account key not found at {KEY_PATH}. Set GOOGLE_APPLICATION_CREDENTIALS or upload the file."
    )

creds = Credentials.from_service_account_file(
    KEY_PATH, scopes=["https://www.googleapis.com/auth/calendar"]
)
_gcal = build("calendar", "v3", credentials=creds)

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------------------------
# DB helpers
# ---------------------------
async def db_exec(query: str, params: tuple = ()):  # simple helper
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
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
    await db_exec(
        """
        CREATE TABLE IF NOT EXISTS events_map (
            discord_message_id INTEGER PRIMARY KEY,
            event_id TEXT NOT NULL,
            channel_id INTEGER NOT NULL,
            thread_id INTEGER
        )
        """
    )
    await db_exec(
        """
        CREATE TABLE IF NOT EXISTS rsvps (
            event_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            PRIMARY KEY (event_id, user_id)
        )
        """
    )
    await db_exec(
        """
        CREATE TABLE IF NOT EXISTS event_tags (
            event_id TEXT NOT NULL,
            tag TEXT NOT NULL,
            PRIMARY KEY (event_id, tag)
        )
        """
    )
    # Ensure thread_id exists if table was created earlier without it
    try:
        cols = [r[1] for r in await db_fetchall("PRAGMA table_info(events_map)")]
        if "thread_id" not in cols:
            await db_exec("ALTER TABLE events_map ADD COLUMN thread_id INTEGER")
    except Exception:
        pass

# Google helpers run off-thread to avoid blocking the event loop
async def gcal_insert_event(body: dict):
    return await asyncio.to_thread(
        lambda: _gcal.events().insert(calendarId=CAL_ID, body=body).execute()
    )

async def gcal_list(time_min_iso: str, time_max_iso: str, max_items: int = 25):
    return await asyncio.to_thread(
        lambda: _gcal.events()
        .list(
            calendarId=CAL_ID,
            timeMin=time_min_iso,
            timeMax=time_max_iso,
            singleEvents=True,
            orderBy="startTime",
            maxResults=max_items,
        )
        .execute()
    )

# ---------------------------
# Time helpers
# ---------------------------

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
    "In person Meet up",
    "Games",
    "Online",
    "Yard sale",
    "Family Friendly",
    "Pop up market",
    "Last Minute meetup",
    "Fitness",
    "21+",
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
# RSVP buttons (auto-add to thread)
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
        # Auto-add RSVP users to the event's private thread (if exists)
        try:
            row = await db_fetchone(
                "SELECT thread_id FROM events_map WHERE event_id=?",
                (self.event_id,),
            )
            if row and row[0]:
                thread = interaction.client.get_channel(row[0]) or await interaction.client.fetch_channel(row[0])
                await thread.add_user(interaction.user)
        except Exception:
            pass

        await interaction.response.send_message(
            f"Your RSVP is **{status}**", ephemeral=True
        )

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
# Embeds and thread creation
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

    # Create a private thread for event discussion
    thread = None
    try:
        thread = await msg.create_thread(
            name=f"{title} chat",
            type=discord.ChannelType.private_thread,
            auto_archive_duration=10080,  # 7 days
        )
        if desc:
            await thread.send("Event details:\n" + desc)
    except Exception:
        pass

    await db_exec(
        "INSERT OR REPLACE INTO events_map(discord_message_id, event_id, channel_id, thread_id) VALUES(?,?,?,?)",
        (msg.id, event["id"], channel.id, thread.id if thread else None),
    )

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
    details="Description",
    tags="Comma-separated topic tags (e.g., Games, Fitness)",
    mode="Event mode"
)
@app_commands.choices(
    mode=[app_commands.Choice(name=m, value=m) for m in MODE_CHOICES]
)
async def event_create(
    interaction: discord.Interaction,
    title: str,
    date: str,
    start_time: str,
    duration_minutes: app_commands.Range[int, 5, 10080] = 60,
    location: str = "",
    details: str = "",
    tags: str = "",
    mode: app_commands.Choice[str] | None = None,
):
    if EVENT_CHANNEL_ID and interaction.channel_id != EVENT_CHANNEL_ID:
        return await interaction.response.send_message(
            "Please use the designated events channel.", ephemeral=True
        )

    try:
        start_local = TZ.localize(
            datetime.strptime(f"{date} {start_time}", "%Y-%m-%d %H:%M")
        )
        end_local = start_local + timedelta(minutes=duration_minutes)
    except Exception as e:
        return await interaction.response.send_message(
            f"Time parse error: {e}", ephemeral=True
        )

    tag_list = [norm_tag(t) for t in tags.split(",") if t.strip()] if tags else []
    # include mode as a tag for role pinging
    if mode and mode.value not in tag_list:
        tag_list.insert(0, mode.value)

    color_id = pick_color_id(tag_list)

    body = {
        "summary": title,
        "location": location,
        "description": details,
        "start": {"dateTime": start_local.isoformat()},
        "end": {"dateTime": end_local.isoformat()},
    }
    if color_id:
        body["colorId"] = color_id

    await interaction.response.send_message("Creating event...", ephemeral=True)
    try:
        event = await gcal_insert_event(body)

        # persist tags
        for t in tag_list:
            await db_exec(
                "INSERT OR REPLACE INTO event_tags(event_id, tag) VALUES(?,?)",
                (event["id"], t),
            )

        # embed + private thread
        await post_event_embed(interaction.channel, event)

        # ping one mode role (if any) + first matching topic role (to avoid spam)
        roles_to_ping = []
        if mode:
            r = discord.utils.get(interaction.guild.roles, name=mode.value)
            if r:
                roles_to_ping.append(r)
        for t in tag_list:
            if mode and t == mode.value:
                continue
            r = discord.utils.get(interaction.guild.roles, name=t)
            if r:
                roles_to_ping.append(r)
                break

        if roles_to_ping:
            allowed = discord.AllowedMentions(roles=True)
            await interaction.channel.send(
                "New event for " + " ".join(r.mention for r in roles_to_ping),
                allowed_mentions=allowed,
            )

        await interaction.followup.send(f"Created **{title}**", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Calendar error: {e}", ephemeral=True)

@bot.tree.command(description="List upcoming events from the shared calendar")
@app_commands.describe(days="How many days ahead to list")
async def event_list(
    interaction: discord.Interaction, days: app_commands.Range[int, 1, 60] = 14
):
    now = datetime.now(TZ)
    time_min = now.isoformat()
    time_max = (now + timedelta(days=days)).isoformat()

    try:
        resp = await gcal_list(time_min, time_max, max_items=25)
        items = resp.get("items", [])
        if not items:
            return await interaction.response.send_message(
                "No upcoming events.", ephemeral=True
            )

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
    await interaction.response.send_message(
        f"Running on **{platform}**.", ephemeral=True
    )

# ---------------------------
# Subscription management & utilities
# ---------------------------
@bot.tree.command(description="Subscribe to an event interest tag")
@app_commands.describe(tag="Pick a tag to subscribe to")
@app_commands.choices(tag=[app_commands.Choice(name=t, value=t) for t in INTEREST_TAGS])
async def notify_subscribe(
    interaction: discord.Interaction, tag: app_commands.Choice[str]
):
    role = discord.utils.get(interaction.guild.roles, name=tag.value)
    if not role:
        try:
            role = await interaction.guild.create_role(
                name=tag.value, mentionable=False, reason="Interest role"
            )
        except Exception as e:
            return await interaction.response.send_message(
                f"Could not create role: {e}", ephemeral=True
            )
    try:
        await interaction.user.add_roles(role, reason="Interest subscribe")
        await interaction.response.send_message(
            f"Subscribed to **{tag.value}**", ephemeral=True
        )
    except Exception as e:
        await interaction.response.send_message(
            f"Could not add role: {e}", ephemeral=True
        )


@bot.tree.command(description="Unsubscribe from an event interest tag")
@app_commands.describe(tag="Pick a tag to leave")
@app_commands.choices(tag=[app_commands.Choice(name=t, value=t) for t in INTEREST_TAGS])
async def notify_unsubscribe(
    interaction: discord.Interaction, tag: app_commands.Choice[str]
):
    role = discord.utils.get(interaction.guild.roles, name=tag.value)
    if not role:
        return await interaction.response.send_message(
            "You are not subscribed.", ephemeral=True
        )
    try:
        await interaction.user.remove_roles(role, reason="Interest unsubscribe")
        await interaction.response.send_message(
            f"Unsubscribed from **{tag.value}**", ephemeral=True
        )
    except Exception as e:
        await interaction.response.send_message(
            f"Could not remove role: {e}", ephemeral=True
        )


@bot.tree.command(description="Show this channel's ID")
async def channelid(interaction: discord.Interaction):
    ch = interaction.channel
    gid = interaction.guild_id
    await interaction.response.send_message(
        f"Guild ID: `{gid}`\nChannel ID: `{ch.id}`\nName: {ch.name}",
        ephemeral=True,
    )


@bot.tree.command(description="Show Google Calendar event colors")
async def event_colors(interaction: discord.Interaction):
    pal = await asyncio.to_thread(lambda: _gcal.colors().get().execute())
    lines = []
    for cid, spec in pal.get("event", {}).items():
        lines.append(f"{cid}: {spec['background']} on {spec['foreground']}")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)

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
        for _ev in items:
            # Implement dedupe + reminder posting if you want.
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
