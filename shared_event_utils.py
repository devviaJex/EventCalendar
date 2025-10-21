from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo
import re, discord
from shared import TZ_NAME, gcal_insert_event
import os, uuid, aiohttp

API_BASE = os.getenv("GGW_API_BASE")            # e.g. https://gibsongatorwatch.com
API_KEY  = os.getenv("GGW_API_KEY")             # bearer token

async def api_create_event(payload: dict) -> dict | None:
    if not API_BASE or not API_KEY: return None
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{API_BASE}/api/events", json=payload, headers={"Authorization": f"Bearer {API_KEY}"} ) as r:
            return await r.json() if r.status < 300 else None


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
        async def _ok(i: discord.Interaction): self.stop(); await i.response.edit_message(view=None)
        async def _cancel(i: discord.Interaction): self.selected=[]; self.stop(); await i.response.edit_message(content="Canceled.", view=None)
        ok.callback = _ok  # type: ignore
        cancel.callback = _cancel  # type: ignore
        self.add_item(ok); self.add_item(cancel)

# ---------- shared modal ----------
class EventModal(discord.ui.Modal):
    def __init__(self, cfg: EventCfg):
        super().__init__(title=cfg.modal_title)
        self.cfg = cfg
        self.title_in    = discord.ui.TextInput(label="Title", max_length=120, required=True)
        self.start_dt_in = discord.ui.TextInput(label="Start (MM/DD/YYYY h:mm am/pm)", required=True)
        self.end_dt_in   = discord.ui.TextInput(label="End (MM/DD/YYYY h:mm am/pm)", required=True)
        self.loc_in      = discord.ui.TextInput(label="Location (optional)", required=False)
        self.desc_in     = discord.ui.TextInput(label="Details (optional)", style=discord.TextStyle.paragraph, required=False, max_length=1000)
        for x in (self.title_in, self.start_dt_in, self.end_dt_in, self.loc_in, self.desc_in):
            self.add_item(x)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        tz = ZoneInfo(TZ_NAME)
        try:
            start = parse_mdy12(self.start_dt_in.value).replace(tzinfo=tz)
            end   = parse_mdy12(self.end_dt_in.value).replace(tzinfo=tz)
        except Exception:
            await interaction.followup.send("Invalid datetime. Use MM/DD/YYYY h:mm am/pm.", ephemeral=True); return
        if end <= start:
            await interaction.followup.send("End must be after start.", ephemeral=True); return

        # resolve forum channel
        ch = interaction.guild.get_channel(self.cfg.channel_id) if interaction.guild else None
        if ch is None:
            try:
                ch = await interaction.client.fetch_channel(self.cfg.channel_id)
            except Exception:
                await interaction.followup.send("Configured channel not accessible.", ephemeral=True); return
        if not isinstance(ch, discord.ForumChannel):
            await interaction.followup.send("Target channel is not a Forum.", ephemeral=True); return

        # tag picker
        view = ForumTagView(ch)
        await interaction.followup.send(self.cfg.tag_prompt, view=view, ephemeral=True)
        await view.wait()
        if not view.selected:
            return
        tags, missing = resolve_forum_tags(ch, view.selected)
        if not tags:
            await interaction.followup.send("No valid tags selected.", ephemeral=True); return

        # create calendar events
        cal_links = []
        sd0 = start.replace(hour=0, minute=0, second=0, microsecond=0)
        ed0 = end.replace(hour=0, minute=0, second=0, microsecond=0)
        for day in daterange(sd0, ed0):
            sd = day.replace(hour=start.hour, minute=start.minute)
            ed = day.replace(hour=end.hour, minute=end.minute)
            if ed <= sd: ed += timedelta(days=1)
            try:
                ev = await gcal_insert_event(self.title_in.value, sd, ed, self.loc_in.value or None, self.desc_in.value or None)
                if ev and ev.get("htmlLink"): cal_links.append(ev["htmlLink"])
            except Exception:
                pass

        # compose embed
        when_lines = [f"{d.strftime('%a %b %d')}  {start.strftime('%H:%M')}-{end.strftime('%H:%M')}" for d in daterange(sd0, ed0)]
        title_prefix = sd0.strftime("%m/%d") if sd0.date() == ed0.date() else f"{sd0.strftime('%m/%d')}-{ed0.strftime('%m/%d')}"
        final_title = f"{title_prefix} {self.title_in.value}"

        embed = discord.Embed(title=final_title, description=self.desc_in.value or "")
        embed.add_field(name="When", value="\n".join(when_lines), inline=False)
        if self.loc_in.value:
            embed.add_field(name="Where", value=self.loc_in.value, inline=False)
        embed.add_field(name="Tags", value=", ".join(view.selected), inline=False)
        if cal_links:
            if len(cal_links) == 1:
                embed.add_field(name="Calendar", value=f"[Google Calendar]({cal_links[0]})", inline=False)
            else:
                parts = []
                for idx, d in enumerate(daterange(sd0, ed0)):
                    if idx >= len(cal_links): break
                    parts.append(f"[{d.strftime('%a %b %d')}]({cal_links[idx]})")
                val = " | ".join(parts[:3]) + (f" (+{len(cal_links)-3} more)" if len(cal_links) > 3 else "")
                embed.add_field(name="Calendar", value=val, inline=False)
        
        await ch.create_thread(name=final_title, embed=embed, applied_tags=tags[:5])
        await interaction.followup.send("Event posted.", ephemeral=True)
