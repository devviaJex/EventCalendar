import os
import asyncio
import aiosqlite
from datetime import datetime
from dateutil import parser as du_parser
import pytz
from dotenv import load_dotenv

# Google
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

load_dotenv()

TZ_NAME = os.getenv("TZ", "America/Chicago")
TZ = pytz.timezone(TZ_NAME)
CAL_ID = os.getenv("CALENDAR_ID")
KEY_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "creds/service-account.json")
EVENT_CHANNEL_ID = int(os.getenv("EVENT_CHANNEL_ID", "0"))
CREATE_FROM_CHANNEL_ID = int(os.getenv("CREATE_FROM_CHANNEL_ID", "1412901135357968405"))
DB_PATH = os.getenv("DB_PATH", "data/bot.db")
SHEET_ID = os.getenv("ROLES_SHEET_ID")
EMBED_COLOR = 0x2E8B57  # swamp green

# sanity check
if not CAL_ID:
    raise RuntimeError("CALENDAR_ID env var is required.")
if not os.path.exists(KEY_PATH):
    raise RuntimeError(f"Service account key not found at {KEY_PATH}")

creds = Credentials.from_service_account_file(
    KEY_PATH, scopes=["https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/spreadsheets.readonly",]
)
_GCAL = build("calendar", "v3", credentials=creds)
_SHEETS = build("sheets", "v4", credentials=creds)
async def list_interest_roles(range_name: str = "roles!A:B") -> list[str]:
    """Return names from the sheet where Role Type == 'Interest'.
       Assumes headers: column A='Role', column B='Role Type'."""
    def _fetch():
        return (
            _SHEETS.spreadsheets()
            .values()
            .get(spreadsheetId=SHEET_ID, range=range_name)
            .execute()
        )
    if not SHEET_ID:
        return []
    data = await asyncio.to_thread(_fetch)
    values = data.get("values", [])
    if not values:
        return []
    # header row expected
    headers = [h.strip().lower() for h in values[0]]
    try:
        role_i = headers.index("role")
        type_i = headers.index("role type")
    except ValueError:
        return []
    out = []
    for row in values[1:]:
        if len(row) <= max(role_i, type_i):
            continue
        if str(row[type_i]).strip().lower() == "interest":
            out.append(str(row[role_i]).strip())
    return out
# ---- DB helpers ----
async def db_exec(query: str, params: tuple = ()):
    dirn = os.path.dirname(DB_PATH)
    if dirn:
        os.makedirs(dirn, exist_ok=True)
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
    dirn = os.path.dirname(DB_PATH)
    if dirn:
        os.makedirs(dirn, exist_ok=True)
    await db_exec(
        """CREATE TABLE IF NOT EXISTS events_map (
            discord_message_id INTEGER PRIMARY KEY,
            event_id TEXT NOT NULL,
            channel_id INTEGER NOT NULL,
            thread_id INTEGER
        )"""
    )
    await db_exec(
        """CREATE TABLE IF NOT EXISTS rsvps (
            event_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            PRIMARY KEY (event_id, user_id)
        )"""
    )
    await db_exec(
        """CREATE TABLE IF NOT EXISTS event_tags (
            event_id TEXT NOT NULL,
            tag TEXT NOT NULL,
            PRIMARY KEY (event_id, tag)
        )"""
    )
    await db_exec(
        """CREATE TABLE IF NOT EXISTS reminder_log(
            event_id TEXT NOT NULL,
            tag TEXT NOT NULL,
            notified_at TEXT,
            PRIMARY KEY (event_id, tag)
        )"""
    )

# ---- Google helpers ----
async def gcal_insert_event(body: dict):
    return await asyncio.to_thread(
        lambda: _GCAL.events().insert(calendarId=CAL_ID, body=body).execute()
    )

async def gcal_list(time_min_iso: str, time_max_iso: str, max_items: int = 25):
    return await asyncio.to_thread(
        lambda: _GCAL.events()
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

# ---- Time/format helpers ----
def display_dt(dt_iso: str | None) -> str:
    if not dt_iso:
        return ""
    dt = du_parser.isoparse(dt_iso)
    local = dt.astimezone(TZ)
    return local.strftime("%a, %b %d at %I:%M %p %Z")


# ---- Tags & colors ----
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
