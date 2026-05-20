"""Formatting + view-shape helpers shared across routes.

Templates need pre-formatted strings (relative time, "12.3K tokens", clock
time, etc.) because Jinja doesn't have a good way to compute them inline.
Keeping the transforms here means routes stay focused on data, templates
stay focused on layout.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any


def fmt_tokens(n: int | None) -> str:
    """Compact: 12345 → 12.3K, 1234567 → 1.2M."""
    if n is None:
        return "—"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def fmt_ms(ms: int | None) -> str:
    if ms is None:
        return "—"
    if ms < 1000:
        return f"{ms}ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s" if ms < 10_000 else f"{ms // 1000}s"
    m, s = divmod(ms // 1000, 60)
    return f"{m}m {s}s"


def rel_time(dt: Any) -> str:
    """Relative time from now. Accepts datetime, ISO string, or None."""
    if dt is None:
        return "—"
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except ValueError:
            return dt
    if not isinstance(dt, datetime):
        return str(dt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delta = datetime.now(UTC) - dt
    s = int(delta.total_seconds())
    if s < 0:
        return "just now"
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86_400:
        return f"{s // 3600}h ago"
    return f"{s // 86_400}d ago"


def clock_time(dt: Any) -> str:
    """HH:MM in the local clock — used in the live-feed list."""
    if dt is None:
        return "—"
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except ValueError:
            return dt[11:16] if len(dt) >= 16 else dt
    if not isinstance(dt, datetime):
        return str(dt)
    return dt.strftime("%H:%M")


def greeting_for(now: datetime | None = None) -> str:
    """Good morning / afternoon / evening — owner-facing greeting."""
    now = now or datetime.now()
    h = now.hour
    if h < 12:
        return "Good morning"
    if h < 18:
        return "Good afternoon"
    return "Good evening"


def utc_iso_minutes_ago(minutes: int) -> str:
    dt = datetime.now(UTC) - timedelta(minutes=minutes)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"


def utc_iso_hours_ago(hours: int) -> str:
    return utc_iso_minutes_ago(hours * 60)


def bucket_into(values: list[Any], buckets: int = 12) -> list[int]:
    """Squash a sequence into N equal-count buckets for the sparkline.
    Returns one int per bucket — useful for spark() macro."""
    if not values or buckets <= 0:
        return [0] * max(buckets, 1)
    out = [0] * buckets
    step = max(1, len(values) // buckets)
    for i, _ in enumerate(values):
        bucket = min(i // step, buckets - 1)
        out[bucket] += 1
    return out


def context_peak_pct(
    peak_tokens: int | None, window_size: int | None
) -> float | None:
    """Wakeup ctx-peak as a percentage of the model's context window."""
    if not peak_tokens or not window_size:
        return None
    return peak_tokens / window_size * 100
