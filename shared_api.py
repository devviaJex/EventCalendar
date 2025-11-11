# shared_api.py
from __future__ import annotations
import os, aiohttp
from typing import Any, Dict, Optional

# Env:
#   GGW_API_BASE = https://gibsongatorwatch.com   (no /api)
#   GGW_API_PREFIX = /api                         (defaults to /api)
#   GGW_API_KEY = <token>                         (same value server checks)

API_ORIGIN = os.getenv("GGW_API_BASE", "https://gibsongatorwatch.com").rstrip("/")
API_PREFIX = os.getenv("GGW_API_PREFIX", "/api").strip() or "/api"
if not API_PREFIX.startswith("/"):
    API_PREFIX = "/" + API_PREFIX

API_KEY = os.getenv("GGW_API_KEY", "")

def _build_url(path: str) -> str:
    # allow absolute URLs too
    if path.startswith("http://") or path.startswith("https://"):
        return path
    path = path.lstrip("/")
    return f"{API_ORIGIN}{API_PREFIX}/{path}"

def _headers(extra: Optional[Dict[str,str]]=None) -> Dict[str,str]:
    h = {"Content-Type": "application/json"}
    if API_KEY:
        # server expects X-API-Key; keep Authorization if you later add OAuth
        h["X-API-Key"] = API_KEY
    if extra:
        h.update(extra)
    return h

async def api_call(method: str, path: str, *, json: Any=None, params: Dict[str,Any]|None=None) -> Optional[Dict[str,Any]]:
    url = _build_url(path)
    async with aiohttp.ClientSession() as s:
        async with s.request(method.upper(), url, json=json, params=params, headers=_headers()) as r:
            txt = await r.text()
            # Return parsed JSON on 2xx, else None
            if 200 <= r.status < 300:
                try:
                    return await r.json()
                except Exception:
                    return {"ok": True, "raw": txt}
            # surface brief error in logs
            print("API", method, url, "->", r.status, txt[:300])
            return None

# convenience
async def api_get(path: str, *, params: Dict[str,Any]|None=None):   return await api_call("GET", path, params=params)
async def api_post(path: str, json: Any):                            return await api_call("POST", path, json=json)
async def api_patch(path: str, json: Any):                           return await api_call("PATCH", path, json=json)
async def api_delete(path: str):                                     return await api_call("DELETE", path)

