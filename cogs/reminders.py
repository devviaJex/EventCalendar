# cogs/reminders.py
import os
from datetime import datetime, timedelta
from dateutil import parser as du_parser

import discord
from discord.ext import commands, tasks

from shared import (
    TZ, EVENT_CHANNEL_ID, display_dt,
    gcal_list, db_exec, db_fetchone
)

REMIND_MINUTES = int(os.getenv("REMIND_MINUTES", "60"))  # minutes before start

class Reminders(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.reminder_loop.start()

    def cog_unload(self):
        self.reminder_loop.cancel()

    @tasks.loop(seconds=60)
    async def reminder_loop(self):
        now = datetime.now(TZ)
        window_start = now
        window_end = now + timedelta(minutes=REMIND_MINUTES)
        try:
            resp = await gcal_list(window_start.isoformat(), window_end.isoformat(), max_items=50)
            items = resp.get("items", [])
        except Exception:
            return

        for ev in items:
            event_id = ev.get("id")
            start_iso = ev.get("start", {}).get("dateTime")
            if not start_iso:  # skip all-day events
                continue
            try:
                start_dt = du_parser.isoparse(start_iso).astimezone(TZ)
            except Exception:
                continue

            minutes_left = int((start_dt - now).total_seconds() // 60)
            if minutes_left < 0 or minutes_left > REMIND_MINUTES:
                continue

            tag = f"T-{REMIND_MINUTES}"
            seen = await db_fetchone("SELECT 1 FROM reminder_log WHERE event_id=? AND tag=?", (event_id, tag))
            if seen:
                continue

            # Prefer event thread; fallback to events channel
            row = await db_fetchone("SELECT thread_id, channel_id FROM events_map WHERE event_id=?", (event_id,))
            channel = None
            thread = None
            if row:
                thread_id, chan_id = row[0], row[1]
                if thread_id:
                    try:
                        thread = self.bot.get_channel(thread_id) or await self.bot.fetch_channel(thread_id)
                    except Exception:
                        thread = None
                if not thread and chan_id:
                    channel = self.bot.get_channel(chan_id)
            if not (thread or channel):
                channel = self.bot.get_channel(EVENT_CHANNEL_ID) if EVENT_CHANNEL_ID else None
            if not (thread or channel):
                continue

            title = ev.get("summary", "Event")
            when = display_dt(start_iso)
            link = ev.get("htmlLink")
            content = f"⏰ Reminder: **{title}** starts in {minutes_left} min — {when}\n{link}\n— Marsh Mellow 🐊"
            try:
                if thread:
                    await thread.send(content)
                else:
                    await channel.send(content)
                await db_exec(
                    "INSERT OR REPLACE INTO reminder_log(event_id, tag, notified_at) VALUES(?,?,?)",
                    (event_id, tag, datetime.now(TZ).isoformat()),
                )
            except Exception:
                pass

    @reminder_loop.before_loop
    async def before(self):
        await self.bot.wait_until_ready()

async def setup(bot: commands.Bot):
    await bot.add_cog(Reminders(bot))
