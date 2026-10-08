from types import SimpleNamespace

import pytest

from settings import ConfigError, load_settings

TOKEN = "MTIz.abc.def"

BASE_ENV = {
    "DISCORD_BOT_TOKEN": TOKEN,
    "WATCHED_BOT_ID": "111",
    "GUILD_ID": "222",
    "STATUS_EMBED_CHANNEL_ID": "333",
    "STATUS_LOG_CHANNEL_ID": "444",
}


def test_env_only():
    s = load_settings(env={**BASE_ENV, "DEVELOPER_IDS": "1, 2,2", "OFFLINE_GRACE_SECONDS": "90"}, config=None)
    assert s.bot_token == TOKEN
    assert s.watched_bot_id == 111
    assert s.developer_ids == (1, 2)
    assert s.status_role_id is None
    assert s.offline_grace_seconds == 90
    assert s.check_interval_seconds == 1200


def test_config_module_and_env_override():
    config = SimpleNamespace(
        BOT_TOKEN="from.config.file",
        WATCHED_BOT_ID=111,
        GUILD_ID=222,
        STATUS_EMBED_CHANNEL_ID=333,
        STATUS_LOG_CHANNEL_ID=444,
        STATUS_ROLE_ID=0,
        DEVELOPER_IDS=[5, 6],
        ADMIN_ROLE_IDS=[],
    )
    s = load_settings(env={"DISCORD_BOT_TOKEN": TOKEN}, config=config)
    assert s.bot_token == TOKEN
    assert s.developer_ids == (5, 6)

    s = load_settings(env={}, config=config)
    assert s.bot_token == "from.config.file"


def test_token_not_in_repr():
    s = load_settings(env=BASE_ENV, config=None)
    assert TOKEN not in repr(s)


def test_missing_values_are_all_reported():
    with pytest.raises(ConfigError) as exc:
        load_settings(env={}, config=None)
    problems = "\n".join(exc.value.problems)
    for key in ("BOT_TOKEN", "WATCHED_BOT_ID", "GUILD_ID", "STATUS_EMBED_CHANNEL_ID", "STATUS_LOG_CHANNEL_ID"):
        assert key in problems


@pytest.mark.parametrize(
    "key,value",
    [
        ("WATCHED_BOT_ID", "abc"),
        ("WATCHED_BOT_ID", "-5"),
        ("DEVELOPER_IDS", "1,x"),
        ("CHECK_INTERVAL_SECONDS", "5"),
        ("OFFLINE_GRACE_SECONDS", "99999"),
        ("LEAVE_UNAUTHORIZED_GUILDS", "maybe"),
        ("LOG_LEVEL", "LOUD"),
        ("DISCORD_BOT_TOKEN", "not a token"),
        ("DISCORD_BOT_TOKEN", "nodots"),
    ],
)
def test_invalid_values(key, value):
    with pytest.raises(ConfigError):
        load_settings(env={**BASE_ENV, key: value}, config=None)


def test_watched_bot_must_not_be_developer():
    with pytest.raises(ConfigError):
        load_settings(env={**BASE_ENV, "DEVELOPER_IDS": "111"}, config=None)


def test_booleans_and_paths(tmp_path):
    s = load_settings(
        env={**BASE_ENV, "LEAVE_UNAUTHORIZED_GUILDS": "yes", "LOG_FILE": "none", "DATA_FILE": str(tmp_path / "d.json")},
        config=None,
    )
    assert s.leave_unauthorized_guilds is True
    assert s.log_file is None
    assert s.data_file == tmp_path / "d.json"
