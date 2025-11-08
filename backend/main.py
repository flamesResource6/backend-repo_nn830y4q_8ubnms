import os
from datetime import datetime, timedelta, time
from typing import List, Tuple

import pytz
import requests
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class AvailabilityResponse(BaseModel):
    date: str
    timezone: str
    slot_minutes: int
    slots: List[str]
    configured: bool


@app.get("/")
def read_root():
    return {"message": "Hello from FastAPI Backend!"}


@app.get("/api/hello")
def hello():
    return {"message": "Hello from the backend API!"}


@app.get("/test")
def test_database():
    """Test endpoint to check if database is available and accessible"""
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": [],
    }

    try:
        # Try to import database module
        from database import db  # type: ignore

        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Configured"
            response["database_name"] = db.name if hasattr(db, "name") else "✅ Connected"
            response["connection_status"] = "Connected"

            # Try to list collections to verify connectivity
            try:
                collections = db.list_collection_names()
                response["collections"] = collections[:10]  # Show first 10 collections
                response["database"] = "✅ Connected & Working"
            except Exception as e:  # noqa: BLE001
                response["database"] = f"⚠️  Connected but Error: {str(e)[:50]}"
        else:
            response["database"] = "⚠️  Available but not initialized"

    except ImportError:
        response["database"] = "❌ Database module not found (run enable-database first)"
    except Exception as e:  # noqa: BLE001
        response["database"] = f"❌ Error: {str(e)[:50]}"

    # Check environment variables
    response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
    response["database_name"] = "✅ Set" if os.getenv("DATABASE_NAME") else "❌ Not Set"

    return response


# --- Availability / Google Calendar (via public iCal) ---

def get_business_hours(day: datetime) -> Tuple[time, time] | None:
    # Monday=0 ... Sunday=6
    weekday = day.weekday()
    if weekday in (0, 1, 2, 3, 4):  # Mon-Fri
        return time(9, 0), time(18, 0)
    if weekday == 5:  # Sat
        return time(10, 0), time(14, 0)
    # Sunday closed
    return None


def fetch_busy_intervals_ical(date_local: datetime, tz: pytz.BaseTzInfo) -> List[Tuple[datetime, datetime]]:
    ical_url = os.getenv("GOOGLE_CALENDAR_ICAL_URL", "").strip()
    if not ical_url:
        return []

    try:
        from ics import Calendar  # imported here so app works if not installed elsewhere
    except Exception:
        return []

    try:
        resp = requests.get(ical_url, timeout=10)
        resp.raise_for_status()
        cal = Calendar(resp.text)
    except Exception:
        return []

    start_of_day = tz.localize(datetime(date_local.year, date_local.month, date_local.day, 0, 0, 0))
    end_of_day = start_of_day + timedelta(days=1)

    busy: List[Tuple[datetime, datetime]] = []
    for event in cal.events:
        # event.begin/end can be date or datetime; convert to aware datetimes in local tz
        try:
            ev_start = event.begin.datetime
            ev_end = event.end.datetime if event.end else (event.begin.datetime + timedelta(minutes=30))
        except Exception:
            continue

        # normalize to aware datetimes in local tz
        if ev_start.tzinfo is None:
            ev_start = tz.localize(ev_start)
        else:
            ev_start = ev_start.astimezone(tz)

        if ev_end.tzinfo is None:
            ev_end = tz.localize(ev_end)
        else:
            ev_end = ev_end.astimezone(tz)

        # consider only events overlapping the day
        latest_start = max(ev_start, start_of_day)
        earliest_end = min(ev_end, end_of_day)
        if latest_start < earliest_end:
            busy.append((latest_start, earliest_end))

    # Optional: merge overlapping intervals
    busy.sort(key=lambda x: x[0])
    merged: List[Tuple[datetime, datetime]] = []
    for s, e in busy:
        if not merged or s > merged[-1][1]:
            merged.append((s, e))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))

    return merged


def generate_slots(date_str: str, slot_minutes: int = 30) -> AvailabilityResponse:
    tz_name = os.getenv("BUSINESS_TZ", "Europe/Amsterdam")
    tz = pytz.timezone(tz_name)

    # Parse requested date in local tz
    date_local = datetime.strptime(date_str, "%Y-%m-%d")

    hours = get_business_hours(date_local)
    if hours is None:
        return AvailabilityResponse(
            date=date_str, timezone=tz_name, slot_minutes=slot_minutes, slots=[], configured=bool(os.getenv("GOOGLE_CALENDAR_ICAL_URL"))
        )

    open_t, close_t = hours
    start = tz.localize(datetime.combine(date_local.date(), open_t))
    end = tz.localize(datetime.combine(date_local.date(), close_t))

    busy = fetch_busy_intervals_ical(date_local, tz)

    # Iterate through the working window and add slots that do not overlap busy intervals
    cur = start
    slots: List[str] = []
    delta = timedelta(minutes=slot_minutes)

    def is_free(s: datetime, e: datetime) -> bool:
        for b_start, b_end in busy:
            if s < b_end and e > b_start:
                return False
        return True

    while cur + delta <= end:
        s = cur
        e = cur + delta
        if is_free(s, e):
            slots.append(s.isoformat())
        cur += delta

    return AvailabilityResponse(
        date=date_str, timezone=tz_name, slot_minutes=slot_minutes, slots=slots, configured=bool(os.getenv("GOOGLE_CALENDAR_ICAL_URL")),
    )


@app.get("/api/availability", response_model=AvailabilityResponse)
def availability(date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"), slot_minutes: int = 30):
    """
    Returns available start times for the given date, based on business hours
    and busy events pulled from a public Google Calendar iCal feed.

    Environment variables:
    - GOOGLE_CALENDAR_ICAL_URL: Public iCal URL of Sanne's Google Calendar (no auth required)
    - BUSINESS_TZ: IANA timezone (default Europe/Amsterdam)
    """
    return generate_slots(date, slot_minutes)


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
