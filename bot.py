"""
Discord Monitoring Bot (Pycord)
Monitors the online/offline status of another bot.
Displays a self-updating live status embed, sends ping notifications
on status changes, tracks availability statistics and provides slash
commands for history, uptime, maintenance and configuration.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler

import discord
from discord.ext import commands, tasks

from settings import ConfigError, Settings, load_settings
from stats import format_duration, format_percent, summarize, uptime_percent
from storage import MAX_PRESENCE_TEXT_LENGTH, MAX_REASON_LENGTH, BotData, load_data, save_data

# ============================================================
# CONFIGURATION
# ============================================================

try:
    settings: Settings = load_settings()
except ConfigError as e:
    print(f"[FATAL] {e}", file=sys.stderr)
    sys.exit(1)

log = logging.getLogger("monitor")

EMBED_COLOR = 0x252627

# Custom emojis
E_SETTINGS = "<a:settings:1480272427819991070>"
E_ONLINE = "<a:online:1480273793833504908>"
E_OFFLINE = "<a:offline:1480273832425296038>"
E_ALERT = "<a:Arlert:1480274858066841842>"
E_SANDWATCH = "<a:Sandwatch:1480277835678613554>"
E_BUG = "<:bug:1480279132330791107>"
E_LOADING = "<a:loading:1480279699879100547>"
E_CHECK = "<a:checkmark:1480279615695229111>"

# Permissions the bot needs in the embed and log channel
REQUIRED_CHANNEL_PERMISSIONS = ("view_channel", "send_messages", "embed_links")

# ============================================================
# BOT SETUP
# ============================================================

# Least privilege: only the gateway intents the monitor actually needs
intents = discord.Intents.none()
intents.guilds = True
intents.members = True
intents.presences = True

try:
    asyncio.get_running_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

# No mentions unless a message explicitly allows them (see alert_mentions)
bot = discord.Bot(intents=intents, allowed_mentions=discord.AllowedMentions.none())

GUILD_IDS = [settings.guild_id]

# ============================================================
# STATE
# ============================================================

# Persistent data (loaded in main())
data = BotData()

last_known_online: bool | None = None

# Timestamp since which the bot has been continuously online
online_since: datetime | None = None

# Timestamp of the last check
last_checked: datetime | None = None

# Pending offline confirmation while the grace period runs
pending_offline_task: asyncio.Task | None = None

# Serialises status transitions and live embed edits
status_lock = asyncio.Lock()
embed_lock = asyncio.Lock()

startup_done = False


# ============================================================
# PERSISTENCE
# ============================================================

def persist() -> None:
    try:
        save_data(settings.data_file, data)
    except OSError as e:
        log.error("Could not save %s: %s", settings.data_file, e)


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ts(dt: datetime, style: str = "R") -> str:
    return f"<t:{int(dt.timestamp())}:{style}>"


def embed_channel_id() -> int:
    return data.embed_channel_id or settings.status_embed_channel_id


def log_channel_id() -> int:
    return data.log_channel_id or settings.status_log_channel_id


def build_ping_string() -> str:
    pings = [f"<@{uid}>" for uid in settings.developer_ids]
    if settings.status_role_id:
        pings.append(f"<@&{settings.status_role_id}>")
    return " ".join(pings)


def alert_mentions() -> discord.AllowedMentions:
    """Allows exactly the configured developers and status role to be pinged."""
    return discord.AllowedMentions(
        everyone=False,
        users=[discord.Object(uid) for uid in settings.developer_ids],
        roles=[discord.Object(settings.status_role_id)] if settings.status_role_id else False,
        replied_user=False,
    )


def is_bot_online(member: discord.Member | None) -> bool:
    if member is None:
        return False
    return member.status in (discord.Status.online, discord.Status.idle, discord.Status.dnd)


def find_watched_member() -> discord.Member | None:
    guild = bot.get_guild(settings.guild_id)
    if guild is not None:
        member = guild.get_member(settings.watched_bot_id)
        if member is not None:
            return member
    for guild in bot.guilds:
        member = guild.get_member(settings.watched_bot_id)
        if member is not None:
            return member
    return None


def is_staff(user: discord.abc.User) -> bool:
    """Developers, members with an admin role and server administrators."""
    if user.id in settings.developer_ids:
        return True
    if not isinstance(user, discord.Member) or user.guild.id != settings.guild_id:
        return False
    if user.guild_permissions.administrator:
        return True
    return any(role.id in settings.admin_role_ids for role in user.roles)


def missing_permissions(channel: discord.abc.GuildChannel) -> list[str]:
    me = channel.guild.me
    if me is None:
        return []
    perms = channel.permissions_for(me)
    return [name for name in REQUIRED_CHANNEL_PERMISSIONS if not getattr(perms, name)]


async def resolve_channel(channel_id: int) -> discord.abc.Messageable | None:
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.HTTPException as e:
            log.warning("Channel %s not found or not accessible: %s", channel_id, e)
            return None
    if not isinstance(channel, discord.abc.Messageable):
        log.warning("Channel %s is not a text channel.", channel_id)
        return None
    return channel


async def send_log_message(embed: discord.Embed, ping: bool = True) -> None:
    """Sends a log message (down/up/maintenance/audit) to the log channel."""
    channel = await resolve_channel(log_channel_id())
    if channel is None:
        return
    content = build_ping_string() if ping else ""
    try:
        await channel.send(
            content=content or None,
            embed=embed,
            allowed_mentions=alert_mentions() if ping else discord.AllowedMentions.none(),
        )
    except discord.HTTPException as e:
        log.error("Could not send log message: %s", e)


async def send_alert_ping() -> None:
    """Sends a ping in the embed channel while the bot is offline."""
    if data.alert_ping_message_id is not None:
        return
    channel = await resolve_channel(embed_channel_id())
    if channel is None:
        return
    if settings.status_role_id:
        target = f"<@&{settings.status_role_id}>"
    else:
        target = " ".join(f"<@{uid}>" for uid in settings.developer_ids)
    content = f"⚠️ {target} — Bot is **offline**!" if target else "⚠️ Bot is **offline**!"
    try:
        msg = await channel.send(content, allowed_mentions=alert_mentions())
    except discord.HTTPException as e:
        log.error("Could not send alert ping: %s", e)
        return
    data.alert_ping_message_id = msg.id
    persist()


async def delete_alert_ping(channel_id: int | None = None) -> None:
    """Deletes the alert ping message from the embed channel."""
    if data.alert_ping_message_id is None:
        return
    channel = await resolve_channel(channel_id or embed_channel_id())
    if channel is not None:
        try:
            await channel.get_partial_message(data.alert_ping_message_id).delete()
        except discord.HTTPException:
            pass
    data.alert_ping_message_id = None
    persist()


async def audit(ctx: discord.ApplicationContext, title: str, description: str | None = None, *, ping: bool = False) -> discord.Embed:
    """Logs a staff action to the log file and the log channel."""
    log.info("AUDIT %s (%s): %s%s", ctx.author, ctx.author.id, title, f" — {description}" if description else "")
    embed = discord.Embed(title=title, description=description, color=EMBED_COLOR, timestamp=utcnow())
    embed.set_footer(text=f"By: {ctx.author.display_name} ({ctx.author.id})")
    await send_log_message(embed, ping=ping)
    return embed


# ============================================================
# LIVE-STATUS-EMBED
# ============================================================

def availability(window: timedelta, now: datetime | None = None) -> float | None:
    now = now or utcnow()
    ongoing = data.outage_start if last_known_online is False else None
    return uptime_percent(data.outage_history, now, window, data.monitoring_since, ongoing)


def build_live_embed() -> discord.Embed:
    """Builds the central live status embed."""
    now = utcnow()
    watched = f"<@{settings.watched_bot_id}>"

    if data.maintenance_mode:
        embed = discord.Embed(
            title=f"{E_SETTINGS} Maintenance Mode",
            description=f"{watched} is currently under **maintenance**.",
            color=EMBED_COLOR,
        )
        if data.maintenance_reason:
            embed.add_field(name="📝 Reason:", value=data.maintenance_reason, inline=False)
    elif last_known_online is None:
        embed = discord.Embed(
            title=f"{E_LOADING} Checking…",
            description=f"The status of {watched} is being checked.",
            color=EMBED_COLOR,
        )
    elif last_known_online:
        embed = discord.Embed(
            title=f"{E_ONLINE} Online",
            description=f"{watched} is **online** and operational.",
            color=EMBED_COLOR,
        )
    else:
        embed = discord.Embed(
            title=f"{E_OFFLINE} Offline!",
            description=f"▬▬▬▬▬▬▬`Status:`▬▬▬▬▬▬▬\n{watched} is currently **__unavailable__**!\n {E_ALERT} The developers have been notified\n ▬▬▬▬▬▬▬`Infos:`▬▬▬▬▬▬▬\n",
            color=EMBED_COLOR,
        )

    # Uptime / Downtime / Maintenance duration
    if data.maintenance_mode and data.maintenance_since:
        embed.add_field(name=f"{E_SANDWATCH} Maintenance since:", value=ts(data.maintenance_since), inline=True)
    elif last_known_online and online_since:
        embed.add_field(name=f"{E_SANDWATCH} Uptime:", value=ts(online_since), inline=True)
    elif last_known_online is False and data.offline_since:
        embed.add_field(name=f"{E_SANDWATCH} Downtime:", value=ts(data.offline_since), inline=True)

    # Last outage from history
    if data.outage_history:
        embed.add_field(name=f"{E_BUG} Last Outage:", value=ts(data.outage_history[-1].end), inline=True)

    # Last checked
    if last_checked:
        embed.add_field(name=f"{E_LOADING} Last Checked:", value=ts(last_checked), inline=True)

    # Availability
    for label, window in (("24h", timedelta(days=1)), ("7d", timedelta(days=7)), ("30d", timedelta(days=30))):
        embed.add_field(name=f"📊 Availability {label}:", value=f"`{format_percent(availability(window, now))}`", inline=True)

    embed.set_footer(text=f"Live Status • Checked every {format_duration(settings.check_interval_seconds)}")
    embed.timestamp = now
    return embed


async def update_live_embed() -> None:
    """Updates the live status embed (or creates a new one)."""
    async with embed_lock:
        channel = await resolve_channel(embed_channel_id())
        if channel is None:
            return

        embed = build_live_embed()

        # Try to edit the existing message
        if data.status_message_id is not None:
            try:
                await channel.get_partial_message(data.status_message_id).edit(embed=embed)
                return
            except discord.NotFound:
                log.info("Live embed message not found, creating a new one.")
                data.status_message_id = None
            except discord.HTTPException as e:
                # Transient error: don't post a duplicate embed
                log.warning("Could not edit live embed: %s", e)
                return

        # Send a new message
        try:
            msg = await channel.send(embed=embed)
        except discord.HTTPException as e:
            log.error("Could not send live embed: %s", e)
            return
        data.status_message_id = msg.id
        persist()
        log.info("Live status embed created (Message ID: %s)", msg.id)


# ============================================================
# STATUS EVALUATION
# ============================================================

async def evaluate_status(member: discord.Member | None) -> bool:
    """Applies the current status of the watched bot. Returns True if it changed."""
    async with status_lock:
        return await _apply_status(is_bot_online(member))


async def _apply_status(online: bool) -> bool:
    global last_known_online, online_since

    now = utcnow()

    # First run — set initial state
    if last_known_online is None:
        if online:
            last_known_online = True
            online_since = now
            if data.offline_since is not None:
                log.info("Watched bot recovered while the monitor was offline; discarding unfinished outage.")
                data.offline_since = None
                data.outage_start = None
                persist()
            await delete_alert_ping()
            log.info("Initial status: online")
            return True

        if data.offline_since is not None:
            # Resume the outage that was running before the monitor restarted
            last_known_online = False
            if not data.maintenance_mode:
                await send_alert_ping()
            log.info("Initial status: offline (resuming outage since %s)", data.offline_since.isoformat())
            return True

        log.info("Initial status: offline")
        await mark_offline(now)
        return True

    if online:
        cancel_pending_offline("watched bot came back within the grace period")
        if last_known_online:
            return False
        await mark_online(now)
        return True

    if last_known_online is False:
        return False

    if settings.offline_grace_seconds > 0:
        schedule_offline_confirmation(now)
        return False

    await mark_offline(now)
    return True


async def mark_offline(went_offline_at: datetime) -> None:
    global last_known_online, online_since

    log.info("Status change: watched bot is OFFLINE")
    last_known_online = False
    online_since = None
    data.offline_since = went_offline_at
    data.outage_start = None if data.maintenance_mode else went_offline_at
    persist()

    embed = discord.Embed(
        title=f"{E_ALERT} Bot Offline",
        description=f"<@{settings.watched_bot_id}> is currently **__unavailable__**!",
        color=EMBED_COLOR,
        timestamp=went_offline_at,
    )
    embed.add_field(name=f"{E_SANDWATCH} Down Since:", value=ts(went_offline_at, "F"), inline=False)
    if data.maintenance_mode:
        embed.set_footer(text="Maintenance mode is active — no alert sent.")
    await send_log_message(embed, ping=not data.maintenance_mode)
    if not data.maintenance_mode:
        await send_alert_ping()


async def mark_online(now: datetime) -> None:
    global last_known_online, online_since

    log.info("Status change: watched bot is ONLINE")
    last_known_online = True
    online_since = now
    offline_since = data.offline_since
    await delete_alert_ping()

    embed = discord.Embed(
        title=f"{E_ONLINE} Bot Online",
        description=f"<@{settings.watched_bot_id}> is back **online**.",
        color=EMBED_COLOR,
        timestamp=now,
    )
    if offline_since is not None:
        embed.add_field(name=f"{E_SANDWATCH} Downtime:", value=format_duration(now - offline_since), inline=True)
        embed.add_field(name=f"{E_OFFLINE} Offline Since:", value=ts(offline_since, "F"), inline=True)
        embed.add_field(name=f"{E_ONLINE} Back Online:", value=ts(now, "F"), inline=True)

    # Save to history (outage_start is None during maintenance)
    if data.outage_start is not None:
        data.add_outage(data.outage_start, now, settings.max_history_entries)
    data.offline_since = None
    data.outage_start = None
    persist()

    await send_log_message(embed, ping=not data.maintenance_mode)


def schedule_offline_confirmation(went_offline_at: datetime) -> None:
    global pending_offline_task
    if pending_offline_task is not None and not pending_offline_task.done():
        return
    log.info("Watched bot appears offline, confirming in %ss.", settings.offline_grace_seconds)
    pending_offline_task = asyncio.create_task(confirm_offline(went_offline_at))


def cancel_pending_offline(reason: str) -> None:
    global pending_offline_task
    if pending_offline_task is not None and not pending_offline_task.done():
        pending_offline_task.cancel()
        log.info("Offline alert cancelled: %s.", reason)
    pending_offline_task = None


async def confirm_offline(went_offline_at: datetime) -> None:
    """Raises the offline alert only if the bot is still offline after the grace period."""
    global pending_offline_task
    await asyncio.sleep(settings.offline_grace_seconds)
    try:
        async with status_lock:
            pending_offline_task = None
            if last_known_online is False or is_bot_online(find_watched_member()):
                return
            await mark_offline(went_offline_at)
        await update_live_embed()
    except Exception:
        log.exception("Offline confirmation failed")


# ============================================================
# EVENTS
# ============================================================

async def leave_if_unauthorized(guild: discord.Guild) -> None:
    if not settings.leave_unauthorized_guilds or guild.id == settings.guild_id:
        return
    log.warning("Leaving unauthorized guild %s (%s).", guild.name, guild.id)
    try:
        await guild.leave()
    except discord.HTTPException as e:
        log.error("Could not leave guild %s: %s", guild.id, e)


async def restore_presence() -> None:
    if data.presence_type and data.presence_text:
        try:
            await bot.change_presence(activity=build_activity(data.presence_type, data.presence_text))
        except Exception as e:
            log.warning("Could not restore presence: %s", e)


def check_setup() -> None:
    """Logs configuration problems that would silently break alerts."""
    guild = bot.get_guild(settings.guild_id)
    if guild is None:
        log.error("The bot is not a member of the configured guild %s!", settings.guild_id)
        return

    for label, channel_id in (("Embed", embed_channel_id()), ("Log", log_channel_id())):
        channel = bot.get_channel(channel_id)
        if channel is None:
            log.error("%s channel %s NOT found!", label, channel_id)
            continue
        log.info("%s channel found: %s (%s)", label, channel.name, channel.id)
        if isinstance(channel, discord.abc.GuildChannel):
            if channel.guild.id != settings.guild_id:
                log.warning("%s channel %s is not in the configured guild.", label, channel_id)
            missing = missing_permissions(channel)
            if missing:
                log.error("Missing permissions in %s channel: %s", label.lower(), ", ".join(missing))

    if settings.status_role_id:
        role = guild.get_role(settings.status_role_id)
        if role is None:
            log.error("Status role %s NOT found!", settings.status_role_id)
        elif not role.mentionable and not guild.me.guild_permissions.mention_everyone:
            log.warning("Status role %s is not mentionable — alert pings won't notify anyone.", role.name)

    member = find_watched_member()
    if member is not None:
        log.info("Watched bot found: %s — Status: %s", member, member.status)
    else:
        log.warning("Watched bot %s not found!", settings.watched_bot_id)


@bot.event
async def on_ready():
    global startup_done

    log.info("Monitoring bot connected as %s (ID: %s)", bot.user, bot.user.id)

    if startup_done:
        # Reconnect: presence updates may have been missed
        if await evaluate_status(find_watched_member()):
            await update_live_embed()
        return
    startup_done = True

    log.info("Watching bot ID: %s", settings.watched_bot_id)

    for guild in list(bot.guilds):
        await leave_if_unauthorized(guild)

    for guild in bot.guilds:
        if not guild.chunked:
            log.info("Loading member cache for guild: %s (%s)", guild.name, guild.id)
            try:
                await guild.chunk()
            except Exception as e:
                log.warning("Chunk for %s failed: %s", guild.name, e)

    check_setup()
    await restore_presence()

    if not status_check_loop.is_running():
        status_check_loop.start()

    log.info("Bot is ready.")


@bot.event
async def on_guild_join(guild: discord.Guild):
    log.info("Joined guild %s (%s).", guild.name, guild.id)
    await leave_if_unauthorized(guild)


@bot.event
async def on_presence_update(before: discord.Member, after: discord.Member):
    if after.id != settings.watched_bot_id:
        return
    # Ignore activity-only changes (e.g. rotating status texts)
    if before.status == after.status:
        return
    log.debug("Presence update: %s -> %s", before.status, after.status)
    if await evaluate_status(after):
        await update_live_embed()


# ============================================================
# BACKGROUND TASK (Fallback Polling + Live Embed Update)
# ============================================================

@tasks.loop(seconds=settings.check_interval_seconds)
async def status_check_loop():
    global last_checked
    # An unhandled exception would stop the loop for good
    try:
        last_checked = utcnow()
        await evaluate_status(find_watched_member())
        await update_live_embed()
    except Exception:
        log.exception("Status check failed")


@status_check_loop.before_loop
async def before_status_check():
    await bot.wait_until_ready()


# ============================================================
# PERMISSIONS & ERROR HANDLING
# ============================================================

class NotStaff(discord.CheckFailure):
    pass


class WrongGuild(discord.CheckFailure):
    pass


@bot.check
async def only_configured_guild(ctx: discord.ApplicationContext) -> bool:
    # Commands are registered for GUILD_ID only; this is defense in depth
    if ctx.guild_id != settings.guild_id:
        raise WrongGuild()
    return True


async def staff_check(ctx: discord.ApplicationContext) -> bool:
    if not is_staff(ctx.author):
        raise NotStaff()
    return True


def public_command(**kwargs):
    return bot.slash_command(guild_ids=GUILD_IDS, **kwargs)


def staff_command(**kwargs):
    return bot.slash_command(guild_ids=GUILD_IDS, checks=[staff_check], **kwargs)


@bot.event
async def on_application_command_error(ctx: discord.ApplicationContext, error: discord.DiscordException):
    command = ctx.command.qualified_name if ctx.command else "?"
    if isinstance(error, commands.CommandOnCooldown):
        message = f"{E_ALERT} Slow down! Try again in {error.retry_after:.0f}s."
    elif isinstance(error, NotStaff):
        log.warning("Denied /%s for %s (%s).", command, ctx.author, ctx.author.id)
        message = f"{E_ALERT} You don't have permission."
    elif isinstance(error, discord.CheckFailure):
        message = f"{E_ALERT} This command can't be used here."
    else:
        original = getattr(error, "original", error)
        log.error("Error in /%s", command, exc_info=(type(original), original, original.__traceback__))
        # Never leak internal details to users
        message = f"{E_ALERT} An unexpected error occurred. It has been logged."
    try:
        await ctx.respond(message, ephemeral=True)
    except discord.HTTPException:
        pass


class ConfirmView(discord.ui.View):
    """Two-button confirmation that only the invoking user can answer."""

    def __init__(self, author_id: int, confirm_label: str):
        super().__init__(timeout=30)
        self.author_id = author_id
        self.confirmed = False
        self.children[0].label = confirm_label

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user is not None and interaction.user.id == self.author_id

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.confirmed = True
        await interaction.response.edit_message(view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.edit_message(content="Cancelled.", view=None)
        self.stop()


# ============================================================
# SLASH-COMMANDS: MAINTENANCE
# ============================================================

@staff_command(name="maintenance_on", description="Enables maintenance mode.")
@commands.cooldown(1, 5, commands.BucketType.user)
async def maintenance_on(
    ctx: discord.ApplicationContext,
    reason: discord.Option(str, "Reason shown in the status embed", required=False, max_length=MAX_REASON_LENGTH, default=None),
):
    await ctx.defer(ephemeral=True)

    async with status_lock:
        if data.maintenance_mode:
            await ctx.respond(f"{E_ALERT} Maintenance mode is already enabled.", ephemeral=True)
            return
        now = utcnow()
        data.maintenance_mode = True
        data.maintenance_since = now
        data.maintenance_reason = reason.strip() if reason and reason.strip() else None
        data.maintenance_by = ctx.author.id
        # An outage that is already running ends where maintenance begins
        if last_known_online is False and data.outage_start is not None:
            data.add_outage(data.outage_start, now, settings.max_history_entries)
            data.outage_start = None
        persist()
        await delete_alert_ping()

    await audit(
        ctx,
        f"{E_SETTINGS} Maintenance Mode Enabled",
        "The monitored bot is currently under **maintenance**."
        + (f"\n**Reason:** {data.maintenance_reason}" if data.maintenance_reason else ""),
    )
    await update_live_embed()
    await ctx.respond(f"{E_CHECK} Maintenance mode has been enabled.", ephemeral=True)


@staff_command(name="maintenance_off", description="Disables maintenance mode.")
@commands.cooldown(1, 5, commands.BucketType.user)
async def maintenance_off(ctx: discord.ApplicationContext):
    await ctx.defer(ephemeral=True)

    async with status_lock:
        if not data.maintenance_mode:
            await ctx.respond(f"{E_ALERT} Maintenance mode is not enabled.", ephemeral=True)
            return
        data.maintenance_mode = False
        data.maintenance_since = None
        data.maintenance_reason = None
        data.maintenance_by = None
        # Still offline after maintenance: that is a real outage from now on
        still_offline = last_known_online is False
        if still_offline:
            data.outage_start = utcnow()
        persist()

    description = "Maintenance mode has been **disabled**."
    if still_offline:
        description += f"\n{E_ALERT} <@{settings.watched_bot_id}> is still **offline**!"
    await audit(ctx, f"{E_SETTINGS} Maintenance Mode Disabled", description, ping=still_offline)
    if still_offline:
        await send_alert_ping()
    await update_live_embed()
    await ctx.respond(f"{E_CHECK} Maintenance mode has been disabled.", ephemeral=True)


# ============================================================
# SLASH-COMMANDS: INFORMATION
# ============================================================

@public_command(name="history", description="Shows the recent outages of the monitored bot.")
@commands.cooldown(1, 5, commands.BucketType.user)
async def history_cmd(
    ctx: discord.ApplicationContext,
    limit: discord.Option(int, "Number of outages to show", required=False, min_value=1, max_value=25, default=10),
):
    if not data.outage_history:
        await ctx.respond(f"{E_ALERT} No outages recorded.", ephemeral=True)
        return

    # Newest first
    entries = data.outage_history[-limit:][::-1]

    embed = discord.Embed(
        title=f"{E_SETTINGS} Outage History",
        description=f"Last {len(entries)} outages of <@{settings.watched_bot_id}>",
        color=discord.Color.blurple(),
        timestamp=utcnow(),
    )
    for i, outage in enumerate(entries, 1):
        embed.add_field(
            name=f"#{i} — {format_duration(outage.duration_seconds)}",
            value=f"{ts(outage.start, 'F')} → {ts(outage.end, 't')}",
            inline=False,
        )

    total = len(data.outage_history)
    if total > len(entries):
        embed.set_footer(text=f"Showing {len(entries)} of {total} total outages")

    await ctx.respond(embed=embed, ephemeral=True)


@public_command(name="uptime", description="Shows availability statistics of the monitored bot.")
@commands.cooldown(1, 5, commands.BucketType.user)
async def uptime_cmd(ctx: discord.ApplicationContext):
    now = utcnow()

    if data.maintenance_mode:
        current = f"{E_SETTINGS} Maintenance"
    elif last_known_online:
        current = f"{E_ONLINE} Online" + (f" since {ts(online_since)}" if online_since else "")
    elif last_known_online is False:
        current = f"{E_OFFLINE} Offline" + (f" since {ts(data.offline_since)}" if data.offline_since else "")
    else:
        current = f"{E_LOADING} Unknown"

    embed = discord.Embed(
        title="📊 Availability of the monitored bot",
        description=f"<@{settings.watched_bot_id}> — {current}",
        color=EMBED_COLOR,
        timestamp=now,
    )
    for label, days in (("24 hours", 1), ("7 days", 7), ("30 days", 30)):
        window = timedelta(days=days)
        summary = summarize(data.outage_history, since=now - window)
        embed.add_field(
            name=f"Last {label}",
            value=f"`{format_percent(availability(window, now))}`\n{summary.count} outage(s)",
            inline=True,
        )

    overall = summarize(data.outage_history)
    if overall.count:
        embed.add_field(name="Recorded outages", value=str(overall.count), inline=True)
        embed.add_field(name="Longest outage", value=format_duration(overall.longest_seconds), inline=True)
        embed.add_field(name="Avg. time to recover", value=format_duration(overall.average_seconds), inline=True)
    if data.monitoring_since:
        embed.set_footer(text=f"Monitoring since {data.monitoring_since:%Y-%m-%d %H:%M} UTC")

    await ctx.respond(embed=embed, ephemeral=True)


@public_command(name="ping", description="Shows the latency of the monitoring bot.")
@commands.cooldown(1, 5, commands.BucketType.user)
async def ping_cmd(ctx: discord.ApplicationContext):
    await ctx.respond(f"🏓 Pong! Gateway latency: **{bot.latency * 1000:.0f} ms**", ephemeral=True)


# ============================================================
# SLASH-COMMANDS: ADMINISTRATION
# ============================================================

@staff_command(name="refresh", description="Checks the status right now and refreshes the live embed.")
@commands.cooldown(1, 30, commands.BucketType.guild)
async def refresh_cmd(ctx: discord.ApplicationContext):
    global last_checked
    await ctx.defer(ephemeral=True)
    last_checked = utcnow()
    member = find_watched_member()
    await evaluate_status(member)
    await update_live_embed()
    status = member.status if member is not None else "not found"
    await ctx.respond(f"{E_CHECK} Status checked (`{status}`) and live embed refreshed.", ephemeral=True)


@staff_command(name="settings", description="Shows the current monitor configuration.")
@commands.cooldown(1, 5, commands.BucketType.user)
async def settings_cmd(ctx: discord.ApplicationContext):
    def mentions(ids: tuple[int, ...], fmt: str) -> str:
        return ", ".join(fmt.format(i) for i in ids) or "—"

    embed = discord.Embed(title=f"{E_SETTINGS} Monitor Settings", color=EMBED_COLOR, timestamp=utcnow())
    embed.add_field(name="Watched bot", value=f"<@{settings.watched_bot_id}>", inline=True)
    embed.add_field(name="Embed channel", value=f"<#{embed_channel_id()}>", inline=True)
    embed.add_field(name="Log channel", value=f"<#{log_channel_id()}>", inline=True)
    embed.add_field(name="Status role", value=f"<@&{settings.status_role_id}>" if settings.status_role_id else "—", inline=True)
    embed.add_field(name="Developers", value=mentions(settings.developer_ids, "<@{}>"), inline=True)
    embed.add_field(name="Admin roles", value=mentions(settings.admin_role_ids, "<@&{}>"), inline=True)
    embed.add_field(name="Check interval", value=format_duration(settings.check_interval_seconds), inline=True)
    embed.add_field(
        name="Offline grace period",
        value=format_duration(settings.offline_grace_seconds) if settings.offline_grace_seconds else "off",
        inline=True,
    )
    embed.add_field(name="Leave foreign guilds", value="yes" if settings.leave_unauthorized_guilds else "no", inline=True)
    maintenance = "off"
    if data.maintenance_mode:
        maintenance = f"on since {ts(data.maintenance_since)}" if data.maintenance_since else "on"
        if data.maintenance_by:
            maintenance += f" by <@{data.maintenance_by}>"
    embed.add_field(name="Maintenance", value=maintenance, inline=True)
    embed.add_field(name="History entries", value=f"{len(data.outage_history)} / {settings.max_history_entries}", inline=True)
    await ctx.respond(embed=embed, ephemeral=True)


@staff_command(name="history_clear", description="Deletes the complete outage history.")
@commands.cooldown(1, 30, commands.BucketType.user)
async def history_clear(ctx: discord.ApplicationContext):
    count = len(data.outage_history)
    if not count:
        await ctx.respond(f"{E_ALERT} The outage history is already empty.", ephemeral=True)
        return

    view = ConfirmView(ctx.author.id, confirm_label=f"Delete {count} outage(s)")
    await ctx.respond(
        f"{E_ALERT} This permanently deletes **{count}** recorded outage(s) and resets the availability statistics.",
        view=view,
        ephemeral=True,
    )
    timed_out = await view.wait()
    if not view.confirmed:
        if timed_out:
            await ctx.edit(content="Timed out — nothing was deleted.", view=None)
        return

    data.outage_history.clear()
    data.monitoring_since = utcnow()
    persist()
    await audit(ctx, f"{E_SETTINGS} Outage History Cleared", f"{count} outage(s) were deleted.")
    await update_live_embed()
    await ctx.edit(content=f"{E_CHECK} Deleted {count} outage(s).", view=None)


def build_activity(kind: str, text: str) -> discord.BaseActivity:
    if kind == "playing":
        return discord.Game(name=text)
    if kind == "watching":
        return discord.Activity(type=discord.ActivityType.watching, name=text)
    if kind == "listening":
        return discord.Activity(type=discord.ActivityType.listening, name=text)
    if kind == "competing":
        return discord.Activity(type=discord.ActivityType.competing, name=text)
    return discord.CustomActivity(name=text)


@staff_command(name="status", description="Change the presence of this monitoring bot.")
@commands.cooldown(1, 15, commands.BucketType.user)
async def status(
    ctx: discord.ApplicationContext,
    type: discord.Option(str, "Status type", choices=["playing", "watching", "listening", "competing", "custom", "clear"]),
    text: discord.Option(str, "Status text", required=False, max_length=MAX_PRESENCE_TEXT_LENGTH, default=None),
):
    text = text.strip() if text else ""

    if type == "clear":
        await bot.change_presence(activity=None)
        data.presence_type = None
        data.presence_text = None
        persist()
        await audit(ctx, f"{E_SETTINGS} Presence Cleared")
        await ctx.respond(f"{E_CHECK} Status cleared.", ephemeral=True)
        return

    if not text:
        await ctx.respond(f"{E_ALERT} Please provide a status text.", ephemeral=True)
        return

    await bot.change_presence(activity=build_activity(type, text))
    data.presence_type = type
    data.presence_text = text
    persist()
    await audit(ctx, f"{E_SETTINGS} Presence Changed", f"**{type}** {discord.utils.escape_markdown(text)}")
    await ctx.respond(f"{E_CHECK} Status changed to **{type} {discord.utils.escape_markdown(text)}**", ephemeral=True)


async def validate_target_channel(ctx: discord.ApplicationContext, channel: discord.abc.GuildChannel) -> bool:
    if channel.guild.id != settings.guild_id:
        await ctx.respond(f"{E_ALERT} The channel must be in this server.", ephemeral=True)
        return False
    missing = missing_permissions(channel)
    if missing:
        await ctx.respond(
            f"{E_ALERT} I'm missing these permissions in {channel.mention}: `{', '.join(missing)}`",
            ephemeral=True,
        )
        return False
    return True


@staff_command(name="set_embed_channel", description="Set the channel where the live status embed is posted.")
@commands.cooldown(1, 15, commands.BucketType.guild)
async def set_embed_channel(
    ctx: discord.ApplicationContext,
    channel: discord.Option(
        discord.abc.GuildChannel,
        "Select a channel",
        channel_types=[discord.ChannelType.text, discord.ChannelType.news],
    ),
):
    if not await validate_target_channel(ctx, channel):
        return
    old_channel_id = embed_channel_id()
    if channel.id == old_channel_id:
        await ctx.respond(f"{E_ALERT} {channel.mention} is already the embed channel.", ephemeral=True)
        return
    await ctx.defer(ephemeral=True)

    # Remove the old live embed and alert ping so nothing stale is left behind
    async with embed_lock:
        if data.status_message_id is not None:
            old_channel = await resolve_channel(old_channel_id)
            if old_channel is not None:
                try:
                    await old_channel.get_partial_message(data.status_message_id).delete()
                except discord.HTTPException:
                    pass
        data.status_message_id = None
    had_alert = data.alert_ping_message_id is not None
    await delete_alert_ping(old_channel_id)

    data.embed_channel_id = channel.id
    persist()

    if had_alert:
        await send_alert_ping()
    await update_live_embed()
    await audit(ctx, f"{E_SETTINGS} Embed Channel Changed", f"<#{old_channel_id}> → {channel.mention}")
    await ctx.respond(f"{E_CHECK} Live status embed channel set to {channel.mention}.", ephemeral=True)


@staff_command(name="set_log_channel", description="Set the channel where status changes are logged.")
@commands.cooldown(1, 15, commands.BucketType.guild)
async def set_log_channel(
    ctx: discord.ApplicationContext,
    channel: discord.Option(
        discord.abc.GuildChannel,
        "Select a channel",
        channel_types=[discord.ChannelType.text, discord.ChannelType.news],
    ),
):
    if not await validate_target_channel(ctx, channel):
        return
    old_channel_id = log_channel_id()
    if channel.id == old_channel_id:
        await ctx.respond(f"{E_ALERT} {channel.mention} is already the log channel.", ephemeral=True)
        return
    await ctx.defer(ephemeral=True)

    data.log_channel_id = channel.id
    persist()
    await audit(ctx, f"{E_SETTINGS} Log Channel Changed", f"<#{old_channel_id}> → {channel.mention}")
    await ctx.respond(f"{E_CHECK} Log channel set to {channel.mention}.", ephemeral=True)


# ============================================================
# START
# ============================================================

def setup_logging() -> None:
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(settings.log_level)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    if settings.log_file is not None:
        try:
            settings.log_file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(settings.log_file, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
        except OSError as e:
            log.warning("File logging disabled: %s", e)
        else:
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)

    # discord.py internals are noisy on DEBUG/INFO
    logging.getLogger("discord").setLevel(max(logging.WARNING, root.level))


def main() -> None:
    global data

    setup_logging()

    try:
        data = load_data(settings.data_file, settings.max_history_entries)
    except OSError as e:
        log.critical("Could not read %s: %s", settings.data_file, e)
        sys.exit(1)

    if data.monitoring_since is None:
        # Older data files: statistics start with the first recorded outage
        data.monitoring_since = data.outage_history[0].start if data.outage_history else utcnow()
        persist()

    try:
        bot.run(settings.bot_token)
    except discord.LoginFailure:
        log.critical("Login failed: the bot token is invalid. Check DISCORD_BOT_TOKEN / BOT_TOKEN.")
        sys.exit(1)
    except discord.PrivilegedIntentsRequired:
        log.critical("Enable the PRESENCE and SERVER MEMBERS intents in the Discord Developer Portal.")
        sys.exit(1)


if __name__ == "__main__":
    main()
