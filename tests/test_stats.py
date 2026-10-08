from datetime import datetime, timedelta, timezone

from stats import downtime_seconds, format_duration, format_percent, summarize, uptime_percent
from storage import Outage

NOW = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)
DAY = timedelta(days=1)


def outage(start_hours_ago: float, minutes: float) -> Outage:
    start = NOW - timedelta(hours=start_hours_ago)
    return Outage(start=start, end=start + timedelta(minutes=minutes))


def test_format_duration():
    assert format_duration(0) == "0s"
    assert format_duration(59) == "59s"
    assert format_duration(3600) == "1h"
    assert format_duration(3725) == "1h 2m 5s"
    assert format_duration(timedelta(days=2, minutes=1)) == "2d 1m"
    assert format_duration(-5) == "0s"


def test_format_percent_never_rounds_up_to_100():
    assert format_percent(None) == "n/a"
    assert format_percent(100.0) == "100%"
    assert format_percent(99.9999) == "99.99%"
    assert format_percent(50.0) == "50.00%"


def test_downtime_clips_to_window():
    outages = [outage(30, 120), outage(5, 60)]  # first one is outside the 24h window
    assert downtime_seconds(outages, NOW - DAY, NOW) == 3600


def test_downtime_partial_overlap_and_ongoing():
    outages = [outage(24.5, 60)]  # 30 of 60 minutes inside the window
    assert downtime_seconds(outages, NOW - DAY, NOW) == 1800
    assert downtime_seconds([], NOW - DAY, NOW, ongoing_since=NOW - timedelta(minutes=10)) == 600


def test_uptime_percent():
    outages = [outage(12, 144)]  # 2.4h of 24h = 10%
    assert round(uptime_percent(outages, NOW, DAY, monitoring_since=NOW - 10 * DAY), 6) == 90.0
    assert uptime_percent([], NOW, DAY, monitoring_since=NOW - 10 * DAY) == 100.0


def test_uptime_respects_monitoring_start():
    # Monitoring for 2h, offline for the last hour -> 50%
    pct = uptime_percent([], NOW, DAY, monitoring_since=NOW - timedelta(hours=2), ongoing_since=NOW - timedelta(hours=1))
    assert round(pct, 6) == 50.0
    assert uptime_percent([], NOW, DAY, monitoring_since=NOW) is None


def test_summarize():
    outages = [outage(48, 10), outage(5, 30), outage(2, 20)]
    s = summarize(outages, since=NOW - DAY)
    assert (s.count, s.total_seconds, s.longest_seconds, s.average_seconds) == (2, 3000, 1800, 1500)
    assert summarize(outages).count == 3
    assert summarize([]).count == 0
