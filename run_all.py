import time
import random
import logging
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo  # Python 3.9+
import pytz
from sqlalchemy import delete, select

from database.database import init_db, init_sqlite, session_scope
from database.models import NwasLogsheet, UpdateLogsheet, RebookJobs, AppMeta

# === Your tasks ===
from get_nwas_data import main as run_nwas
from get_address_from_ghost import main as run_ghost
from update_booking_time import main as update_booking_time

uk_tz = pytz.timezone("Europe/London")

RESET_KEY = "nwas_logsheet_last_reset_date"  # stored as YYYY-MM-DD

# === Config ===
TZ = ZoneInfo("Europe/London")
OPERATING_START_HOUR = 5
OPERATING_END_HOUR = 21
INTERVAL_MINUTES = 5
LOG_MAX_BYTES = 2_000_000
LOG_BACKUP_COUNT = 5
JITTER_MAX_SECONDS = 1.5


def reset_db_once_per_day():
    """
    Wipe the nwas_logsheet table once per new UK day.
    Returns True if a reset happened, otherwise False.

    NOTE: Assumes init_sqlite() and init_db() already ran.
    """
    today_str = datetime.now(uk_tz).date().isoformat()

    with session_scope() as db:
        # Read last reset date from meta
        last_reset_date = db.execute(
            select(AppMeta.value).where(AppMeta.key == RESET_KEY)
        ).scalar_one_or_none()

        if last_reset_date == today_str:
            return False

        # Reset the tables for the new day
        db.execute(delete(NwasLogsheet))
        db.execute(delete(UpdateLogsheet))
        db.execute(delete(RebookJobs))

        # Update the last reset date in meta
        row = db.get(AppMeta, RESET_KEY)
        if row is None:
            db.add(AppMeta(key=RESET_KEY, value=today_str))
        else:
            row.value = today_str


# === Time helpers ===
def now() -> datetime:
    return datetime.now(TZ)


def within_operating_hours(dt: datetime) -> bool:
    start = dt.replace(hour=OPERATING_START_HOUR, minute=0, second=0, microsecond=0)
    end = dt.replace(hour=OPERATING_END_HOUR, minute=0, second=0, microsecond=0)
    return start <= dt < end


def seconds_until_next_start(dt: datetime) -> float:
    if within_operating_hours(dt):
        return 0.0
    start_today = dt.replace(
        hour=OPERATING_START_HOUR, minute=0, second=0, microsecond=0
    )
    if dt < start_today:
        target = start_today
    else:
        target_date = (dt + timedelta(days=1)).date()
        target = datetime.combine(target_date, dtime(hour=OPERATING_START_HOUR), TZ)
    return max(0.0, (target - dt).total_seconds())


def seconds_until_end(dt: datetime) -> float:
    end = dt.replace(hour=OPERATING_END_HOUR, minute=0, second=0, microsecond=0)
    return max(0.0, (end - dt).total_seconds())


def capped_sleep(seconds: float) -> None:
    jitter = random.uniform(0, JITTER_MAX_SECONDS) if seconds > 0 else 0.0
    time.sleep(max(0.0, seconds) + jitter)


def next_reset_datetime(dt: datetime) -> datetime:
    """
    Next daily reset boundary at OPERATING_START_HOUR (5am UK by default).
    """
    reset_today = dt.replace(
        hour=OPERATING_START_HOUR, minute=0, second=0, microsecond=0
    )
    if dt < reset_today:
        return reset_today
    # tomorrow
    return (dt + timedelta(days=1)).replace(
        hour=OPERATING_START_HOUR, minute=0, second=0, microsecond=0
    )


# === Main loop ===
def main() -> None:
    interval_seconds = INTERVAL_MINUTES * 60

    # Init DB once
    init_sqlite()
    init_db()

    # Run reset check ONCE at startup (covers missed 5am + restarts)
    reset_db_once_per_day()

    # Then schedule next reset boundary in memory (no repeated checks)
    next_reset_at = next_reset_datetime(now())

    try:
        while True:
            t = now()

            # If outside hours, sleep to the next start
            wait = seconds_until_next_start(t)
            if wait > 0:
                logging.info(
                    f"Outside operating hours (5am–9pm). Sleeping until {now() + timedelta(seconds=wait)}"
                )
                capped_sleep(wait)
                continue

            # Only attempt reset when we cross the boundary (once/day)
            if t >= next_reset_at:
                reset_db_once_per_day()
                next_reset_at = next_reset_datetime(t)

            # Run tasks (errors per task)
            logging.info(f"Running jobs at {now().strftime('%Y-%m-%d %H:%M:%S %Z')}")
            for name, fn in [
                # Get NWAS data and save to DB
                ("run_nwas", run_nwas),
                # Get address from ghost and update DB
                ("run_ghost", run_ghost),
                # Update booking times in Autocab based on NWAS data
                ("update_booking_time", update_booking_time),
            ]:
                try:
                    fn()
                    logging.info(f"{name} completed successfully")
                except SystemExit as e:  # PATCH: don't let sys.exit kill the scheduler
                    logging.warning(
                        f"{name} exited with code {getattr(e, 'code', None)}; continuing scheduler"
                    )
                except Exception as e:
                    logging.error(f"{name} failed: {e!r}")

            # Sleep until next cycle or cutoff
            remaining_today = seconds_until_end(now())
            sleep_for = (
                min(interval_seconds, remaining_today) if remaining_today > 0 else 0
            )
            if sleep_for == 0:
                continue
            capped_sleep(sleep_for)

    except KeyboardInterrupt:
        logging.warning("Stopped by user.")


if __name__ == "__main__":
    main()
