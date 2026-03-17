
A Discord monitoring bot (Pycord) that tracks another bot's online status and displays it in a live embed.

## Features

- Live status embed (Online / Offline / Maintenance)
- Automatic notifications on status changes
- Pings developers + status role during outages
- Slash command for outage history
- Maintenance mode (with admin roles)
- Persistent storage of embed/history data in `status_data.json`

## Project Structure

```text
bot.py                # Main bot logic
config.example.py     # Configuration template
requirements.txt      # Python dependencies
start.sh              # Simple startup script
status_data.json      # Runtime data (managed automatically)
```

## Requirements

- Python 3.10+
- A Discord bot token
- Enabled gateway intents in the Discord Developer Portal:
  - `PRESENCE INTENT`
  - `SERVER MEMBERS INTENT`

## Installation

1. Clone the repository
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Create your configuration:

- Copy `config.example.py` to `config.py`
- Fill in your values in `config.py`

Example:

```python
BOT_TOKEN = ""
WATCHED_BOT_ID = 0

STATUS_EMBED_CHANNEL_ID = 0
STATUS_LOG_CHANNEL_ID = 0

STATUS_ROLE_ID = 0
DEVELOPER_IDS = []
ADMIN_ROLE_IDS = []
GUILD_ID = 0
```

## Run

### Windows (PowerShell)

```powershell
python bot.py
```

### Linux/macOS

```bash
bash start.sh
```

## Slash Commands

- `/maintenance_on` – Enable maintenance mode (only roles in `ADMIN_ROLE_IDS`)
- `/maintenance_off` – Disable maintenance mode (only roles in `ADMIN_ROLE_IDS`)
- `/history` – Show recent outages
- `/status` – Change bot presence (Discord administrators only)

## How It Works

- Presence updates are handled directly through `on_presence_update`.
- A fallback check runs every 20 minutes (`CHECK_INTERVAL_SECONDS = 1200`).
- A central status embed is continuously updated in the configured channel.
- Outages are stored in `status_data.json` (up to 50 entries).

## Security

- `config.py` is listed in `.gitignore` and should never be pushed.
- Keep token and IDs local or in secure secrets.

## Dependencies

- `py-cord>=2.6.0`
- `audioop-lts`

