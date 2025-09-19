# shared.py
import os
import asyncio
import aiosqlite
from dateutil import parser as du_parser
import pytz
from dotenv import load_dotenv
import gspread

# Google APIs
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

load_dotenv()

# ---- Env ----
TZ_NAME = os.getenv("TZ", "America/Chicago")
TZ = pytz.timezone(TZ_NAME)
CAL_ID = os.getenv("CALENDAR_ID")
KEY_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "creds/service-account.json")
EVENT_CHANNEL_ID = int(os.getenv("EVENT_CHANNEL_ID", "0"))
CREATE_FROM_CHANNEL_ID = int(os.getenv("CREATE_FROM_CHANNEL_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "data/bot.db")
ROLES_SHEET = os.getenv("ROLES_SHEET_ID")  # Google Sheet ID for roles
RULES_SHEET = os.getenv("RULES_SHEET_ID")  # Google Sheet ID for rules
MEMBERS_TAB = os.getenv("ROLES_SHEET_TAB", "Members")  # Google Tab ID for members
ROLES_TAB = os.getenv("ROLES_SHEET_TAB", "Permission_Roles")  # Tab name in roles sheet

if not CAL_ID:
    raise RuntimeError("CALENDAR_ID env var is required.")
if not os.path.exists(KEY_PATH):
    raise RuntimeError("Service account key not found at GOOGLE_APPLICATION_CREDENTIALS path.")

# ---- Google clients ----
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]
creds = Credentials.from_service_account_file(KEY_PATH, scopes=SCOPES)

_GCAL = build("calendar", "v3", credentials=creds)
_SHEETS = build("sheets", "v4", credentials=creds)

def get_sheets_client():
    """High-level gspread client, if you want it."""
    return gspread.authorize(creds)

def open_ws(spreadsheet_id: str, tab: str):
    gc = get_sheets_client()
    sh = gc.open_by_key(spreadsheet_id)
    return sh.worksheet(tab)  # returns gspread.Worksheet


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
    await db_exec(
        """
        CREATE TABLE IF NOT EXISTS reminder_log(
            event_id TEXT NOT NULL,
            tag TEXT NOT NULL,
            notified_at TEXT,
            PRIMARY KEY (event_id, tag)
        )
        """
    )

# ---- Google Calendar helpers ----
async def gcal_insert_event(body: dict):
    return await asyncio.to_thread(lambda: _GCAL.events().insert(calendarId=CAL_ID, body=body).execute())

async def gcal_list(time_min_iso: str, time_max_iso: str, max_items: int = 25):
    return await asyncio.to_thread(
        lambda: _GCAL.events().list(
            calendarId=CAL_ID,
            timeMin=time_min_iso,
            timeMax=time_max_iso,
            singleEvents=True,
            orderBy="startTime",
            maxResults=max_items,
        ).execute()
    )

# ---- Time/format helpers ----
def display_dt(dt_iso: str | None) -> str:
    if not dt_iso:
        return ""
    dt = du_parser.isoparse(dt_iso)
    local = dt.astimezone(TZ)
    return local.strftime("%a, %b %d at %I:%M %p %Z")

def norm_tag(t: str) -> str:
    return t.strip()

# ---- Sheets helpers ----
async def list_interest_roles(range_name: str = "Permission_Roles!A:C") -> list[str]:
    """
    Return a list of role names where Role Type == 'interest'.
    Headers expected: A='Role', B='Role Type', C optional.
    """
    if not ROLES_SHEET:
        return []

    def _fetch():
        return _SHEETS.spreadsheets().values().get(
            spreadsheetId=ROLES_SHEET, range=range_name
        ).execute()

    data = await asyncio.to_thread(_fetch)
    values = data.get("values", [])
    if not values:
        return []

    headers = [h.strip().lower() for h in values[0]]
    try:
        role_i = headers.index("role")
        type_i = headers.index("role type")
    except ValueError:
        return []

    out: list[str] = []
    for row in values[1:]:
        if len(row) <= max(role_i, type_i):
            continue
        if str(row[type_i]).strip().lower() != "interest":
            continue
        role = str(row[role_i]).strip()
        if role:
            out.append(role)
    return out
