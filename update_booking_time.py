import os
import json
import pandas as pd
import requests
from datetime import datetime, timedelta
from pytz import timezone
from dateutil.parser import isoparse
from collections import defaultdict
from pathlib import Path
from requests.exceptions import RequestException
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from sqlalchemy import select, func
from database.database import session_scope
from database.models import UpdateLogsheet
from database.db_conn import mark_jrny_ids_booked

# Constants
log_dir = Path("json_data/bookings_log")
uk_tz = timezone("Europe/London")
base_url = "https://autocab-api.azure-api.net/booking/v1/booking/"
session = requests.Session()
AUTOCAB_API_KEY = os.getenv("AUTOCAB_API_KEY")
TEST_MODE = os.getenv("TEST_MODE", "False").lower() in ("true", "1", "yes")

retry = Retry(
    total=2,
    backoff_factor=0.4,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "POST", "DELETE"],
    raise_on_status=False,
)

adapter = HTTPAdapter(max_retries=retry)
session.mount("https://", adapter)


def shift_time_to_next_year(iso_datetime_str):
    dt = isoparse(iso_datetime_str)
    try:
        return dt.replace(year=dt.year + 1).isoformat()
    except ValueError:
        return (dt + timedelta(days=365)).isoformat()


def get_today_json():
    today_str = datetime.now(uk_tz).strftime("%Y-%m-%d")
    log_dir.mkdir(parents=True, exist_ok=True)
    booking_log_path = log_dir / f"bookings_log_{today_str}.json"
    try:
        with open(booking_log_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def update_booking_time(booking_id, new_time, jrny_ids):
    get_url = f"{base_url}{booking_id}"
    headers = {
        "Cache-Control": "no-cache",
        "Ocp-Apim-Subscription-Key": AUTOCAB_API_KEY,
    }

    nt = pd.to_datetime(new_time)
    if nt.tzinfo is None:
        nt = uk_tz.localize(nt)

    payload_time = nt.isoformat()
    payload_time_utc = nt.astimezone(timezone("UTC")).isoformat()

    try:
        response = session.get(get_url, headers=headers, timeout=10)
        if response.status_code != 200:
            mark_jrny_ids_booked(UpdateLogsheet, jrny_ids, "skipped")
            return

        data = response.json()
        data["pickupDueTime"] = payload_time
        data["pickupDueTimeUtc"] = payload_time_utc
        data["capabilities"] = []

        response2 = session.post(get_url, headers=headers, json=data, timeout=10)
        ok = response2.status_code in (200, 201)

        mark_jrny_ids_booked(UpdateLogsheet, jrny_ids, "updated" if ok else "skipped")

    except Exception:
        mark_jrny_ids_booked(UpdateLogsheet, jrny_ids, "skipped")
                          

def process_updates(jobs_to_update_df, booking_log):
    jobs_to_update_df["unique_run"] = "j_id " + jobs_to_update_df["jrny_id"].astype(str)

    booking_map_update = {}
    for ur in jobs_to_update_df["unique_run"].dropna().unique():
        booking_id = get_booking_id_for_jid(ur, booking_log)
        if booking_id is not None:
            booking_map_update[ur] = booking_id

    latest_updates = find_latest_times(jobs_to_update_df, booking_map_update)

    for booking_id, update_info in latest_updates.items():
        if TEST_MODE:
            new_time = shift_time_to_next_year(update_info["new_time"])
        else:
            new_time = update_info["new_time"]

        # Clamp time to now if earlier so job is not updated to a past time which would cause errors
        now_time = pd.to_datetime(new_time)
        if now_time.tzinfo is None:
            now_time = uk_tz.localize(now_time)
        now_uk = datetime.now(uk_tz)
        if now_time < now_uk:
            now_time = now_uk

        update_booking_time(booking_id, now_time.isoformat(), update_info["jrny_ids"])


def find_latest_times(df, booking_map):
    df["formatted_time_dt"] = pd.to_datetime(df["formatted_time"])
    booking_to_runs = defaultdict(list)

    for unique_run, booking_id in booking_map.items():
        booking_to_runs[booking_id].append(unique_run)

    results = {}
    for booking_id, runs in booking_to_runs.items():
        subset = df[df["unique_run"].isin(runs)]
        if subset.empty:
            continue
        latest = subset.loc[subset["formatted_time_dt"].idxmax()]
        jrny_ids = subset["jrny_id"].dropna().unique().tolist()
        results[booking_id] = {
            "new_time": latest["formatted_time"],
            "unique_runs": runs,
            "jrny_ids": jrny_ids,
        }

    return results


def get_booking_id_for_jid(jid, booking_log):
    """
    Return the highest (latest) booking ID that contains the given j_id.
    Assumes newer bookings have higher booking ID numbers.
    """
    matching_ids = [
        int(booking_id) for booking_id, jids in booking_log.items() if jid in jids
    ]

    if not matching_ids:
        return None

    return str(max(matching_ids))


def process_cancel_jobs(jobs_to_cancel_df, booking_log):
    jobs_to_cancel_df["unique_run"] = "j_id " + jobs_to_cancel_df["jrny_id"].astype(str)
    booking_map = {}

    for _, row in jobs_to_cancel_df.iterrows():
        ur = row["unique_run"]
        booking_id = get_booking_id_for_jid(ur, booking_log)
        if booking_id:
            booking_map[ur] = booking_id

    for booking_id in booking_map.values():
        del_url = f"{base_url}{booking_id}"
        headers = {
            "Cache-Control": "no-cache",
            "Ocp-Apim-Subscription-Key": AUTOCAB_API_KEY,
        }
        try:
            response = session.delete(del_url, headers=headers, timeout=10)
            if response.status_code != 200:
                print(f"Failed to cancel booking ID {booking_id}")
            else:
                print(f"Cancelled booking ID {booking_id}")
        except RequestException as e:
            print(f"Exception while cancelling booking ID {booking_id}: {e}")

    jrny_ids = [key.replace("j_id ", "") for key in booking_map.keys()]
    mark_jrny_ids_booked(UpdateLogsheet, jrny_ids, "updated")


def load_jobs_to_process():
    with session_scope() as session:
        status_norm = func.lower(func.trim(func.coalesce(UpdateLogsheet.status, "")))
        columns = [c for c in UpdateLogsheet.__table__.columns]
        stmt = select(*columns).where(status_norm.notin_(["updated", "skipped"]))
        results = session.execute(stmt).mappings().all()
        return pd.DataFrame(results)


def main():
    # Step 1: reload Excel (some rows will now be marked as booked/updated)
    booking_log = get_today_json()
    combined_df = load_jobs_to_process()

    if combined_df.empty:
        print("No jobs to update or cancel.")
        return

    # Step 2: proceed with normal update/cancel
    jobs_to_update_df = combined_df[
        (combined_df["type"] == "R") & (combined_df["status"] != "updated")
    ].copy()

    jobs_to_cancel_df = combined_df[
        (combined_df["type"].isin(["ABORTED", "CANCELLED"]))
        & (combined_df["status"] != "updated")
    ].copy()


    print(f"Jobs to Update:{len(jobs_to_update_df)}")
    process_updates(jobs_to_update_df, booking_log)
    print(f"Jobs to Cancel:{len(jobs_to_cancel_df)}")
    process_cancel_jobs(jobs_to_cancel_df, booking_log)


if __name__ == "__main__":
    main()
