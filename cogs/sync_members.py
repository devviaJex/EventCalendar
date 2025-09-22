# cogs/sync_members.py
import discord
from discord import app_commands
from discord.ext import commands
from typing import List
from datetime import datetime, timezone

from shared import open_ws, GUILD_ID, MEMBERS_TAB,ROLES_SHEET,RULES_SHEET
    
TAB_NAME = MEMBERS_TAB

UTC = timezone.utc

class SyncMembers(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="sync_members",
        description="Upsert all members into MemberTable and mark leavers."
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def sync_members(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)
        now = datetime.now(UTC).isoformat()

        guild = self.bot.get_guild(GUILD_ID) or await self.bot.fetch_guild(GUILD_ID)

        # fetch all members
        members: List[discord.Member] = []
        async for m in guild.fetch_members(limit=None):
            members.append(m)

        ws = open_ws(ROLES_SHEET,TAB_NAME)
        values = ws.get_all_values()
        header = values[0] if values else []

        # map headers to indices
        idx = {name: i for i, name in enumerate(header)}

        # build current member set
        current_ids = {str(m.id) for m in members}

        # index existing rows by User ID
        existing = {}
        for row_num, row in enumerate(values[1:], start=2):  # skip header
            if len(row) <= idx.get("User Name", -1):
                continue
            user = row[idx["User Name"]]
            if user:
                existing[user] = (row_num, row)

        updates = []
        appends = []

        for m in members:
            uid = str(m.id)
            username = f"{m.name}#{m.discriminator}"
            display = m.display_name
            roles = ", ".join(sorted(r.name for r in m.roles if r and r.name != "@everyone"))

            row_data = {
                "User Name": username,
                "First Name": display,  # adjust if you want real split
                "Last Name": "",
                "Area Role": "",
                "Permission Level": "",
                "Service Offered": "",
                "Interests": "",
                "Activity type": "",
                "Contributions": "",
                "First Seen": now,
                "Last Seen": now,
                "Active": "YES",
                "Left At": "",
            }

            if uid in existing:
                # update
                row_num, row = existing[uid]
                row[idx["Last Seen"]] = now
                row[idx["Active"]] = "YES"
                updates.append((row_num, row))
            else:
                # new row
                row = [row_data.get(col, "") for col in header]
                appends.append(row)

        # mark leavers
        for uid, (row_num, row) in existing.items():
            if uid not in current_ids:
                row[idx["Active"]] = "NO"
                if not row[idx["Left At"]]:
                    row[idx["Left At"]] = now
                updates.append((row_num, row))

        # write
        if appends:
            ws.append_rows(appends, value_input_option="RAW")

        for row_num, row in updates:
            rng = f"A{row_num}:{chr(65+len(header)-1)}{row_num}"
            ws.update(rng, [row], value_input_option="RAW")

        await interaction.followup.send(
            f"Synced {len(members)} active members. Added {len(appends)} new. Updated {len(updates)} existing.",
            ephemeral=True
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(SyncMembers(bot))

