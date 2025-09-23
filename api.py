# api.py
import os
import time
from typing import Dict, List, Tuple, Optional, Set

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

import gspread
from google.oauth2.service_account import Credentials

# ---- CONFIG (env or edit inline) ----
SERVICE_FILE = os.getenv("GOOGLE_SERVICE_FILE", "creds/service-account.json")
SPREADSHEET_ID = os.getenv("ROLES_SHEET_ID", "1HR_D1a1h6Y7n0t-_8gYVNKZoeJxQZG5aYBXqC8JC1jc")
ROLES_TAB = os.getenv("ROLES_TAB", "Permission Roles")
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

# Columns expected in ROLES_TAB
COL_ROLE_NAME = "role name"
COL_ROLE_TYPE = "role type"
COL_ROLE_DESC = "role description"

# ---- Google Sheets client ----
_creds = Credentials.from_service_account_file(SERVICE_FILE, scopes=SCOPES)
_gc = gspread.authorize(_creds)

def _open_ws():
    sh = _gc.open_by_key(SPREADSHEET_ID)
    return sh.worksheet(ROLES_TAB)

# ---- Simple cache ----
CACHE_TTL = int(os.getenv("CACHE_TTL", "60"))  # seconds
_cache = {"at": 0.0, "rows": []}

def _sheet_values() -> List[List[str]]:
    now = time.time()
    if now - _cache["at"] > CACHE_TTL or not _cache["rows"]:
        ws = _open_ws()
        _cache["rows"] = ws.get_all_values()
        _cache["at"] = now
    return _cache["rows"]

# ---- Parsers ----
def _parse_roles(allowed: Optional[Set[str]]) -> Dict[str, List[Tuple[str, str]]]:
    """Return {role_type: [(name, desc), ...]} from the sheet."""
    vals = _sheet_values()
    if not vals:
        return {}

    hdr = [str(h).strip().lower() for h in vals[0]]
    try:
        i_name = hdr.index(COL_ROLE_NAME)
        i_type = hdr.index(COL_ROLE_TYPE)
    except ValueError:
        return {}

    i_desc = hdr.index(COL_ROLE_DESC) if COL_ROLE_DESC in hdr else None

    out: Dict[str, List[Tuple[str, str]]] = {}
    for r in vals[1:]:
        if len(r) <= max(i_name, i_type, (i_desc or 0)):
            continue
        t = str(r[i_type]).strip().lower()
        if allowed and t not in allowed:
            continue
        name = str(r[i_name]).strip()
        if not name:
            continue
        desc = str(r[i_desc]).strip() if i_desc is not None and i_desc < len(r) else ""
        out.setdefault(t, []).append((name, desc))
    return out

def _flatten(types: Set[str]) -> List[Tuple[str, str]]:
    bucketed = _parse_roles(types)
    flat: List[Tuple[str, str]] = []
    for t in types:
        flat.extend(bucketed.get(t, []))
    return flat

def _all_types() -> List[str]:
    bucketed = _parse_roles(None)
    return sorted(bucketed.keys())

# ---- FastAPI ----
app = FastAPI(title="Roles API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/role-types")
async def role_types():
    return _all_types()

@app.get("/roles")
async def roles(types: List[str] = Query(default=[])):
    tset = {t.strip().lower() for t in types} if types else set(_all_types())
    pairs = _flatten(tset)
    # return as objects for easier front-end use
    return [{"name": n, "desc": d} for n, d in pairs]

@app.get("/roles/grouped")
async def roles_grouped(types: List[str] = Query(default=[])):
    tset = {t.strip().lower() for t in types} if types else None
    data = _parse_roles(tset)
    # shape: {type: [{name, desc}]}
    return {t: [{"name": n, "desc": d} for n, d in pairs] for t, pairs in data.items()}

