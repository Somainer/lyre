"""Time / recurrence helpers for the scheduled-mail feature.

Two duration vocabularies:
  - Lyre duration strings ("30m", "2h", "1d", "1w") — used in deliver_in and
    recur_every. Minute precision; minimum 1 minute; maximum 1 year.
  - Cron expressions (5-field POSIX) — used in recur_cron. Parsed by croniter.

A small set of helpers, intentionally pure & sync so they're trivial to test
and to reuse from both the tool layer and the CLI:

  parse_duration("1h") -> timedelta(hours=1)
  resolve_first_fire(deliver_at=..., deliver_in=...) -> datetime UTC
  compute_next_fire(recur_kind, recur_value, after=now) -> datetime UTC | None
  validate_cron(cron_expr) -> None  (raises ValueError on bad expr)
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from croniter import croniter

MAX_HORIZON = timedelta(days=365)
MIN_INTERVAL = timedelta(minutes=1)

_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$")
_DURATION_UNITS = {
    "s": "seconds",   # accepted but warned against; floors to minute later
    "m": "minutes",
    "h": "hours",
    "d": "days",
    "w": "weeks",
}


def parse_duration(s: str) -> timedelta:
    """'30m' -> 30 minutes; '2h' -> 2 hours; etc.

    Raises ValueError on malformed input or on durations below 1 minute /
    above 1 year. We accept seconds-precision shorthand ('30s') only to
    fail loud with a nice error — Lyre is minute-grained on purpose.
    """
    if not isinstance(s, str):
        raise ValueError(f"duration must be a string like '30m', got {s!r}")
    m = _DURATION_RE.match(s)
    if not m:
        raise ValueError(
            f"unrecognized duration {s!r}; expected '<int><unit>' where "
            f"unit is one of m/h/d/w (e.g. '30m', '2h', '1d', '1w')"
        )
    n = int(m.group(1))
    unit = m.group(2)
    if unit == "s":
        raise ValueError(
            f"second-grained duration {s!r} not supported; Lyre future mail "
            f"is minute-precision. Use '<N>m' (≥1m) instead."
        )
    delta = timedelta(**{_DURATION_UNITS[unit]: n})
    if delta < MIN_INTERVAL:
        raise ValueError(
            f"duration {s!r} below minimum (1m). Use '1m' or longer."
        )
    if delta > MAX_HORIZON:
        raise ValueError(
            f"duration {s!r} exceeds 1-year horizon."
        )
    return delta


def validate_cron(expr: str) -> None:
    """Raise ValueError if `expr` isn't a valid 5-field cron expression.

    We deliberately reject `@daily`/`@hourly` and other special strings,
    even though croniter accepts them — Lyre standardizes on the 5-field
    POSIX form so audit logs and dashboards have a single shape.
    """
    if not isinstance(expr, str):
        raise ValueError(f"cron expression must be a string, got {type(expr)}")
    if expr.strip().startswith("@"):
        raise ValueError(
            f"invalid cron expression {expr!r}: special strings like "
            f"'@daily' are not supported. Use 5-field POSIX form, e.g. "
            f"'0 9 * * 1-5' (workday 9am) or '0 * * * *' (hourly)."
        )
    if not croniter.is_valid(expr):
        raise ValueError(
            f"invalid cron expression {expr!r}. Use 5-field POSIX form, "
            f"e.g. '0 9 * * 1-5' (workday 9am)."
        )


def now_utc() -> datetime:
    """Single seam — tests monkey-patch this when they need to fix time."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def _ensure_utc(dt: datetime) -> datetime:
    """Coerce a naive datetime to UTC; pass through if already aware."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class PastDeliveryError(ValueError):
    """Raised when deliver_at resolves to a moment in the past.

    Carries `current_utc` so the tool layer can surface it back to the
    agent ('use this instead'). Matches the S3 decision: agents that
    schedule in the past have almost always hallucinated current time;
    erroring out forces a recompute on the next turn.
    """

    def __init__(self, scheduled: datetime, current: datetime) -> None:
        super().__init__(
            f"deliver_at {scheduled.isoformat()} is in the past. "
            f"Current UTC is {current.isoformat()}. Use deliver_in "
            f"('1m','2h','1d','1w') for relative scheduling, or pass a "
            f"deliver_at strictly greater than current UTC."
        )
        self.scheduled = scheduled
        self.current = current


def resolve_first_fire(
    deliver_at: str | datetime | None,
    deliver_in: str | None,
    recur_cron: str | None,
    now: datetime | None = None,
) -> datetime:
    """Compute the first delivery moment.

    Priority:
      1. deliver_at (explicit absolute)
      2. deliver_in (relative shortcut)
      3. recur_cron alone — first fire = next cron match after now

    Returns aware UTC datetime. Raises:
      - ValueError on bad input
      - PastDeliveryError if deliver_at < now (S3 reject)
      - ValueError if first fire > now + 1 year (S5 cap)
    """
    if now is None:
        now = now_utc()
    if deliver_at is not None and deliver_in is not None:
        raise ValueError("pass at most one of deliver_at / deliver_in")
    if deliver_at is not None:
        if isinstance(deliver_at, str):
            try:
                first = datetime.fromisoformat(deliver_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(
                    f"deliver_at must be ISO 8601 (e.g. "
                    f"'2026-06-01T09:00:00Z'); got {deliver_at!r}"
                ) from exc
        else:
            first = deliver_at
        first = _ensure_utc(first).replace(microsecond=0)
        if first <= now:
            raise PastDeliveryError(first, now)
    elif deliver_in is not None:
        first = now + parse_duration(deliver_in)
    elif recur_cron is not None:
        validate_cron(recur_cron)
        first = croniter(recur_cron, now).get_next(datetime)
        first = _ensure_utc(first).replace(microsecond=0)
    else:
        raise ValueError(
            "must supply one of deliver_at, deliver_in, or recur_cron"
        )

    if first > now + MAX_HORIZON:
        raise ValueError(
            f"first delivery {first.isoformat()} is more than 1 year out "
            f"(current UTC {now.isoformat()}). Hard cap to prevent agents "
            f"from accidentally scheduling things into 2099."
        )
    return first


def compute_next_fire(
    recur_kind: str | None,
    recur_value: str | None,
    after: datetime,
    recur_until: datetime | None = None,
) -> datetime | None:
    """Compute the next-fire moment for a recurring schedule.

    Returns None if there are no further occurrences (recurring schedule
    has reached its recur_until). Caller treats None as 'mark completed'.
    """
    if recur_kind is None:
        return None
    after = _ensure_utc(after)
    if recur_kind == "interval":
        if not recur_value:
            return None
        delta = parse_duration(recur_value)
        next_at = after + delta
    elif recur_kind == "cron":
        if not recur_value:
            return None
        next_at = croniter(recur_value, after).get_next(datetime)
        next_at = _ensure_utc(next_at).replace(microsecond=0)
    else:
        raise ValueError(f"unknown recur_kind {recur_kind!r}")

    if recur_until is not None and next_at > _ensure_utc(recur_until):
        return None
    return next_at


def default_recur_until(first_fire: datetime) -> datetime:
    """Per design: if user doesn't pass recur_until, cap at first + 1 year.

    Bounds runaway recurring schedules without forcing the user to think
    about end-dates in the common case.
    """
    return first_fire + MAX_HORIZON


def iso(dt: datetime) -> str:
    """Canonical Lyre ISO 8601 UTC string ('Z' suffix, no microseconds)."""
    return _ensure_utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")
