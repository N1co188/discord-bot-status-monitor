# Copy this file to config.py and fill in your values.
# Every value can also be set as an environment variable (or in a .env file),
# which takes precedence over config.py.

# Recommended: leave this empty and set DISCORD_BOT_TOKEN as an environment
# variable / in .env instead, so the token never ends up in a source file.
BOT_TOKEN = ""

# ---- Required ----
WATCHED_BOT_ID = 0
GUILD_ID = 0                  # Server the slash commands are registered in

STATUS_EMBED_CHANNEL_ID = 0
STATUS_LOG_CHANNEL_ID = 0

# ---- Optional ----
STATUS_ROLE_ID = 0            # Role pinged on outages (0 = no role ping)
DEVELOPER_IDS = []            # Users pinged on outages; also allowed to use staff commands
ADMIN_ROLE_IDS = []           # Roles allowed to use staff commands

CHECK_INTERVAL_SECONDS = 1200 # Fallback polling interval (min. 60)
OFFLINE_GRACE_SECONDS = 60    # Wait this long before alerting (0 = alert immediately)
MAX_HISTORY_ENTRIES = 500     # Stored outages (used for /history and /uptime)

LEAVE_UNAUTHORIZED_GUILDS = False  # Leave every server except GUILD_ID

LOG_LEVEL = "INFO"            # DEBUG, INFO, WARNING, ERROR
LOG_FILE = "logs/monitor.log" # None = console only
