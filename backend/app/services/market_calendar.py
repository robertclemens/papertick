"""NYSE trading calendar and market hours.

Holidays are computed from exchange rules (fixed dates with Sat->Fri / Sun->Mon
observance, floating Mondays/Thursdays, Good Friday via computus) plus a small
set of special closures. Times are true America/New_York wall-clock (DST-safe):
regular session 9:30-16:00 ET. Early closes (day after Thanksgiving etc.) are
not modeled; those days trade the full session here.
"""

from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
OPEN_T = time(9, 30)
CLOSE_T = time(16, 0)
MF_FILL_DELAY = timedelta(minutes=45)  # NAV fills post shortly after the close

# Unscheduled full-day closures within the platform's data range (2015+)
EXTRA_CLOSURES = {
    date(2018, 12, 5),  # G.H.W. Bush national day of mourning
    date(2025, 1, 9),   # J. Carter national day of mourning
}


def _easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous computus)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 19 * l) // 433
    month = (h + l - 7 * m + 90) // 25
    day = (h + l - 7 * m + 33 * month + 19) % 32
    return date(year, month, day)


def _observed(d: date) -> date | None:
    if d.weekday() == 5:  # Saturday -> Friday before
        return d - timedelta(days=1)
    if d.weekday() == 6:  # Sunday -> Monday after
        return d + timedelta(days=1)
    return d


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year, month, 28)
    while d.month == month:
        d += timedelta(days=1)
    d -= timedelta(days=1)
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d


@lru_cache(maxsize=64)
def holidays(year: int) -> frozenset[date]:
    hs: set[date] = set()
    ny = _observed(date(year, 1, 1))
    # New Year's on a Saturday is not observed the prior Friday by NYSE
    if ny is not None and ny.year == year:
        hs.add(ny)
    hs.add(_nth_weekday(year, 1, 0, 3))    # MLK Day
    hs.add(_nth_weekday(year, 2, 0, 3))    # Washington's Birthday
    hs.add(_easter(year) - timedelta(days=2))  # Good Friday
    hs.add(_last_weekday(year, 5, 0))      # Memorial Day
    if year >= 2022:
        j = _observed(date(year, 6, 19))   # Juneteenth
        if j:
            hs.add(j)
    i = _observed(date(year, 7, 4))
    if i:
        hs.add(i)
    hs.add(_nth_weekday(year, 9, 0, 1))    # Labor Day
    hs.add(_nth_weekday(year, 11, 3, 4))   # Thanksgiving
    x = _observed(date(year, 12, 25))
    if x:
        hs.add(x)
    hs |= {d for d in EXTRA_CLOSURES if d.year == year}
    return frozenset(hs)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in holidays(d.year)


def market_open_at(d: date) -> datetime:
    return datetime.combine(d, OPEN_T, tzinfo=ET).astimezone(timezone.utc)


def market_close_at(d: date) -> datetime:
    return datetime.combine(d, CLOSE_T, tzinfo=ET).astimezone(timezone.utc)


def is_market_open(now: datetime) -> bool:
    d = now.astimezone(ET).date()
    return is_trading_day(d) and market_open_at(d) <= now < market_close_at(d)


def next_trading_day(d: date) -> date:
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def next_market_open(now: datetime) -> datetime:
    """First session open strictly after `now`."""
    d = now.astimezone(ET).date()
    while True:
        if is_trading_day(d) and market_open_at(d) > now:
            return market_open_at(d)
        d += timedelta(days=1)


def next_market_close(now: datetime) -> datetime:
    d = now.astimezone(ET).date()
    while True:
        if is_trading_day(d) and market_close_at(d) > now:
            return market_close_at(d)
        d += timedelta(days=1)


def nav_date_for(now: datetime) -> date:
    """The trading day whose closing NAV a mutual-fund order placed at `now`
    receives: today if before the 4:00 PM ET cutoff on a trading day, else the
    next trading day (standard forward pricing)."""
    d = now.astimezone(ET).date()
    if is_trading_day(d) and now < market_close_at(d):
        return d
    return next_trading_day(d + timedelta(days=1))


def mf_fill_time(nav_date: date) -> datetime:
    return market_close_at(nav_date) + MF_FILL_DELAY
