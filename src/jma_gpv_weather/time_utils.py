from datetime import date, datetime, time as dt_time, timedelta, timezone
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
UTC = timezone.utc

def target_window(target_date: date) -> tuple[datetime, datetime]:
    start = datetime.combine(target_date, dt_time.min, JST)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)

def expected_valid_times(target_date: date, step: int) -> tuple[datetime, ...]:
    start, end = target_window(target_date)
    return tuple(start + timedelta(hours=h) for h in range(0, int((end-start).total_seconds()/3600), step))
