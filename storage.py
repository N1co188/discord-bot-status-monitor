"""
Persistent runtime data (status_data.json).

The file is written atomically (temp file + rename) with owner-only
permissions, and everything read back from it is validated so a damaged or
tampered file can't crash the bot or inject unexpected types.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("monitor.storage")

MAX_SNOWFLAKE = 2**64 - 1
MAX_REASON_LENGTH = 200
MAX_PRESENCE_TEXT_LENGTH = 128
PRESENCE_TYPES = ("playing", "watching", "listening", "competing", "custom")


@dataclass
class Outage:
    start: datetime
    end: datetime

    @property
    def duration_seconds(self) -> int:
        return int((self.end - self.start).total_seconds())

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "duration_seconds": self.duration_seconds,
        }


@dataclass
class BotData:
    # Live embed + alert ping messages
    status_message_id: int | None = None
    alert_ping_message_id: int | None = None

    # Channels changed via slash commands (override config values)
    embed_channel_id: int | None = None
    log_channel_id: int | None = None

    # Maintenance mode (survives restarts)
    maintenance_mode: bool = False
    maintenance_since: datetime | None = None
    maintenance_reason: str | None = None
    maintenance_by: int | None = None

    # Current outage (survives restarts)
    offline_since: datetime | None = None
    outage_start: datetime | None = None

    # Start of monitoring, used for availability statistics
    monitoring_since: datetime | None = None

    # Presence set via /status
    presence_type: str | None = None
    presence_text: str | None = None

    outage_history: list[Outage] = field(default_factory=list)

    def add_outage(self, start: datetime, end: datetime, max_entries: int) -> Outage:
        outage = Outage(start=start, end=max(start, end))
        self.outage_history.append(outage)
        if len(self.outage_history) > max_entries:
            del self.outage_history[: len(self.outage_history) - max_entries]
        return outage

    def to_dict(self) -> dict[str, Any]:
        return {
            "status_message_id": self.status_message_id,
            "alert_ping_message_id": self.alert_ping_message_id,
            "embed_channel_id": self.embed_channel_id,
            "log_channel_id": self.log_channel_id,
            "maintenance_mode": self.maintenance_mode,
            "maintenance_since": _dt_to_str(self.maintenance_since),
            "maintenance_reason": self.maintenance_reason,
            "maintenance_by": self.maintenance_by,
            "offline_since": _dt_to_str(self.offline_since),
            "outage_start": _dt_to_str(self.outage_start),
            "monitoring_since": _dt_to_str(self.monitoring_since),
            "presence_type": self.presence_type,
            "presence_text": self.presence_text,
            "outage_history": [o.to_dict() for o in self.outage_history],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any], max_history: int) -> BotData:
        data = cls(
            status_message_id=_snowflake(raw.get("status_message_id")),
            alert_ping_message_id=_snowflake(raw.get("alert_ping_message_id")),
            embed_channel_id=_snowflake(raw.get("embed_channel_id")),
            log_channel_id=_snowflake(raw.get("log_channel_id")),
            maintenance_mode=raw.get("maintenance_mode") is True,
            maintenance_since=_parse_dt(raw.get("maintenance_since")),
            maintenance_reason=_text(raw.get("maintenance_reason"), MAX_REASON_LENGTH),
            maintenance_by=_snowflake(raw.get("maintenance_by")),
            offline_since=_parse_dt(raw.get("offline_since")),
            outage_start=_parse_dt(raw.get("outage_start")),
            monitoring_since=_parse_dt(raw.get("monitoring_since")),
        )

        presence_type = raw.get("presence_type")
        presence_text = _text(raw.get("presence_text"), MAX_PRESENCE_TEXT_LENGTH)
        if presence_type in PRESENCE_TYPES and presence_text:
            data.presence_type = presence_type
            data.presence_text = presence_text

        if not data.maintenance_mode:
            data.maintenance_since = None
            data.maintenance_reason = None
            data.maintenance_by = None
        elif data.maintenance_since is None:
            data.maintenance_since = datetime.now(timezone.utc)

        history = raw.get("outage_history")
        skipped = 0
        if isinstance(history, list):
            for entry in history:
                outage = _parse_outage(entry)
                if outage is None:
                    skipped += 1
                else:
                    data.outage_history.append(outage)
        if skipped:
            log.warning("Skipped %d invalid outage history entries.", skipped)

        data.outage_history.sort(key=lambda o: o.end)
        if len(data.outage_history) > max_history:
            del data.outage_history[: len(data.outage_history) - max_history]
        return data


def load_data(path: Path, max_history: int) -> BotData:
    """Loads and validates the data file. A corrupt file is backed up and replaced."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return BotData()
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        _backup_corrupt_file(path, str(e))
        return BotData()

    if not isinstance(raw, dict):
        _backup_corrupt_file(path, "top-level value is not an object")
        return BotData()
    return BotData.from_dict(raw, max_history)


def save_data(path: Path, data: BotData) -> None:
    """Writes the data file atomically so a crash can never leave it half-written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # mkstemp creates the file with 0600 permissions (owner read/write only)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data.to_dict(), f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp_name)
        raise


def _backup_corrupt_file(path: Path, reason: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    backup = path.with_name(f"{path.name}.corrupt-{stamp}")
    try:
        os.replace(path, backup)
        log.error("%s is corrupt (%s). Backed up to %s, starting fresh.", path.name, reason, backup.name)
    except OSError as e:
        log.error("%s is corrupt (%s) and could not be backed up: %s", path.name, reason, e)


def _dt_to_str(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _snowflake(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 < value <= MAX_SNOWFLAKE else None


def _text(value: Any, max_length: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:max_length] or None


def _parse_outage(entry: Any) -> Outage | None:
    if not isinstance(entry, dict):
        return None
    start = _parse_dt(entry.get("start"))
    end = _parse_dt(entry.get("end"))
    if start is None or end is None or end < start:
        return None
    return Outage(start=start, end=end)
