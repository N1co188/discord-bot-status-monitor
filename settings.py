"""
Configuration loading and validation.

Values are read from environment variables first (optionally from a `.env`
file) and fall back to `config.py`. Every value is validated at startup so
misconfigurations fail loudly instead of causing odd behaviour at runtime.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

BASE_DIR = Path(__file__).resolve().parent

# Discord snowflakes are unsigned 64-bit integers
MAX_SNOWFLAKE = 2**64 - 1

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}

_UNSET = object()


class ConfigError(Exception):
    """Raised when the configuration is missing or invalid."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__(
            "Invalid configuration:\n" + "\n".join(f"  - {p}" for p in problems)
        )


@dataclass(frozen=True)
class Settings:
    # repr=False keeps the token out of logs and tracebacks
    bot_token: str = field(repr=False)
    watched_bot_id: int
    guild_id: int
    status_embed_channel_id: int
    status_log_channel_id: int
    status_role_id: int | None = None
    developer_ids: tuple[int, ...] = ()
    admin_role_ids: tuple[int, ...] = ()
    check_interval_seconds: int = 1200
    offline_grace_seconds: int = 0
    max_history_entries: int = 500
    leave_unauthorized_guilds: bool = False
    log_level: str = "INFO"
    log_file: Path | None = BASE_DIR / "logs" / "monitor.log"
    data_file: Path = BASE_DIR / "status_data.json"


def _load_dotenv() -> None:
    """Loads a `.env` file next to the bot if python-dotenv is installed."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(BASE_DIR / ".env", override=False)


def _import_config() -> ModuleType | None:
    try:
        return importlib.import_module("config")
    except ModuleNotFoundError as e:
        if e.name != "config":
            raise
        return None


class _Reader:
    """Reads raw values (env first, then config module) and collects problems."""

    def __init__(self, env: Mapping[str, str], config: ModuleType | None):
        self.env = env
        self.config = config
        self.problems: list[str] = []

    def raw(self, key: str, *aliases: str) -> Any:
        for name in (*aliases, key):
            value = self.env.get(name)
            if value is not None and value.strip() != "":
                return value.strip()
        if self.config is not None and hasattr(self.config, key):
            return getattr(self.config, key)
        return _UNSET

    def snowflake(self, key: str, *, required: bool) -> int | None:
        value = self.raw(key)
        if value is _UNSET or value in (0, "0", None, ""):
            if required:
                self.problems.append(f"{key} is required (Discord ID).")
            return None
        parsed = _parse_snowflake(value)
        if parsed is None:
            self.problems.append(f"{key} must be a valid Discord ID, got {value!r}.")
        return parsed

    def snowflake_list(self, key: str) -> tuple[int, ...]:
        value = self.raw(key)
        if value is _UNSET or value in (None, ""):
            return ()
        if isinstance(value, str):
            items: list[Any] = [part.strip() for part in value.split(",") if part.strip()]
        elif isinstance(value, (list, tuple, set, frozenset)):
            items = list(value)
        else:
            self.problems.append(f"{key} must be a list of Discord IDs.")
            return ()

        result: list[int] = []
        for item in items:
            parsed = _parse_snowflake(item)
            if parsed is None:
                self.problems.append(f"{key} contains an invalid Discord ID: {item!r}.")
            elif parsed not in result:
                result.append(parsed)
        return tuple(result)

    def integer(self, key: str, default: int, *, minimum: int, maximum: int) -> int:
        value = self.raw(key)
        if value is _UNSET:
            return default
        try:
            if isinstance(value, bool):
                raise ValueError
            parsed = int(value)
        except (TypeError, ValueError):
            self.problems.append(f"{key} must be an integer, got {value!r}.")
            return default
        if not minimum <= parsed <= maximum:
            self.problems.append(f"{key} must be between {minimum} and {maximum}, got {parsed}.")
            return default
        return parsed

    def boolean(self, key: str, default: bool) -> bool:
        value = self.raw(key)
        if value is _UNSET:
            return default
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in _TRUE_VALUES:
            return True
        if text in _FALSE_VALUES:
            return False
        self.problems.append(f"{key} must be true or false, got {value!r}.")
        return default

    def path(self, key: str, default: Path | None) -> Path | None:
        value = self.raw(key)
        if value is _UNSET:
            return default
        if value in (None, "", False) or str(value).strip().lower() in ("none", "off", "false"):
            return None
        path = Path(str(value)).expanduser()
        return path if path.is_absolute() else BASE_DIR / path


def _parse_snowflake(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if not 0 < parsed <= MAX_SNOWFLAKE:
        return None
    return parsed


def _validate_token(token: Any, problems: list[str]) -> str:
    if token is _UNSET or not isinstance(token, str) or not token.strip():
        problems.append(
            "BOT_TOKEN is missing. Set the DISCORD_BOT_TOKEN environment variable "
            "(recommended) or BOT_TOKEN in config.py."
        )
        return ""
    token = token.strip()
    if any(ch.isspace() for ch in token):
        problems.append("BOT_TOKEN must not contain whitespace.")
    elif token.count(".") != 2:
        problems.append("BOT_TOKEN does not look like a Discord bot token.")
    return token


def load_settings(
    env: Mapping[str, str] | None = None,
    config: ModuleType | None | object = _UNSET,
) -> Settings:
    """Builds validated settings. Raises ConfigError listing every problem found."""
    if env is None:
        _load_dotenv()
        env = os.environ
    if config is _UNSET:
        config = _import_config()

    reader = _Reader(env, config)  # type: ignore[arg-type]
    problems = reader.problems

    token = _validate_token(reader.raw("BOT_TOKEN", "DISCORD_BOT_TOKEN"), problems)

    watched_bot_id = reader.snowflake("WATCHED_BOT_ID", required=True)
    guild_id = reader.snowflake("GUILD_ID", required=True)
    embed_channel_id = reader.snowflake("STATUS_EMBED_CHANNEL_ID", required=True)
    log_channel_id = reader.snowflake("STATUS_LOG_CHANNEL_ID", required=True)
    status_role_id = reader.snowflake("STATUS_ROLE_ID", required=False)
    developer_ids = reader.snowflake_list("DEVELOPER_IDS")
    admin_role_ids = reader.snowflake_list("ADMIN_ROLE_IDS")

    check_interval = reader.integer("CHECK_INTERVAL_SECONDS", 1200, minimum=60, maximum=86400)
    grace = reader.integer("OFFLINE_GRACE_SECONDS", 0, minimum=0, maximum=3600)
    max_history = reader.integer("MAX_HISTORY_ENTRIES", 500, minimum=10, maximum=10000)
    leave_guilds = reader.boolean("LEAVE_UNAUTHORIZED_GUILDS", False)

    raw_level = reader.raw("LOG_LEVEL")
    log_level = "INFO" if raw_level is _UNSET else str(raw_level).strip().upper()
    if log_level not in LOG_LEVELS:
        problems.append(f"LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}, got {log_level!r}.")
        log_level = "INFO"

    log_file = reader.path("LOG_FILE", BASE_DIR / "logs" / "monitor.log")
    data_file = reader.path("DATA_FILE", BASE_DIR / "status_data.json") or BASE_DIR / "status_data.json"

    if watched_bot_id is not None and watched_bot_id in developer_ids:
        problems.append("WATCHED_BOT_ID must not be listed in DEVELOPER_IDS.")

    if problems:
        raise ConfigError(problems)

    return Settings(
        bot_token=token,
        watched_bot_id=watched_bot_id,  # type: ignore[arg-type]
        guild_id=guild_id,  # type: ignore[arg-type]
        status_embed_channel_id=embed_channel_id,  # type: ignore[arg-type]
        status_log_channel_id=log_channel_id,  # type: ignore[arg-type]
        status_role_id=status_role_id,
        developer_ids=developer_ids,
        admin_role_ids=admin_role_ids,
        check_interval_seconds=check_interval,
        offline_grace_seconds=grace,
        max_history_entries=max_history,
        leave_unauthorized_guilds=leave_guilds,
        log_level=log_level,
        log_file=log_file,
        data_file=data_file,
    )
