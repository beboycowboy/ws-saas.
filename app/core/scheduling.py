from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.errors import BusinessError

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def local_instant(value: str, zone: ZoneInfo) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC)
    possibilities = []
    for fold in (0, 1):
        aware = parsed.replace(tzinfo=zone, fold=fold)
        if aware.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == parsed:
            possibilities.append(aware.astimezone(UTC))
    if not possibilities:
        raise BusinessError("invalid_time", "La hora no existe por cambio de horario")
    if len(set(possibilities)) > 1:
        raise BusinessError("ambiguous_time", "La hora es ambigua; elige un horario con offset")
    return possibilities[0]


def candidates(config, service, start_day: date, end_day: date, now: datetime):
    zone = ZoneInfo(config.tenant.timezone)
    rules = config.appointments
    today = now.astimezone(zone).date()
    start_day = max(start_day, today)
    end_day = min(end_day, today + timedelta(days=rules.max_days_ahead))
    day = start_day
    while day <= end_day:
        if day not in rules.closed_dates:
            ranges = rules.date_overrides.get(day, rules.weekly_hours.get(DAYS[day.weekday()], []))
            for opening, closing in ranges:
                candidate = datetime.combine(day, time.fromisoformat(opening))
                close = datetime.combine(day, time.fromisoformat(closing))
                while candidate < close:
                    try:
                        start = local_instant(candidate.isoformat(), zone)
                        end = start + timedelta(minutes=service["minutes"] + service["buffer_minutes"])
                        close_utc = local_instant(close.isoformat(), zone)
                        if start >= now + timedelta(minutes=rules.min_notice_minutes) and end <= close_utc:
                            yield start, end
                    except BusinessError:
                        pass
                    candidate += timedelta(minutes=rules.slot_step_minutes)
        day += timedelta(days=1)


def available(c, config, service, start_day, end_day, now, limit=6):
    possible = list(candidates(config, service, start_day, end_day, now))
    if not possible:
        return []
    busy = c.execute(
        """SELECT starts_at,ends_at FROM appointments WHERE status='confirmed'
        AND starts_at<%s AND ends_at>%s ORDER BY starts_at""",
        (possible[-1][1], possible[0][0]),
    ).fetchall()
    free = [a for a, b in possible if not any(a < r["ends_at"] and b > r["starts_at"] for r in busy)]
    if len(free) <= limit:
        return free
    # Spread over the interval instead of always offering only the opening hour.
    return [free[round(i * (len(free) - 1) / (limit - 1))] for i in range(limit)]
