
A Discord monitoring bot (Pycord) that tracks another bot's online status and displays it in a live embed.

## Features

- Live status embed (Online / Offline / Maintenance) with availability for the last 24h / 7d / 30d
- Automatic notifications on status changes
- Pings developers + status role during outages
- **Offline grace period** – short restarts/flaps don't trigger false alarms
- Outage history and availability statistics (`/history`, `/uptime`)
- Maintenance mode with an optional reason, persisted across restarts
- Outages that are running while the monitor restarts are resumed instead of lost
- Bot presence set via `/status` is restored after restarts
- Audit log of every staff action in the log channel and the log file
- Rotating log files in `logs/`
- Persistent storage of all runtime data in `status_data.json`

## Project Structure

```text
bot.py                # Main bot logic (events, status evaluation, slash commands)
settings.py           # Configuration loading + validation (env vars / .env / config.py)
storage.py            # Validated, atomic persistence of status_data.json
stats.py              # Availability statistics and formatting helpers
config.example.py     # Configuration template
requirements.txt      # Python dependencies
start.sh              # Startup script (creates a virtualenv)
```

## Requirements

- Python 3.10+
- A Discord bot token
- Enabled gateway intents in the Discord Developer Portal:
  - `PRESENCE INTENT`
  - `SERVER MEMBERS INTENT`
- Bot permissions in the embed and log channel: `View Channel`, `Send Messages`, `Embed Links`

## Installation

1. Clone the repository
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Create your configuration:

- Copy `config.example.py` to `config.py` and fill in your IDs
- Recommended: create a file named `.env` next to `bot.py` containing
  `DISCORD_BOT_TOKEN=your-token` – this keeps the token out of source files

Every setting can be provided as an environment variable, in `.env` or in `config.py`
(environment variables take precedence). Lists are comma-separated in env vars.

| Setting | Required | Default | Description |
| --- | --- | --- | --- |
| `DISCORD_BOT_TOKEN` / `BOT_TOKEN` | yes | – | Bot token |
| `WATCHED_BOT_ID` | yes | – | User ID of the bot to monitor |
| `GUILD_ID` | yes | – | Server the slash commands are registered in |
| `STATUS_EMBED_CHANNEL_ID` | yes | – | Channel of the live status embed |
| `STATUS_LOG_CHANNEL_ID` | yes | – | Channel for status change / audit logs |
| `STATUS_ROLE_ID` | no | `0` (off) | Role pinged on outages |
| `DEVELOPER_IDS` | no | `[]` | Users pinged on outages, may use staff commands |
| `ADMIN_ROLE_IDS` | no | `[]` | Roles that may use staff commands |
| `CHECK_INTERVAL_SECONDS` | no | `1200` | Fallback polling interval (60–86400) |
| `OFFLINE_GRACE_SECONDS` | no | `0` | Seconds the bot must stay offline before alerting (0–3600) |
| `MAX_HISTORY_ENTRIES` | no | `500` | Number of stored outages |
| `LEAVE_UNAUTHORIZED_GUILDS` | no | `False` | Automatically leave every server except `GUILD_ID` |
| `LOG_LEVEL` | no | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `LOG_FILE` | no | `logs/monitor.log` | Log file (`None` / `none` = console only) |
| `DATA_FILE` | no | `status_data.json` | Location of the runtime data file |

The configuration is validated at startup; the bot refuses to start and lists every problem
if a value is missing or invalid.

## Run

### Windows (PowerShell)

```powershell
python bot.py
```

### Linux/macOS

```bash
bash start.sh
```

`start.sh` creates a virtual environment (`venv/`), installs the dependencies and starts the bot.

## Slash Commands

Commands are only registered in (and only accepted from) the server configured in `GUILD_ID`.

**Everyone**

- `/history [limit]` – Show the most recent outages (1–25, default 10)
- `/uptime` – Availability for the last 24h / 7d / 30d, longest outage, average time to recover
- `/ping` – Gateway latency of the monitoring bot

**Staff** (users in `DEVELOPER_IDS`, roles in `ADMIN_ROLE_IDS` or members with the Administrator permission)

- `/maintenance_on [reason]` – Enable maintenance mode (no alerts, outages are not counted)
- `/maintenance_off` – Disable maintenance mode (alerts immediately if the bot is still offline)
- `/refresh` – Check the status right now and refresh the live embed
- `/settings` – Show the current configuration
- `/status <type> [text]` – Change this bot's presence (`playing`, `watching`, `listening`, `competing`, `custom`, `clear`)
- `/set_embed_channel <channel>` – Move the live status embed to another channel
- `/set_log_channel <channel>` – Change the log channel
- `/history_clear` – Delete the outage history (with confirmation)

To hide the staff commands from regular members in the command picker, restrict them under
*Server Settings → Integrations → your bot*. The permission checks in the bot apply regardless.

## How It Works

- Presence updates are handled directly through `on_presence_update` (activity-only changes are ignored).
- With `OFFLINE_GRACE_SECONDS` set, an outage is only reported if the bot is still offline after
  the grace period; the recorded downtime still starts at the moment it went offline.
- A fallback check runs every `CHECK_INTERVAL_SECONDS` and refreshes the live embed.
- Outages are stored in `status_data.json` (up to `MAX_HISTORY_ENTRIES` entries) and used for the
  availability statistics. Time in maintenance mode is not counted as an outage.

## Security

- **Token handling:** the token can be kept out of source files (`DISCORD_BOT_TOKEN` env var / `.env`),
  is never logged and is hidden from the settings `repr`. `config.py`, `.env`, logs and runtime data are git-ignored.
- **Guild lock:** slash commands are registered for `GUILD_ID` only and a global check rejects
  interactions from anywhere else. Optionally the bot leaves every other server (`LEAVE_UNAUTHORIZED_GUILDS`).
- **Permission checks:** all administrative commands share one central staff check; denied attempts are logged.
- **Audit log:** every staff action (maintenance, channel changes, presence, history clear) is logged
  with the user ID to the log channel and the log file.
- **Mention safety:** mentions are disabled by default; alert messages may only ping the configured
  developers and status role – never `@everyone`/`@here`, even if user input contains them.
- **Input validation:** option lengths are limited, channel changes are restricted to the configured server and
  verified against the bot's permissions.
- **Rate limiting:** every command has a per-user (or per-server) cooldown.
- **No self-modifying code:** channel changes are stored in `status_data.json` instead of rewriting `config.py`.
- **Safe persistence:** the data file is written atomically with owner-only permissions (`0600`),
  validated on load, and a corrupt file is backed up instead of crashing the bot.
- **Least privilege:** only the `guilds`, `members` and `presences` gateway intents are requested.
- **No information leaks:** unexpected errors are logged in full but users only see a generic message.

## Dependencies

- `py-cord>=2.6.1,<3`
- `python-dotenv`
- `audioop-lts` (Python 3.13+ only)
