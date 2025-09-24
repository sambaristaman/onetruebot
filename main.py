# main.py
import os
import asyncio
import logging
from datetime import datetime, timezone, timedelta

import discord

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
ROLE_ID = int(os.getenv("ROLE_ID", "0"))
THRESHOLD_DAYS = int(os.getenv("THRESHOLD_DAYS", "2"))

# Optional: customize the DM text via env var
DM_MESSAGE = os.getenv(
    "DM_MESSAGE",
    "🎉 Hi {name}! You've been in **{server}** for more than {days} days, "
    "so I’ve given you the **{role}** role. Welcome aboard!"
)

# Optional: list of role IDs to exclude (comma-separated)
EXCLUDE_ROLE_IDS = [
    int(r.strip()) for r in os.getenv("EXCLUDE_ROLE_IDS", "").split(",") if r.strip()
]

# ---- Test/override flags ----
# TARGET_USER_ID: only act on this single user (skip full-member scan)
TARGET_USER_ID = int(os.getenv("TARGET_USER_ID", "0"))  # 0 disables targeted mode
# FORCE_ASSIGN: if "1", bypass time threshold check (for testing)
FORCE_ASSIGN = os.getenv("FORCE_ASSIGN", "0") == "1"
# DRY_RUN: if "1", log actions but do not add roles or send DMs
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"

intents = discord.Intents.none()
intents.guilds = True
intents.members = True  # IMPORTANT: enable "Server Members Intent" in the Dev Portal

client = discord.Client(intents=intents)


async def add_role_and_dm(guild: discord.Guild, member: discord.Member, role: discord.Role):
    """
    Adds the role and sends the DM (only once, when role is newly added).
    Honors DRY_RUN.
    """
    if role in member.roles:
        logging.info(f"[SKIP] {member} already has role '{role.name}'.")
        return False

    if DRY_RUN:
        logging.info(f"[DRY_RUN] Would add role '{role.name}' to {member}.")
        logging.info(f"[DRY_RUN] Would DM {member}: {DM_MESSAGE.format(name=member.display_name, server=guild.name, days=THRESHOLD_DAYS, role=role.name)}")
        return True

    await member.add_roles(
        role,
        reason=f"{'FORCE_ASSIGN ' if FORCE_ASSIGN else ''}Auto via GitHub Actions",
    )

    # Try to DM; ignore if blocked
    try:
        text = DM_MESSAGE.format(
            name=member.display_name,
            server=guild.name,
            days=THRESHOLD_DAYS,
            role=role.name,
        )
        await member.send(text)
    except discord.Forbidden:
        logging.warning(f"Could not DM {member} (DMs disabled or privacy settings).")
    except discord.HTTPException as e:
        logging.warning(f"HTTP error while DMing {member}: {e}")

    await asyncio.sleep(0.2)  # gentle pacing
    return True


async def process_single_user(guild: discord.Guild, role: discord.Role) -> None:
    """
    Targeted mode: act only on TARGET_USER_ID.
    """
    user_id = TARGET_USER_ID
    if not user_id:
        logging.info("TARGET_USER_ID not set; skipping targeted mode.")
        return

    try:
        member = guild.get_member(user_id) or await guild.fetch_member(user_id)
    except discord.NotFound:
        logging.error(f"TARGET_USER_ID {user_id} not found in guild.")
        return

    # Exclusions
    if any(r.id in EXCLUDE_ROLE_IDS for r in member.roles):
        logging.info(f"[SKIP] {member} has excluded role(s); not changing.")
        return

    # Threshold check (unless FORCE_ASSIGN)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=THRESHOLD_DAYS)
    qualifies = FORCE_ASSIGN or (member.joined_at and member.joined_at <= cutoff)

    logging.info(
        f"Targeted mode for {member} | joined_at={member.joined_at} | "
        f"cutoff={cutoff.isoformat()} | FORCE_ASSIGN={FORCE_ASSIGN} | qualifies={qualifies}"
    )

    if not qualifies:
        logging.info(f"[SKIP] {member} does not meet threshold and FORCE_ASSIGN=0.")
        return

    changed = await add_role_and_dm(guild, member, role)
    if changed:
        logging.info(f"Targeted mode: processed {member}.")


async def process_full_scan(guild: discord.Guild, role: discord.Role) -> None:
    """
    Normal daily mode: scan all members and apply threshold logic.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=THRESHOLD_DAYS)
    added = 0
    checked = 0

    logging.info(
        f"Full scan in '{guild.name}' ({guild.id}) | cutoff={cutoff.isoformat()} | "
        f"EXCLUDE_ROLE_IDS={EXCLUDE_ROLE_IDS} | DRY_RUN={DRY_RUN}"
    )

    async for member in guild.fetch_members(limit=None):
        checked += 1

        if member.bot or getattr(member, "pending", False):
            continue
        if not member.joined_at:
            continue
        if any(r.id in EXCLUDE_ROLE_IDS for r in member.roles):
            continue

        qualifies = member.joined_at <= cutoff or FORCE_ASSIGN
        if qualifies:
            changed = await add_role_and_dm(guild, member, role)
            if changed:
                added += 1

    logging.info(f"Full scan done. Checked {checked} members, affected {added} members.")


async def run_job():
    guild = client.get_guild(GUILD_ID) or await client.fetch_guild(GUILD_ID)

    role = guild.get_role(ROLE_ID)
    if role is None:
        try:
            role = await guild.fetch_role(ROLE_ID)
        except discord.NotFound:
            raise SystemExit(f"Role ID {ROLE_ID} not found in guild {guild.name} ({guild.id}).")

    if TARGET_USER_ID:
        logging.info("Running in TARGETED MODE (single user).")
        await process_single_user(guild, role)
    else:
        logging.info("Running in FULL SCAN MODE (all members).")
        await process_full_scan(guild, role)


@client.event
async def on_ready():
    logging.info(f"Logged in as {client.user} (id={client.user.id})")
    try:
        await run_job()
    finally:
        await client.close()  # clean exit


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not TOKEN or not GUILD_ID or not ROLE_ID:
        raise SystemExit("Set DISCORD_TOKEN, GUILD_ID, ROLE_ID env vars.")
    client.run(TOKEN)
