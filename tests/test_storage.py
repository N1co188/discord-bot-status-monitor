import json
import os
import stat
import sys
from datetime import datetime, timedelta, timezone

import pytest

from storage import BotData, load_data, save_data

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def test_missing_file_returns_defaults(tmp_path):
    data = load_data(tmp_path / "missing.json", 50)
    assert data == BotData()


def test_roundtrip(tmp_path):
    path = tmp_path / "status_data.json"
    data = BotData(
        status_message_id=123,
        maintenance_mode=True,
        maintenance_since=NOW,
        maintenance_reason="Update",
        maintenance_by=42,
        presence_type="watching",
        presence_text="the bot",
        monitoring_since=NOW - timedelta(days=3),
    )
    data.add_outage(NOW - timedelta(hours=2), NOW - timedelta(hours=1), 50)
    save_data(path, data)

    loaded = load_data(path, 50)
    assert loaded == data
    assert loaded.outage_history[0].duration_seconds == 3600


def test_legacy_format_is_compatible(tmp_path):
    path = tmp_path / "status_data.json"
    path.write_text(json.dumps({
        "status_message_id": 1480559132221116487,
        "alert_ping_message_id": None,
        "outage_history": [
            {"start": "2026-01-01T10:00:00+00:00", "end": "2026-01-01T10:05:00+00:00", "duration_seconds": 300},
        ],
    }))
    data = load_data(path, 50)
    assert data.status_message_id == 1480559132221116487
    assert len(data.outage_history) == 1
    assert data.maintenance_mode is False


def test_invalid_values_are_dropped(tmp_path):
    path = tmp_path / "status_data.json"
    path.write_text(json.dumps({
        "status_message_id": "123; DROP TABLE",
        "alert_ping_message_id": True,
        "embed_channel_id": -1,
        "maintenance_mode": "yes",
        "maintenance_reason": "x" * 1000,
        "presence_type": "evil",
        "presence_text": "hi",
        "outage_history": [
            {"start": "garbage", "end": "2026-01-01T10:00:00+00:00"},
            {"start": "2026-01-01T11:00:00+00:00", "end": "2026-01-01T10:00:00+00:00"},
            "not a dict",
            {"start": "2026-01-01T10:00:00", "end": "2026-01-01T10:01:00"},
        ],
    }))
    data = load_data(path, 50)
    assert data.status_message_id is None
    assert data.alert_ping_message_id is None
    assert data.embed_channel_id is None
    assert data.maintenance_mode is False
    assert data.maintenance_reason is None
    assert data.presence_type is None
    assert len(data.outage_history) == 1
    assert data.outage_history[0].start.tzinfo is not None


def test_history_is_capped(tmp_path):
    data = BotData()
    for i in range(10):
        data.add_outage(NOW + timedelta(minutes=i), NOW + timedelta(minutes=i, seconds=30), 5)
    assert len(data.outage_history) == 5
    assert data.outage_history[0].start == NOW + timedelta(minutes=5)

    path = tmp_path / "status_data.json"
    save_data(path, data)
    assert len(load_data(path, 3).outage_history) == 3


def test_corrupt_file_is_backed_up(tmp_path):
    path = tmp_path / "status_data.json"
    path.write_text("{ not json")
    data = load_data(path, 50)
    assert data == BotData()
    assert not path.exists()
    assert len(list(tmp_path.glob("status_data.json.corrupt-*"))) == 1


def test_save_is_atomic_and_leaves_no_temp_files(tmp_path):
    path = tmp_path / "status_data.json"
    save_data(path, BotData(status_message_id=1))
    save_data(path, BotData(status_message_id=2))
    assert load_data(path, 50).status_message_id == 2
    assert [p.name for p in tmp_path.iterdir()] == ["status_data.json"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_saved_file_is_owner_only(tmp_path):
    path = tmp_path / "status_data.json"
    save_data(path, BotData())
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
