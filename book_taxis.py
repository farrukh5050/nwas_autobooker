import os
import random
import pandas as pd
import requests
import json
from datetime import datetime
from dotenv import load_dotenv
import sys
from pathlib import Path
from pytz import timezone

# Ensure dotenv works inside PyInstaller .exe
if getattr(sys, "frozen", False):
    # Running inside PyInstaller bundle
    base_path = Path(getattr(sys, "_MEIPASS"))
else:
    # Running normally
    base_path = Path(__file__).parent



# Load environment variables
dotenv_path = base_path / ".env"
load_dotenv(dotenv_path)

AUTOCAB_API_KEY = str(os.getenv("AUTOCAB_API_KEY"))
COMPANY_ID = os.getenv("COMPANY_ID")
CUSTOMER_ID = os.getenv("CUSTOMER_ID")

booking_url = "https://autocab-api.azure-api.net/booking/v1/booking"
session = requests.Session()
session.headers.update(
    {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "Ocp-Apim-Subscription-Key": AUTOCAB_API_KEY,
    }
)

def _today_log_path():
    today_str = datetime.now().strftime("%Y-%m-%d")
    log_dir = "json_data/bookings_log"
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, f"bookings_log_{today_str}.json")


def has_already_booked_group(jrny_ids):
    """
    Return True only if the *exact same set* of jrny_ids has already been booked today.
    """
    path = _today_log_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            booking_log = json.load(f)
    except FileNotFoundError:
        return False

    jrny_id_set = {("j_id " + str(jid)).strip() for jid in jrny_ids}

    # log maps booking_id -> list of "j_id X"
    for booked_j_ids in booking_log.values():
        if set(booked_j_ids) == jrny_id_set:
            return True
    return False


def log_booking(jrny_ids, booking_id):
    today_str = datetime.now().strftime("%Y-%m-%d")
    log_dir = "json_data/bookings_log"
    os.makedirs(log_dir, exist_ok=True)  # Ensure folder exists

    log_path = os.path.join(log_dir, f"bookings_log_{today_str}.json")

    try:
        with open(log_path, "r", encoding="utf-8") as f:
            booking_log = json.load(f)
    except FileNotFoundError:
        booking_log = {}

    # Ensure booking_id is a string for consistent JSON keys
    booking_id_str = str(booking_id)

    # Add new j_ids to this booking_id
    existing = set(booking_log.get(booking_id_str, []))
    new = {"j_id " + str(jid) for jid in jrny_ids}
    combined = list(existing.union(new))

    # Update log
    booking_log[booking_id_str] = combined

    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(booking_log, f, indent=2)


class FakeResponse:
    def __init__(self):
        self.status_code = 200
        self.text = "OK"
        self._booking_id = random.randint(100000, 999999)

    def json(self):
        return {
            "bookingId": self._booking_id
        }


def make_booking(run_data):
    capabilities = run_data.get("capabilities", [])
    payload = {
        "capabilities": capabilities,
        "companyId": run_data.get("companyId", COMPANY_ID),
        "customerId": run_data.get("customerId", CUSTOMER_ID),
        "pickup": run_data["pickup"],
        "vias": run_data.get("vias", []),
        "destination": run_data["destination"],
        "driverNote": (
            run_data.get("driverNote", "PLEASE GO INSIDE / PLEASE KNOCK ON DOOR")
            or "PLEASE GO INSIDE / PLEASE KNOCK ON DOOR"
        )[:250],
        "name": run_data.get("name", "AUTO BOOKING"),
        "pickupDueTime": run_data.get(
            "pickupDueTime", datetime.now().isoformat()
        ),
        "yourReferences": run_data.get("yourReferences", {"yourReference1": ""}),
        "telephoneNumber": run_data.get("telephoneNumber", ""),
        "officeNote": run_data.get("officeNote", ""),
        "hold": run_data.get("hold", True),
    }

    your_ref_string = payload["yourReferences"]["yourReference1"]
    jrny_ids = [r.strip() for r in your_ref_string.split("+") if r.strip()]

    if has_already_booked_group(jrny_ids):
        print(f"Skipping booking; identical journey group already booked today: {jrny_ids}")
        return {
            "status": "skipped",
            "reason": "duplicate_group",
            "bookingId": None,
            "jrny_ids": jrny_ids,
            "status_code": None,
            "raw": None,
        }

    try:
        response = session.post(booking_url, json=payload, timeout=10)

        # response = FakeResponse()  # Use fake response for testing without hitting real API
        print(f"Booking response: {response.status_code} - {response.text}")
    except requests.exceptions.RequestException as e:
        print(f"API request error: {e}")
        return {
            "status": "error",
            "reason": "request_exception",
            "bookingId": None,
            "jrny_ids": jrny_ids,
            "status_code": None,
            "raw": str(e),
        }

    if response.status_code not in (200, 201):
        return {
            "status": "error",
            "reason": "http_error",
            "bookingId": None,
            "jrny_ids": jrny_ids,
            "status_code": response.status_code,
            "raw": response.text,
        }

    try:
        response_data = response.json()
    except Exception as e:
        return {
            "status": "error",
            "reason": "bad_json",
            "bookingId": None,
            "jrny_ids": jrny_ids,
            "status_code": response.status_code,
            "raw": response.text,
        }

    booking_id = response_data.get("bookingId") or response_data.get("id")
    if not booking_id:
        return {
            "status": "error",
            "reason": "missing_booking_id",
            "bookingId": None,
            "jrny_ids": jrny_ids,
            "status_code": response.status_code,
            "raw": response_data,
        }

    # log successful booking with booking_id and associated jrny_ids
    log_booking(jrny_ids, booking_id)

    return {
        "status": "booked",
        "reason": None,
        "bookingId": booking_id,
        "jrny_ids": jrny_ids,
        "status_code": response.status_code,
        "raw": response_data,
    }


def main():
    # Load routing output JSON
    with open("json_data/ORS_routed_output.json", "r", encoding="utf-8") as f:
        data = json.load(f)

    print("Starting job booking process...\n")
    skipped = 0
    success = 0

    for run_name, details in data.items():
        error_msg = details.get("error")
        if error_msg:
            print(f"Skipping {run_name} due to error: {error_msg}")
            skipped += 1
            continue

        try:
            make_booking(details)
            success += 1
        except Exception as e:
            print(f"booking failed: {e}")

    print("\nAll bookings processed.")
    print(f"Successful: {success}")
    print(f"Skipped or failed: {skipped}")


if __name__ == "__main__":
    main()
