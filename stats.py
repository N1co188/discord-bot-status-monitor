"""Availability statistics and formatting helpers (no Discord dependencies)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from storage import Outage


def format_duration(seconds: float | timedelta) -> str:
    """Formats a duration like '2d 3h 4m 5s' (zero units are left out)."""
    if isinstance(seconds, timedelta):
        seconds = seconds.total_seconds()
    total = max(0, int(seconds))
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)

    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def _overlap_seconds(start: datetime, end: datetime, window_start: datetime, window_end: datetime) -> float:
    latest_start = max(start, window_start)
    earliest_end = min(end, window_end)
    return max(0.0, (earliest_end - latest_start).total_seconds())


def downtime_seconds(
    outages: Iterable[Outage],
    window_start: datetime,
    window_end: datetime,
    ongoing_since: datetime | None = None,
) -> float:
    """Total downtime inside [window_start, window_end], including an ongoing outage."""
    total = sum(_overlap_seconds(o.start, o.end, window_start, window_end) for o in outages)
    if ongoing_since is not None:
        total += _overlap_seconds(ongoing_since, window_end, window_start, window_end)
    return total


def uptime_percent(
    outages: Iterable[Outage],
    now: datetime,
    window: timedelta,
    monitoring_since: datetime | None,
    ongoing_since: datetime | None = None,
) -> float | None:
    """Availability in percent for the last `window`, or None if there is no data yet."""
    window_start = now - window
    if monitoring_since is not None:
        window_start = max(window_start, monitoring_since)
    length = (now - window_start).total_seconds()
    if length <= 0:
        return None
    down = downtime_seconds(outages, window_start, now, ongoing_since)
    return max(0.0, min(100.0, 100.0 * (1 - down / length)))


def format_percent(value: float | None) -> str:
    if value is None:
        return "n/a"
    if value >= 100:
        return "100%"
    # Never round e.g. 99.996% up to a misleading "100.00%"
    return f"{int(value * 100) / 100:.2f}%"


@dataclass(frozen=True)
class OutageSummary:
    count: int
    total_seconds: int
    longest_seconds: int
    average_seconds: int


def summarize(outages: Iterable[Outage], since: datetime | None = None) -> OutageSummary:
    """Counts outages that ended after `since` (all outages if `since` is None)."""
    durations = [o.duration_seconds for o in outages if since is None or o.end >= since]
    if not durations:
        return OutageSummary(0, 0, 0, 0)
    total = sum(durations)
    return OutageSummary(
        count=len(durations),
        total_seconds=total,
        longest_seconds=max(durations),
        average_seconds=total // len(durations),
    )
