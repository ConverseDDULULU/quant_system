"""미국 정규장(NYSE/NASDAQ) 개장 시간·휴장일·조기폐장 판별

외부 의존성 없이 NYSE 휴장 규칙으로 계산한다.
임시 휴장(국가 애도일 등)은 .env의 MARKET_EXTRA_HOLIDAYS=2026-01-09,2027-... 로 추가한다.
"""

import os
from datetime import date, datetime, time as dtime, timedelta

import pytz

NY_TZ = pytz.timezone("America/New_York")
REGULAR_OPEN = dtime(9, 30)
REGULAR_CLOSE = dtime(16, 0)
EARLY_CLOSE = dtime(13, 0)
CLOSING_WINDOW_MINUTES = 10  # 마감 N분 전부터 신규 진입 차단·일괄 청산


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    next_month = date(year + month // 12, month % 12 + 1, 1)
    last = next_month - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    """그레고리력 부활절 (Anonymous Gregorian algorithm)"""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observed(day: date) -> date:
    """토요일 휴일은 금요일, 일요일 휴일은 월요일로 대체"""
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _extra_holidays() -> set[date]:
    result = set()
    for raw in os.getenv("MARKET_EXTRA_HOLIDAYS", "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            result.add(date.fromisoformat(raw))
        except ValueError:
            print(f"⚠️ MARKET_EXTRA_HOLIDAYS 형식 오류(YYYY-MM-DD): {raw}")
    return result


def us_market_holidays(year: int) -> set[date]:
    holidays = {
        _nth_weekday(year, 1, 0, 3),  # 마틴 루터 킹 데이
        _nth_weekday(year, 2, 0, 3),  # 대통령의 날
        _easter(year) - timedelta(days=2),  # 성금요일
        _last_weekday(year, 5, 0),  # 메모리얼 데이
        _observed(date(year, 7, 4)),  # 독립기념일
        _nth_weekday(year, 9, 0, 1),  # 노동절
        _nth_weekday(year, 11, 3, 4),  # 추수감사절
        _observed(date(year, 12, 25)),  # 크리스마스
    }
    # 신정이 토요일이면 전년도 12/31을 대체 휴장하지 않는다 (NYSE 규칙)
    new_year = date(year, 1, 1)
    if new_year.weekday() != 5:
        holidays.add(_observed(new_year))
    if year >= 2022:
        holidays.add(_observed(date(year, 6, 19)))  # 준틴스
    return holidays | {d for d in _extra_holidays() if d.year == year}


def is_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in us_market_holidays(day.year)


def us_market_early_closes(year: int) -> set[date]:
    """13:00 조기폐장일: 독립기념일 전날, 추수감사절 다음날, 크리스마스 이브"""
    candidates = [
        date(year, 7, 3),
        _nth_weekday(year, 11, 3, 4) + timedelta(days=1),
        date(year, 12, 24),
    ]
    return {d for d in candidates if is_trading_day(d)}


def session_bounds(day: date) -> tuple[datetime, datetime] | None:
    """해당 뉴욕 날짜의 정규장 (개장, 마감) 시각. 휴장일이면 None"""
    if not is_trading_day(day):
        return None
    close_time = EARLY_CLOSE if day in us_market_early_closes(day.year) else REGULAR_CLOSE
    open_dt = NY_TZ.localize(datetime.combine(day, REGULAR_OPEN))
    close_dt = NY_TZ.localize(datetime.combine(day, close_time))
    return open_dt, close_dt


def now_ny() -> datetime:
    return datetime.now(NY_TZ)


def market_phase(now: datetime | None = None) -> str:
    """'closed' | 'open' | 'closing'(마감 N분 전 ~ 마감)"""
    now = (now or now_ny()).astimezone(NY_TZ)
    bounds = session_bounds(now.date())
    if not bounds:
        return "closed"
    open_dt, close_dt = bounds
    if not (open_dt <= now < close_dt):
        return "closed"
    if now >= close_dt - timedelta(minutes=CLOSING_WINDOW_MINUTES):
        return "closing"
    return "open"


def next_market_open(now: datetime | None = None) -> datetime:
    """지금 이후 가장 가까운 정규장 개장 시각 (장중이면 오늘 개장 시각)"""
    now = (now or now_ny()).astimezone(NY_TZ)
    day = now.date()
    for _ in range(15):
        bounds = session_bounds(day)
        if bounds and now < bounds[1]:
            return bounds[0]
        day += timedelta(days=1)
    raise RuntimeError("15일 이내 개장일을 찾지 못했습니다.")


def seconds_until_next_open(now: datetime | None = None) -> float:
    now = (now or now_ny()).astimezone(NY_TZ)
    return max(0.0, (next_market_open(now) - now).total_seconds())
