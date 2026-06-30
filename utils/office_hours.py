from datetime import datetime, time, timedelta, timezone, date
from zoneinfo import ZoneInfo
from database import get_database

_IST = ZoneInfo("Asia/Kolkata")
_OFFICE_START = time(9, 30)
_OFFICE_END = time(17, 30)
_SECONDS_PER_WORKDAY = 8 * 60 * 60


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _days_between(start: datetime, end: datetime) -> int:
    return (end.date() - start.date()).days


async def _get_holidays() -> set[date]:
    """Retrieve all holiday dates from the database collection."""
    db = get_database()
    if db is None:
        return set()
    cursor = db.holidays.find({}, {"date": 1})
    holidays = set()
    async for h in cursor:
        d = h.get("date")
        if isinstance(d, datetime):
            holidays.add(d.date())
        elif isinstance(d, date):
            holidays.add(d)
        elif isinstance(d, str):
            try:
                holidays.add(date.fromisoformat(d[:10]))
            except ValueError:
                pass
    return holidays


async def calculate_raw_office_seconds(start_dt: datetime, end_dt: datetime) -> int:
    """Calculate wall-clock seconds within office hours (9:30 AM–5:30 PM IST, Mon–Fri, excluding holidays)."""
    if start_dt >= end_dt:
        return 0

    start = _ensure_utc(start_dt).astimezone(_IST)
    end = _ensure_utc(end_dt).astimezone(_IST)
    
    # Fetch holidays dynamically from the DB
    holidays = await _get_holidays()
    
    total = 0
    days = _days_between(start, end)

    if days == 0:
        return await _same_day_seconds(start, end, holidays)

    for d in range(days + 1):
        day = start + timedelta(days=d)
        day_date = day.date()

        if day.weekday() >= 5 or day_date in holidays:
            continue

        ds = datetime.combine(day_date, _OFFICE_START, tzinfo=_IST)
        de = datetime.combine(day_date, _OFFICE_END, tzinfo=_IST)

        if d == 0:
            a, b = max(ds, start), de
        elif d == days:
            a, b = ds, min(de, end)
        else:
            a, b = ds, de

        if b > a:
            total += int((b - a).total_seconds())

    return total


async def _same_day_seconds(start_ist: datetime, end_ist: datetime, holidays: set[date]) -> int:
    day_date = start_ist.date()
    if start_ist.weekday() >= 5 or day_date in holidays:
        return 0
    ds = datetime.combine(day_date, _OFFICE_START, tzinfo=_IST)
    de = datetime.combine(day_date, _OFFICE_END, tzinfo=_IST)
    a, b = max(ds, start_ist), min(de, end_ist)
    if b > a:
        return int((b - a).total_seconds())
    return 0


async def calculate_office_elapsed_seconds(start_dt: datetime, end_dt: datetime, total_paused_ms: int = 0) -> int:
    """Calculate elapsed seconds within office hours, prorating for paused time and considering holidays."""
    office_sec = await calculate_raw_office_seconds(start_dt, end_dt)
    total_wall_sec = (_ensure_utc(end_dt) - _ensure_utc(start_dt)).total_seconds()
    if total_wall_sec <= 0:
        return 0
    paused_sec = total_paused_ms / 1000
    running_ratio = max(0, (total_wall_sec - paused_sec) / total_wall_sec)
    return int(office_sec * running_ratio)
