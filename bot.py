"""
Discord Monitoring Bot (Pycord)
Monitors the online/offline status of another bot.
Displays a self-updating live status embed, sends ping notifications
on status changes, and provides /history + /maintenance commands.
"""

import asyncio
import json
import os
import discord
from discord.ext import tasks
from datetime import datetime, timezone
from config import (
    BOT_TOKEN, WATCHED_BOT_ID, STATUS_EMBED_CHANNEL_ID,
    STATUS_LOG_CHANNEL_ID, STATUS_ROLE_ID, DEVELOPER_IDS,
    ADMIN_ROLE_IDS, GUILD_ID,
)

# ============================================================
# CONFIGURATION
# ============================================================

CHECK_INTERVAL_SECONDS = 1200

# Persistent data file (message ID + history)
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "status_data.json")

# Maximum number of stored outages in history
MAX_HISTORY_ENTRIES = 50

# ============================================================
# BOT SETUP
# ============================================================

intents = discord.Intents.default()
intents.presences = True
intents.members = True

try:
    asyncio.get_running_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

bot = discord.Bot(intents=intents)

# ============================================================
# STATE
# ============================================================

offline_since: datetime | None = None
maintenance_mode: bool = False
maintenance_since: datetime | None = None
last_known_online: bool | None = None

# Live status embed message ID (persisted)
status_message_id: int | None = None

# Timestamp since which the bot has been continuously online
online_since: datetime | None = None

# Timestamp of the last check
last_checked: datetime | None = None

# History: list of dicts {"start": iso, "end": iso, "duration_seconds": int}
outage_history: list[dict] = []

# ID of the alert ping message in the embed channel (deleted when back online)
alert_ping_message_id: int | None = None


# ============================================================
# PERSISTENCE
# ============================================================

def save_data() -> None:
    """Saves message ID and outage history to a JSON file."""
    data = {
        "status_message_id": status_message_id,
        "alert_ping_message_id": alert_ping_message_id,
        "outage_history": outage_history,
    }
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_data() -> None:
    """Loads saved data from the JSON file."""
    global status_message_id, alert_ping_message_id, outage_history
    if not os.path.exists(DATA_FILE):
        return
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        status_message_id = data.get("status_message_id")
        alert_ping_message_id = data.get("alert_ping_message_id")
        outage_history = data.get("outage_history", [])
    except (json.JSONDecodeError, OSError) as e:
        print(f"[WARN] Could not load {DATA_FILE}: {e}")


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def format_duration(delta) -> str:
    total_seconds = int(delta.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    parts = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def build_ping_string() -> str:
    pings = [f"<@{uid}>" for uid in DEVELOPER_IDS]
    pings.append(f"<@&{STATUS_ROLE_ID}>")
    return " ".join(pings)


def is_bot_online(member: discord.Member | None) -> bool:
    if member is None:
        return False
    return member.status in (discord.Status.online, discord.Status.idle, discord.Status.dnd)


def has_admin_role(member: discord.Member) -> bool:
    return any(role.id in ADMIN_ROLE_IDS for role in member.roles)


async def send_log_message(embed: discord.Embed, ping: bool = True) -> None:
    """Sends a log message (down/up/maintenance) to the log channel."""
    channel = bot.get_channel(STATUS_LOG_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(STATUS_LOG_CHANNEL_ID)
        except discord.HTTPException:
            print(f"[WARN] Log channel {STATUS_LOG_CHANNEL_ID} not found.")
            return
    try:
        content = build_ping_string() if ping else None
        await channel.send(content=content, embed=embed)
    except discord.HTTPException as e:
        print(f"[ERROR] Could not send log message: {e}")


async def send_alert_ping() -> None:
    """Sends a role ping in the embed channel while the bot is offline."""
    global alert_ping_message_id

    channel = bot.get_channel(STATUS_EMBED_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(STATUS_EMBED_CHANNEL_ID)
        except discord.HTTPException:
            return
    try:
        msg = await channel.send(f"⚠️ <@&{STATUS_ROLE_ID}> — Bot is **offline**!")
        alert_ping_message_id = msg.id
        save_data()
    except discord.HTTPException as e:
        print(f"[ERROR] Could not send alert ping: {e}")


async def delete_alert_ping() -> None:
    """Deletes the alert ping message from the embed channel."""
    global alert_ping_message_id

    if alert_ping_message_id is None:
        return

    channel = bot.get_channel(STATUS_EMBED_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(STATUS_EMBED_CHANNEL_ID)
        except discord.HTTPException:
            alert_ping_message_id = None
            return
    try:
        msg = await channel.fetch_message(alert_ping_message_id)
        await msg.delete()
    except (discord.NotFound, discord.HTTPException):
        pass
    alert_ping_message_id = None
    save_data()


# ============================================================
# LIVE-STATUS-EMBED
# ============================================================

def build_live_embed() -> discord.Embed:
    """Builds the central live status embed."""
    now = datetime.now(timezone.utc)

    if maintenance_mode:
        embed = discord.Embed(
            title="<a:settings:1480272427819991070> Maintenance Mode",
            description=f"<@{WATCHED_BOT_ID}> is currently under **maintenance**.",
            color=0x252627,
        )

    elif last_known_online:
        embed = discord.Embed(
            title="<a:online:1480273793833504908> Online",
            description=f"<@{WATCHED_BOT_ID}> is **online** and operational.",
            color=0x252627,
        )
    else:
        embed = discord.Embed(
            title="<a:offline:1480273832425296038> Offline!",
            description=f"▬▬▬▬▬▬▬`Status:`▬▬▬▬▬▬▬\n<@{WATCHED_BOT_ID}> is currently **__unavailable__**!\n <a:Arlert:1480274858066841842> The developers has been notified\n ▬▬▬▬▬▬▬`Infos:`▬▬▬▬▬▬▬\n",
            color=0x252627,
        )

    # Uptime / Downtime / Maintenance duration
    if maintenance_mode and maintenance_since:
        embed.add_field(name="<a:Sandwatch:1480277835678613554> Maintenance since:", value=f"<t:{int(maintenance_since.timestamp())}:R>", inline=True)
    elif last_known_online and online_since:
        embed.add_field(name="<a:Sandwatch:1480277835678613554> Uptime:", value=f"<t:{int(online_since.timestamp())}:R>", inline=True)
    elif not last_known_online and offline_since:
        embed.add_field(name="<a:Sandwatch:1480277835678613554> Downtime:", value=f"<t:{int(offline_since.timestamp())}:R>", inline=True)

    # Last outage from history
    if outage_history:
        last = outage_history[-1]
        end_ts = int(datetime.fromisoformat(last["end"]).timestamp()) if last.get("end") else None
        dur = format_duration(__import__("datetime").timedelta(seconds=last["duration_seconds"])) if last.get("duration_seconds") else "?"
        if end_ts:
            embed.add_field(
                name="<:bug:1480279132330791107> Last Outage:",
                value=f"<t:{end_ts}:R> ",
                inline=True,
            )

    # Last checked
    if last_checked:
        embed.add_field(
            name="<a:loading:1480279699879100547> Last Checked:",
            value=f"<t:{int(last_checked.timestamp())}:R>",
            inline=True,
        )

    embed.set_footer(text="Live Status • Updated every 20 minutes")
    embed.timestamp = now
    return embed


async def update_live_embed() -> None:
    """Updates the live status embed (or creates a new one)."""
    global status_message_id

    channel = bot.get_channel(STATUS_EMBED_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(STATUS_EMBED_CHANNEL_ID)
        except discord.HTTPException:
            print(f"[WARN] Embed channel {STATUS_EMBED_CHANNEL_ID} not found.")
            return

    embed = build_live_embed()

    # Try to edit the existing message
    if status_message_id is not None:
        try:
            msg = await channel.fetch_message(status_message_id)
            await msg.edit(embed=embed)
            return
        except (discord.NotFound, discord.HTTPException):
            print("[INFO] Live embed message not found, creating a new one.")
            status_message_id = None

    # Send a new message
    try:
        msg = await channel.send(embed=embed)
        status_message_id = msg.id
        save_data()
        print(f"[INFO] Live status embed created (Message ID: {status_message_id})")
    except discord.HTTPException as e:
        print(f"[ERROR] Could not send live embed: {e}")


# ============================================================
# EVENTS
# ============================================================

@bot.event
async def on_ready():
    print(f"[INFO] Monitoring bot started as {bot.user} (ID: {bot.user.id})")
    print(f"[INFO] Watching bot ID: {WATCHED_BOT_ID}")

    load_data()

    for guild in bot.guilds:
        print(f"[INFO] Loading member cache for guild: {guild.name} ({guild.id})")
        try:
            await guild.chunk()
        except Exception as e:
            print(f"[WARN] Chunk for {guild.name} failed: {e}")

    ec = bot.get_channel(STATUS_EMBED_CHANNEL_ID)
    if ec:
        print(f"[INFO] Embed channel found: {ec.name} ({ec.id})")
    else:
        print(f"[ERROR] Embed channel {STATUS_EMBED_CHANNEL_ID} NOT found!")

    lc = bot.get_channel(STATUS_LOG_CHANNEL_ID)
    if lc:
        print(f"[INFO] Log channel found: {lc.name} ({lc.id})")
    else:
        print(f"[ERROR] Log channel {STATUS_LOG_CHANNEL_ID} NOT found!")

    for guild in bot.guilds:
        member = guild.get_member(WATCHED_BOT_ID)
        if member is not None:
            print(f"[INFO] Watched bot found: {member} — Status: {member.status}")
            break
    else:
        print(f"[WARN] Watched bot {WATCHED_BOT_ID} not found!")

    if not status_check_loop.is_running():
        status_check_loop.start()

    print("[INFO] Bot is ready.")


@bot.event
async def on_presence_update(before: discord.Member, after: discord.Member):
    if after.id != WATCHED_BOT_ID:
        return
    print(f"[DEBUG] Presence-Update: {before.status} -> {after.status}")
    await evaluate_status(after)
    await update_live_embed()


# ============================================================
# BACKGROUND TASK (Fallback Polling + Live Embed Update)
# ============================================================

@tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
async def status_check_loop():
    global last_checked
    last_checked = datetime.now(timezone.utc)

    for guild in bot.guilds:
        member = guild.get_member(WATCHED_BOT_ID)
        if member is not None:
            await evaluate_status(member)
            await update_live_embed()
            return
    await evaluate_status(None)
    await update_live_embed()


@status_check_loop.before_loop
async def before_status_check():
    await bot.wait_until_ready()


# ============================================================
# STATUS EVALUATION
# ============================================================

async def evaluate_status(member: discord.Member | None) -> None:
    global offline_since, last_known_online, online_since

    currently_online = is_bot_online(member)

    # First run — set initial state
    if last_known_online is None:
        last_known_online = currently_online
        if currently_online:
            online_since = datetime.now(timezone.utc)
        else:
            offline_since = datetime.now(timezone.utc)
        print(f"[INFO] Initial status set: online={currently_online}")
        await update_live_embed()
        return

    # No change
    if currently_online == last_known_online:
        return

    print(f"[INFO] Status change detected: online={last_known_online} -> {currently_online}")

    # ---- Bot went OFFLINE ----
    if not currently_online:
        last_known_online = False
        offline_since = datetime.now(timezone.utc)
        online_since = None

        embed = discord.Embed(
            title="<a:Arlert:1480274858066841842> Bot Offline",
            description=f"<@{WATCHED_BOT_ID}> is currently **__unavailable__**!",
            color=0x252627,
            timestamp=offline_since,
        )
        embed.add_field(
            name="<a:Sandwatch:1480277835678613554> Down Since:",
            value=f"<t:{int(offline_since.timestamp())}:F>",
            inline=False,
        )
        await send_log_message(embed, ping=not maintenance_mode)
        if not maintenance_mode:
            await send_alert_ping()

    # ---- Bot is back ONLINE ----
    else:
        last_known_online = True
        now = datetime.now(timezone.utc)
        online_since = now
        await delete_alert_ping()

        embed = discord.Embed(
            title="<a:online:1480273793833504908> Bot Online",
            description=f"<@{WATCHED_BOT_ID}> is back **online**.",
            color=0x252627,
            timestamp=now,
        )

        if offline_since is not None:
            downtime = now - offline_since
            embed.add_field(name="<a:Sandwatch:1480277835678613554> Downtime:", value=format_duration(downtime), inline=True)
            embed.add_field(name="<a:offline:1480273832425296038> Offline Since:", value=f"<t:{int(offline_since.timestamp())}:F>", inline=True)
            embed.add_field(name="<a:online:1480273793833504908> Back Online:", value=f"<t:{int(now.timestamp())}:F>", inline=True)

            # Save to history (skip during maintenance)
            if not maintenance_mode:
                outage_history.append({
                    "start": offline_since.isoformat(),
                    "end": now.isoformat(),
                    "duration_seconds": int(downtime.total_seconds()),
                })
                while len(outage_history) > MAX_HISTORY_ENTRIES:
                    outage_history.pop(0)
                save_data()

        await send_log_message(embed, ping=not maintenance_mode)
        offline_since = None

    # Update live embed immediately
    await update_live_embed()


# ============================================================
# SLASH-COMMANDS
# ============================================================

@bot.slash_command(
    name="maintenance_on",
    description="Enables maintenance mode.",
    guild_ids=[GUILD_ID] if GUILD_ID else None,
)
async def maintenance_on(ctx: discord.ApplicationContext):
    global maintenance_mode, maintenance_since

    if not has_admin_role(ctx.author):
        await ctx.respond("<a:Arlert:1480274858066841842> You don't have permission.", ephemeral=True)
        return

    maintenance_mode = True
    maintenance_since = datetime.now(timezone.utc)

    embed = discord.Embed(
        title="<a:settings:1480272427819991070> Maintenance Mode Enabled",
        description="The monitored bot is currently under **maintenance**.",
        color=0x252627,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text=f"Enabled by: {ctx.author.display_name}")
    await send_log_message(embed, ping=False)
    await update_live_embed()
    await ctx.respond("<a:checkmark:1480279615695229111> Maintenance mode has been enabled.", ephemeral=True)


@bot.slash_command(
    name="maintenance_off",
    description="Disables maintenance mode.",
    guild_ids=[GUILD_ID] if GUILD_ID else None,
)
async def maintenance_off(ctx: discord.ApplicationContext):
    global maintenance_mode, maintenance_since

    if not has_admin_role(ctx.author):
        await ctx.respond("<a:Arlert:1480274858066841842> You don't have permission.", ephemeral=True)
        return

    maintenance_mode = False
    maintenance_since = None

    embed = discord.Embed(
        title="<a:settings:1480272427819991070> Maintenance Mode Disabled",
        description="Maintenance mode has been **disabled**.",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text=f"Disabled by: {ctx.author.display_name}")
    await send_log_message(embed, ping=False)
    await update_live_embed()
    await ctx.respond("<a:checkmark:1480279615695229111> Maintenance mode has been disabled.", ephemeral=True)


@bot.slash_command(
    name="history",
    description="Shows the recent outages of the monitored bot.",
    guild_ids=[GUILD_ID] if GUILD_ID else None,
)
async def history_cmd(ctx: discord.ApplicationContext):
    if not outage_history:
        await ctx.respond("<a:Arlert:1480274858066841842> No outages recorded.", ephemeral=True)
        return

    from datetime import timedelta

    # Show last 10 outages (newest first)
    entries = outage_history[-10:][::-1]

    embed = discord.Embed(
        title="<a:settings:1480272427819991070> Outage History",
        description=f"Last {len(entries)} outages of <@{WATCHED_BOT_ID}>",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )

    for i, entry in enumerate(entries, 1):
        start = datetime.fromisoformat(entry["start"])
        end_str = ""
        if entry.get("end"):
            end = datetime.fromisoformat(entry["end"])
            end_str = f" → <t:{int(end.timestamp())}:t>"

        dur = format_duration(timedelta(seconds=entry.get("duration_seconds", 0)))

        embed.add_field(
            name=f"#{i} — {dur}",
            value=f"<t:{int(start.timestamp())}:F>{end_str}",
            inline=False,
        )

    total = len(outage_history)
    if total > 10:
        embed.set_footer(text=f"Showing 10 of {total} total outages")

    await ctx.respond(embed=embed, ephemeral=True)

@bot.slash_command(
    name="status",
    description="Change the bot status (Admin only)",
    guild_ids=[GUILD_ID] if GUILD_ID else None,
)
async def status(
    ctx,
    type: discord.Option(
        str,
        "Status type",
        choices=["playing", "watching", "listening", "custom"]
    ),
    text: discord.Option(str, "Status text")
):

    # Admin Check
    if not ctx.author.guild_permissions.administrator:
        await ctx.respond(":Arlert: You must be an administrator to use this command.", ephemeral=True)
        return

    # Status setzen
    if type == "playing":
        activity = discord.Game(name=text)

    elif type == "watching":
        activity = discord.Activity(
            type=discord.ActivityType.watching,
            name=text
        )

    elif type == "listening":
        activity = discord.Activity(
            type=discord.ActivityType.listening,
            name=text
        )

    else:
        activity = discord.CustomActivity(name=text)

    await bot.change_presence(activity=activity)

    await ctx.respond(
        f":checkmark: Status changed to **{type} {text}**",
        ephemeral=True
    )



bot.run(BOT_TOKEN)

# ============================================================
# START
# ============================================================


