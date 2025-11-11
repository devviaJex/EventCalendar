# shared_event_utils.py
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo
import re
import os
import uuid
import aiohttp
import logging
import discord

from shared import TZ_NAME, gcal_insert_event

logger = logging.getLogger("ggw")

API_BASE = os.getenv("GGW_API_BASE", "https://gibsongatorwatch.com/api").rstrip("/")
API_KEY = os.getenv("GGW_API_KEY")  # bearer token


# ---------- HTTP helper (POST to backend) ----------
async def api_create_event(payload: dict) -> dict | None:
    """POST event payload to backend. Returns dict with ok/status/body or error details."""
    if not API_BASE or not API_KEY:
        logger.error("api_create_event missing API_BASE/API_KEY")
        return {"ok": False, "status": None, "error": "missing credentials"}

    url = f"{API_BASE.rstrip('/')}/events"
    headers = {"X-API-Key": API_KEY, "Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=payload, headers=headers) as r:
                text = await r.text()
                try:
                    body = await r.json()
                except Exception:
                    body = text
                if 200 <= r.status < 300:
                    logger.info("api_create_event OK %s -> %s", r.status, body)
                    return {"ok": True, "status": r.status, "body": body}
                logger.error("api_create_event FAILED %s -> %s", r.status, text[:1000])
                return {"ok": False, "status": r.status, "body": body}
    except Exception as e:
        logger.exception("api_create_event exception: %s", e)
        return {"ok": False, "status": None, "error": str(e)}


# ---------- dataclass ----------
@dataclass(frozen=True)
class EventCfg:
    channel_id: int
    modal_title: str
    tag_prompt: str
    require_role: Optional[str] = None


# ---------- helpers ----------
def parse_mdy12(s: str) -> datetime:
    s = re.sub(r"\s*(am|pm)\s*$", lambda m: " " + m.group(1).upper(), s.strip(), flags=re.I)
    return datetime.strptime(s, "%m/%d/%Y %I:%M %p")


def daterange(d0: datetime, d1: datetime):
    cur = d0
    while cur.date() <= d1.date():
        yield cur
        cur += timedelta(days=1)


def resolve_forum_tags(channel: discord.ForumChannel, names: list[str]) -> Tuple[list[discord.ForumTag], list[str]]:
    by_name = {t.name.strip().lower(): t for t in channel.available_tags}
    found, missing = [], []
    for n in names:
        t = by_name.get(n.strip().lower())
        if t:
            found.append(t)
        else:
            missing.append(n)
    return found, missing


# ---------- tag picker ----------
class ForumTagSelect(discord.ui.Select):
    def __init__(self, channel: discord.ForumChannel):
        opts = [discord.SelectOption(label=t.name[:100]) for t in channel.available_tags[:25]] or \
               [discord.SelectOption(label="No tags configured")]
        super().__init__(placeholder="Select tags", min_values=1, max_values=min(5, len(opts)), options=opts)

    async def callback(self, interaction: discord.Interaction):
        view: ForumTagView = self.view  # type: ignore
        view.selected = list(self.values)
        await interaction.response.edit_message(content=f"Chosen tags: {', '.join(view.selected)}", view=view)


class ForumTagView(discord.ui.View):
    def __init__(self, channel: discord.ForumChannel):
        super().__init__(timeout=300)
        self.selected: List[str] = []
        self.add_item(ForumTagSelect(channel))
        ok = discord.ui.Button(label="Confirm", style=discord.ButtonStyle.primary)
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)

        async def _ok(i: discord.Interaction):
            self.stop()
            await i.response.edit_message(view=None)

        async def _cancel(i: discord.Interaction):
            self.selected = []
            self.stop()
            await i.response.edit_message(content="Canceled.", view=None)

        ok.callback = _ok  # type: ignore
        cancel.callback = _cancel  # type: ignore
        self.add_item(ok)
        self.add_item(cancel)


# ---------- shared modal ----------
class EventModal(discord.ui.Modal):
    def __init__(self, cfg: EventCfg):
        super().__init__(title=cfg.modal_title)
        self.cfg = cfg
        self.title_in = discord.ui.TextInput(label="Title", max_length=120, required=True)
        self.start_dt_in = discord.ui.TextInput(label="Start (MM/DD/YYYY h:mm am/pm)", required=True)
        self.end_dt_in = discord.ui.TextInput(label="End (MM/DD/YYYY h:mm am/pm)", required=True)
        self.loc_in = discord.ui.TextInput(label="Location (optional)", required=False)
        self.desc_in = discord.ui.TextInput(label="Details (optional)", style=discord.TextStyle.paragraph, required=False, max_length=1000)
        for x in (self.title_in, self.start_dt_in, self.end_dt_in, self.loc_in, self.desc_in):
            self.add_item(x)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        tz = ZoneInfo(TZ_NAME)
        try:
            start = parse_mdy12(self.start_dt_in.value).replace(tzinfo=tz)
            end = parse_mdy12(self.end_dt_in.value).replace(tzinfo=tz)
        except Exception:
            await interaction.followup.send("Invalid datetime. Use MM/DD/YYYY h:mm am/pm.", ephemeral=True)
            return
        if end <= start:
            await interaction.followup.send("End must be after start.", ephemeral=True)
            return

        # resolve forum channel
        ch = interaction.guild.get_channel(self.cfg.channel_id) if interaction.guild else None
        if ch is None:
            try:
                ch = await interaction.client.fetch_channel(self.cfg.channel_id)
            except Exception:
                await interaction.followup.send("Configured channel not accessible.", ephemeral=True)
                return
        if not isinstance(ch, discord.ForumChannel):
            await interaction.followup.send("Target channel is not a Forum.", ephemeral=True)
            return

        # tag picker
        view = ForumTagView(ch)
        await interaction.followup.send(self.cfg.tag_prompt, view=view, ephemeral=True)
        await view.wait()
        if not view.selected:
            return
        tags, missing = resolve_forum_tags(ch, view.selected)
        if not tags:
            await interaction.followup.send("No valid tags selected.", ephemeral=True)
            return

        sd0 = start.replace(hour=0, minute=0, second=0, microsecond=0)
        ed0 = end.replace(hour=0, minute=0, second=0, microsecond=0)
        cal_links: list[str] = []

        # compose embed (initial - calendar link will be appended later)
        when_lines = [f"{d.strftime('%a %b %d')}  {start.strftime('%H:%M')}-{end.strftime('%H:%M')}" for d in daterange(sd0, ed0)]
        title_prefix = sd0.strftime("%m/%d") if sd0.date() == ed0.date() else f"{sd0.strftime('%m/%d')}-{ed0.strftime('%m/%d')}"
        final_title = f"{title_prefix} {self.title_in.value}"

        embed = discord.Embed(title=final_title, description=self.desc_in.value or "")
        embed.add_field(name="When", value="\n".join(when_lines), inline=False)
        if self.loc_in.value:
            embed.add_field(name="Where", value=self.loc_in.value, inline=False)
        embed.add_field(name="Tags", value=", ".join(view.selected), inline=False)

        # --- Create Discord scheduled event (with perms + time guards) ---
        guild = interaction.guild
        me = guild.me if guild else None
        sched = None
        sched_url = None

        if not guild or not me or not me.guild_permissions.manage_events:
            await interaction.followup.send(
                "Bot lacks 'Manage Events' permission. Enable it to create Discord Events.",
                ephemeral=True,
            )
        else:
            start_utc = start.astimezone(timezone.utc)
            end_utc = end.astimezone(timezone.utc)
            now_utc = datetime.now(timezone.utc)

            if start_utc <= now_utc:
                start_utc = now_utc.replace(second=0, microsecond=0) + timedelta(minutes=2)
                if end_utc <= start_utc:
                    end_utc = start_utc + timedelta(hours=1)

            try:
                metadata = discord.ScheduledEventEntityMetadata(location=(self.loc_in.value or "TBA")[:100])
                sched = await guild.create_scheduled_event(
                    name=self.title_in.value[:100],
                    start_time=start_utc,
                    end_time=end_utc,
                    entity_type=discord.EntityType.external,
                    entity_metadata=metadata,
                    privacy_level=discord.PrivacyLevel.guild_only,
                    description=(self.desc_in.value or "")[:1000],
                )
                sched_url = f"https://discord.com/events/{guild.id}/{sched.id}"
            except discord.Forbidden as e:
                await interaction.followup.send("Bot permission denied creating Discord event. Check Manage Events.", ephemeral=True)
                logger.exception("Forbidden creating scheduled event: %s", e)
            except Exception as e:
                await interaction.followup.send(f"Scheduled Event create failed: {e}", ephemeral=True)
                logger.exception("Failed creating scheduled event: %s", e)

        if sched_url:
            embed.add_field(name="Discord Event", value=f"[Open]({sched_url})", inline=False)

        # --- Create forum thread (ThreadWithMessage safe) ---
        created = await ch.create_thread(
            name=final_title,
            embed=embed,
            applied_tags=tags[:5],
        )
        thread = created.thread if hasattr(created, "thread") else created
        starter_msg = getattr(created, "message", None)

        # create calendar events + POST to backend
        series_id = uuid.uuid4().hex
        manage_token = uuid.uuid4().hex

        sd0 = start.replace(hour=0, minute=0, second=0, microsecond=0)
        ed0 = end.replace(hour=0, minute=0, second=0, microsecond=0)

        # capture first-successful backend id/token for DM/manage link
        first_event_id = None
        first_edit_token = None

        for day in daterange(sd0, ed0):
            sd = day.replace(hour=start.hour, minute=start.minute)
            ed = day.replace(hour=end.hour, minute=end.minute)
            if ed <= sd:
                ed += timedelta(days=1)

            try:
                ev = await gcal_insert_event(
                    self.title_in.value, sd, ed,
                    self.loc_in.value or None,
                    self.desc_in.value or None,
                )

                cal_link = ev.get("htmlLink") if ev else None
                cal_id = ev.get("id") if ev else None
                if cal_link:
                    cal_links.append(cal_link)

                api_payload = {
                    "title": self.title_in.value,
                    "desc": self.desc_in.value or "",
                    "start": sd.isoformat(),
                    "end": ed.isoformat(),
                    "location": self.loc_in.value or "",
                    "channel_id": ch.id,
                    "thread_id": thread.id,
                    "discord_event_id": str(sched.id) if sched else None,
                    "creator_user_id": interaction.user.id,
                    "calendar_event_id": cal_id,
                    "calendar_link": cal_link,
                    "manage_token": manage_token,
                }

                # POST and inspect response
                res = await api_create_event(api_payload)
                if not res or not res.get("ok"):
                    logger.error("Backend failed to save event for %s -> %s", sd.date(), res)
                else:
                    body = res.get("body") if isinstance(res.get("body"), dict) else {}
                    event_id = body.get("id") or body.get("event_id") or None
                    edit_token = body.get("manage_token") or body.get("edit_token") or None
                    if event_id and not first_event_id:
                        first_event_id = event_id
                        first_edit_token = edit_token or manage_token
            except Exception as e:
                logger.exception("Error creating calendar/api event for %s: %s", sd.date(), e)
                continue

        # 3) DM the creator a manage link (once)
        try:
            base = API_BASE or "https://gibsongatorwatch.com"
            event_ref = first_event_id or "pending"
            token_ref = first_edit_token or manage_token
            manage_url = f"{base.rstrip('/')}/events/{event_ref}?token={token_ref}"
            msg = f"Your event is live.\nManage: {manage_url}\nThread: {thread.jump_url}"
            if sched_url:
                msg += f"\nDiscord Event: {sched_url}"
            await interaction.user.send(msg)
        except Exception:
            await interaction.followup.send("Could not DM you. Enable DMs from server members to receive your manage link.", ephemeral=True)

        # final ephemeral confirmation
        await interaction.followup.send("Event posted.", ephemeral=True)

        # Update the thread with calendar links if present (edit starter message if possible)
        if cal_links:
            try:
                # attach calendar field to embed (reuse same embed object)
                if len(cal_links) == 1:
                    embed.add_field(name="Calendar", value=f"[Google Calendar]({cal_links[0]})", inline=False)
                else:
                    parts = []
                    for idx, d in enumerate(daterange(sd0, ed0)):
                        if idx >= len(cal_links):
                            break
                        parts.append(f"[{d.strftime('%a %b %d')}]({cal_links[idx]})")
                    val = " | ".join(parts[:3]) + (f" (+{len(cal_links)-3} more)" if len(cal_links) > 3 else "")
                    embed.add_field(name="Calendar", value=val, inline=False)

                if starter_msg:
                    try:
                        await starter_msg.edit(embed=embed)
                    except Exception:
                        # older library/structures may prevent editing; fallback to posting into thread
                        await thread.send(embed=embed)
                else:
                    await thread.send(embed=embed)
            except Exception as e:
                logger.exception("Failed to update thread with calendar links: %s", e)
