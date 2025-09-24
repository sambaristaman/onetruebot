# main.py
import os
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Tuple, Optional

import discord


# -------------------------
# Helpers & env parsing
# -------------------------
def get_int_env(name: str, default: int = 0) -> int:
    val = os.getenv(name, "")
    try:
        return int(val) if val and val.strip() else default
    except ValueError:
        return default


def get_bool_env(name: str, default: bool = False) -> bool:
    v = (os.getenv(name, "") or "").strip().lower()
    if v in ("1", "true", "yes", "y", "on"):
        return True
    if v in ("0", "false", "no", "n", "off"):
        return False
    return default


def parse_id_csv(name: str) -> List[int]:
    raw = os.getenv(name, "") or ""
    out: List[int] = []
    for part in raw.split(","):
        p = part.strip()
        if not p:
            continue
        try:
            out.append(int(p))
        except ValueError:
            logging.warning(f"Env {name} contains a non-integer: {p!r} (ignored)")
    return out


# Required core config
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID = get_int_env("GUILD_ID", 0)
ROLE_ID = get_int_env("ROLE_ID", 0)
THRESHOLD_DAYS = get_int_env("THRESHOLD_DAYS", 2)

# Original feature extras
DM_MESSAGE = (os.getenv("DM_MESSAGE", "") or "").strip()
FORCE_ASSIGN = get_bool_env("FORCE_ASSIGN", False)
EXCLUDE_ROLE_IDS = parse_id_csv("EXCLUDE_ROLE_IDS")

# Global flags
DRY_RUN = get_bool_env("DRY_RUN", False)
TARGET_USER_ID = get_int_env("TARGET_USER_ID", 0)

# New role-pair feature env
PAIR_PRIMARY_ROLE_ID = get_int_env("PAIR_PRIMARY_ROLE_ID", 0)  # role to remove
PAIR_SECONDARY_ROLE_IDS = parse_id_csv("PAIR_SECONDARY_ROLE_IDS")[:3]  # up to 3 roles

# Messages mapped by index to the secondary roles above.
# Each message can contain \n to split into multiple DMs (one DM per line).
PAIR_DM_MESSAGES = [
    (os.getenv("PAIR_DM_MESSAGE_1", "") or "").strip(),
    (os.getenv("PAIR_DM_MESSAGE_2", "") or "").strip(),
    (os.getenv("PAIR_DM_MESSAGE_3", "") or "").strip(),
]

# Discord intents
intents = discord.Intents.none()
intents.guilds = True
intents.members = True  # Make sure "Server Members Intent" is enabled in your application

client = discord.Client(intents=intents)


# -------------------------
# Messaging helpers
# -------------------------
async def send_single_dm(member: discord.Member, text: str) -> None:
    """Original feature behavior: one DM total (no line splitting)."""
    if not text:
        return
    try:
        if DRY_RUN:
            logging.info(f"[DRY_RUN] Would DM {member}: {text}")
        else:
            await member.send(text)
    except discord.Forbidden:
        logging.warning(f"Could not DM {member}: privacy settings.")
    except discord.HTTPException as e:
        logging.warning(f"HTTP error DMing {member}: {e}")


async def send_multi_dm(member: discord.Member, text: str) -> None:
    """New feature behavior: one DM per line."""
    if not text:
        return
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    for ln in lines:
        try:
            if DRY_RUN:
                logging.info(f"[DRY_RUN] Would DM {member}: {ln}")
            else:
                await member.send(ln)
        except discord.Forbidden:
            logging.warning(f"Could not DM {member}: privacy settings.")
            break
        except discord.HTTPException as e:
            logging.warning(f"HTTP error DMing {member}: {e}")
            break
        await asyncio.sleep(0.2)  # gentle pacing


# -------------------------
# Original feature (assign on threshold)
# -------------------------
def is_past_threshold(member: discord.Member) -> bool:
    if FORCE_ASSIGN:
        return True
    created = member.created_at.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - created
    return age >= timedelta(days=max(0, THRESHOLD_DAYS))


async def add_role_if_needed(member: discord.Member, role: discord.Role) -> bool:
    """Returns True if role was (or would be) added; False otherwise."""
    if member.bot:
        return False
    if role in member.roles:
        logging.info(f"[SKIP] {member} already has '{role.name}'.")
        return False
    if any((r.id in EXCLUDE_ROLE_IDS) for r in member.roles):
        logging.info(f"[SKIP] {member} excluded via EXCLUDE_ROLE_IDS.")
        return False
    if not is_past_threshold(member):
        logging.info(f"[SKIP] {member} not past threshold.")
        return False

    if DRY_RUN:
        logging.info(f"[DRY_RUN] Would add role '{role.name}' to {member}.")
    else:
        try:
            await member.add_roles(role, reason="Auto: threshold reached")
        except discord.Forbidden:
            logging.warning(f"Missing permissions to add role '{role.name}' to {member}.")
            return False
        except discord.HTTPException as e:
            logging.warning(f"HTTP error adding role to {member}: {e}")
            return False

    # Original behavior: single DM (no splitting)
    await send_single_dm(member, DM_MESSAGE)
    return True


async def process_single_user(guild: discord.Guild, role: discord.Role) -> None:
    try:
        member = await guild.fetch_member(TARGET_USER_ID)
    except discord.NotFound:
        logging.error(f"TARGET_USER_ID {TARGET_USER_ID} not found in guild.")
        return
    await add_role_if_needed(member, role)


async def process_full_scan(guild: discord.Guild, role: discord.Role) -> None:
    changed = 0
    async for member in guild.fetch_members(limit=None):
        changed += 1 if await add_role_if_needed(member, role) else 0
        await asyncio.sleep(0.05)  # be nice to the API
    logging.info(f"Threshold scan done. Members updated: {changed}.")


# -------------------------
# New feature (role-pair scanner)
# -------------------------
async def process_role_pairs(guild: discord.Guild) -> None:
    """
    If a member has PAIR_PRIMARY_ROLE_ID AND any of PAIR_SECONDARY_ROLE_IDS,
    remove the primary and send mapped multi-line DM.
    """
    if not PAIR_PRIMARY_ROLE_ID or not PAIR_SECONDARY_ROLE_IDS:
        logging.info("Role-pair scan not configured. Skipping.")
        return

    # Resolve primary role
    primary_role: Optional[discord.Role] = guild.get_role(PAIR_PRIMARY_ROLE_ID)
    if primary_role is None:
        try:
            primary_role = await guild.fetch_role(PAIR_PRIMARY_ROLE_ID)
        except discord.NotFound:
            logging.warning(f"Primary role {PAIR_PRIMARY_ROLE_ID} not found. Skipping role-pair scan.")
            return

    # Resolve secondary roles into (index, Role|None)
    sec_roles: List[Tuple[int, Optional[discord.Role]]] = []
    for idx, sec_id in enumerate(PAIR_SECONDARY_ROLE_IDS):
        role_obj = guild.get_role(sec_id)
        if role_obj is None:
            try:
                role_obj = await guild.fetch_role(sec_id)
            except discord.NotFound:
                logging.warning(f"Secondary role {sec_id} not found; will be ignored.")
                role_obj = None
        sec_roles.append((idx, role_obj))

    affected = 0

    async for member in guild.fetch_members(limit=None):
        if member.bot:
            continue
        if TARGET_USER_ID and member.id != TARGET_USER_ID:
            continue
        if primary_role not in member.roles:
            continue

        # Find the first matching secondary role
        matched_idx = None
        matched_role = None
        for idx, sec in sec_roles:
            if sec and sec in member.roles:
                matched_idx = idx
                matched_role = sec
                break
        if matched_idx is None:
            continue

        msg = PAIR_DM_MESSAGES[matched_idx] if matched_idx < len(PAIR_DM_MESSAGES) else ""

        # Remove primary role
        if DRY_RUN:
            logging.info(f"[DRY_RUN] Would REMOVE '{primary_role.name}' from {member} (paired with '{matched_role.name}').")
        else:
            try:
                await member.remove_roles(primary_role, reason="Auto: role-pair resolution")
            except discord.Forbidden:
                logging.warning(f"Missing permissions to remove '{primary_role.name}' from {member}.")
                continue
            except discord.HTTPException as e:
                logging.warning(f"HTTP error removing role from {member}: {e}")
                continue

        # Send multi-line DM mapped to the matching secondary role
        await send_multi_dm(member, msg)
        affected += 1
        await asyncio.sleep(0.05)

    logging.info(f"Role-pair scan done. Affected members: {affected}.")


# -------------------------
# Job orchestration
# -------------------------
async def run_job():
    guild = client.get_guild(GUILD_ID) or await client.fetch_guild(GUILD_ID)

    # Resolve the role for the original feature
    base_role = guild.get_role(ROLE_ID)
    if base_role is None:
        try:
            base_role = await guild.fetch_role(ROLE_ID)
        except discord.NotFound:
            logging.error(f"ROLE_ID {ROLE_ID} not found in guild.")
            return

    # 1) New feature first: resolve role pairs
    await process_role_pairs(guild)

    # 2) Original feature next: targeted or full scan
    if TARGET_USER_ID:
        logging.info("Running in TARGETED MODE (single user).")
        await process_single_user(guild, base_role)
    else:
        logging.info("Running in FULL SCAN MODE (all members).")
        await process_full_scan(guild, base_role)


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
