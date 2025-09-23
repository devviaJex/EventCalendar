# shared.py

# --- env + google clients (matches your .env names) ---
import os, asyncio
import aiosqlite
from pathlib import Path
import pytz
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from dateutil import parser as du_parser

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

TZ_NAME = os.getenv("TZ", "America/Chicago")
TZ = pytz.timezone(TZ_NAME)

CAL_ID = os.getenv("CALENDAR_ID")
KEY_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "creds/service-account.json")
KEY_PATH = str((ROOT / KEY_PATH).resolve())

EVENT_CHANNEL_ID = int(os.getenv("EVENT_CHANNEL_ID", "0"))
CREATE_FROM_CHANNEL_ID = int(os.getenv("CREATE_FROM_CHANNEL_ID", "0"))
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "data/bot.db")

ROLES_SHEET = os.getenv("ROLES_SHEET_ID")
ROLES_TAB   = os.getenv("ROLES_TAB_NAME", "Roles")
MEMBERS_TAB = os.getenv("MEMBER_TAB_NAME", "Members")
RULES_SHEET = os.getenv("RULES_SHEET_ID")
EVENT_SHEET = os.getenv("EVENT_SHEET_ID")
EVENT_TAB   = os.getenv("EVENT_TAB_NAME", "Events")

if not CAL_ID:
    raise RuntimeError("CALENDAR_ID is required")
if not os.path.isfile(KEY_PATH):
    raise RuntimeError(f"Service account key not found: {KEY_PATH}")

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly"
]
creds = Credentials.from_service_account_file(KEY_PATH, scopes=SCOPES)
_GCAL = build("calendar", "v3", credentials=creds)
_SHEETS = build("sheets", "v4", credentials=creds)

# --- DB helpers ---

async def db_exec(query: str, params: tuple = ()):
    d = os.path.dirname(DB_PATH)
    if d: os.makedirs(d, exist_ok=True)
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
    await db_exec("""
        CREATE TABLE IF NOT EXISTS events_map(
          discord_message_id INTEGER PRIMARY KEY,
          event_id TEXT NOT NULL,
          channel_id INTEGER NOT NULL,
          thread_id INTEGER
        )""")
    await db_exec("""
        CREATE TABLE IF NOT EXISTS rsvps(
          event_id TEXT NOT NULL,
          user_id INTEGER NOT NULL,
          status TEXT NOT NULL,
          PRIMARY KEY (event_id, user_id)
        )""")
    await db_exec("""
        CREATE TABLE IF NOT EXISTS event_tags(
          event_id TEXT NOT NULL,
          tag TEXT NOT NULL,
          PRIMARY KEY (event_id, tag)
        )""")


# optional gspread helpers if you installed gspread
try:
    import gspread
    def get_sheets_client():
        return gspread.authorize(creds)
    def open_ws(sheet_id: str, tab: str | None = None):
        sh = get_sheets_client().open_by_key(sheet_id)
        return sh.worksheet(tab) if tab else sh.sheet1
except Exception:
    pass

def display_dt(dt_iso: str | None) -> str:
    if not dt_iso:
        return ""
    dt = du_parser.isoparse(dt_iso)
    local = dt.astimezone(TZ)
    return local.strftime("%a, %b %d at %I:%M %p %Z")

# --- Sheets helpers (use your tab names) ---
def _a1(tab: str, rng: str) -> str:
    safe = (tab or "").replace("'", "''")
    return f"'{safe}'!{rng}" if tab else rng

async def sheet_values(sheet_id: str, tab: str, rng: str) -> list[list[str]]:
    a1 = _a1(tab, rng)
    data = await asyncio.to_thread(
        lambda: _SHEETS.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=a1
        ).execute()
    )
    return data.get("values", [])

def _rows_to_dicts(values: list[list[str]]) -> list[dict]:
    if not values: return []
    hdr = [h.strip() for h in values[0]]
    out = []
    for row in values[1:]:
        row = row + [""] * (len(hdr) - len(row))
        out.append(dict(zip(hdr, row)))
    return out

# roles: filter Role Type == 'interest'
async def list_interest_roles() -> list[str]:
    if not ROLES_SHEET or not ROLES_TAB:
        return []
    vals = await sheet_values(ROLES_SHEET, ROLES_TAB, "A:C")
    if not vals: return []
    hdr = [h.strip().lower() for h in vals[0]]
    try:
        i_role = hdr.index("role name"); i_type = hdr.index("role type")
    except ValueError:
        return []
    out = []
    for r in vals[1:]:
        if len(r) <= max(i_role, i_type): continue
        if str(r[i_type]).strip().lower() == "interest":
            name = str(r[i_role]).strip()
            if name: out.append(name)
    return out

# members tab (same spreadsheet)
async def list_members() -> list[dict]:
    if not ROLES_SHEET or not MEMBERS_TAB:
        return []
    vals = await sheet_values(ROLES_SHEET, MEMBERS_TAB, "A:Z")
    return _rows_to_dicts(vals)

def norm_tag(t: str) -> str:
    return t.strip()
# stricter:
# def norm_tag(t: str) -> str:
#     return " ".join(t.split()).strip().lower()

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





