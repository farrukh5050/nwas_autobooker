import os
import traceback
import requests
import pandas as pd
import json
from datetime import datetime, timedelta
from geopy.distance import geodesic
import re
from datetime import timedelta
from book_taxis import make_booking
from database.db_conn import mark_jrny_ids_booked
import sys
from pathlib import Path
from dotenv import load_dotenv
import html

# Ensure dotenv works inside PyInstaller .exe
if getattr(sys, "frozen", False):
    base_path = Path(sys.executable).parent
else:
    base_path = Path(__file__).parent

dotenv_path = base_path / ".env"

load_dotenv(dotenv_path)

HERE_API_KEY = os.getenv("HERE_API_KEY")
API_URL = "https://wps.hereapi.com/v8/findsequence2"
TEST_MODE = os.getenv("TEST_MODE", "False").lower() in ("true", "1", "yes")
COMPANY_ID=os.getenv("COMPANY_ID")
CUSTOMER_ID=os.getenv("CUSTOMER_ID")

def parse_int_list(value):
    if not value:
        return []
    return [int(x.strip()) for x in value.split(",") if x.strip().isdigit()]

CAPABILITIES=parse_int_list(os.getenv("CAPABILITIES"))
FORBIDDEN_DRIVERS = parse_int_list(os.getenv("FORBIDDEN_DRIVERS"))
FORBIDDEN_VEHICLES = parse_int_list(os.getenv("FORBIDDEN_VEHICLES"))

POSTCODE_RE = re.compile(
    r"""
    ^[A-Z]{1,2}\d{1,2}[A-Z]?\s*\d[A-Z]{2}$   # loose UK postcode matcher (e.g. M40 1LQ, OL2 6JG)
""",
    re.IGNORECASE | re.VERBOSE,
)


ghost_name_df = pd.read_excel("xl_data/hospital names.xlsx")
time_adjust_df = pd.read_excel("xl_data/time_adjust.xlsx")
output_path = "json_data/ORS_routed_output.json"

ghost_name_map = {
    str(row["pts name"]).strip().lower(): str(row["ghost name"]).strip()
    for _, row in ghost_name_df.iterrows()
    if pd.notna(row["pts name"]) and pd.notna(row["ghost name"])
}

time_adjust_map = {
    str(row["name"].strip()): (
        str(row["address"].strip()),
        int(row["time adjust"]),
    )
    for _, row in time_adjust_df.iterrows()
}


def apply_ghost_name(name):
    if not isinstance(name, str):
        return name
    name_clean = name.strip().lower()
    if name_clean in ghost_name_map:
        return ghost_name_map[name_clean]
    for pts_name in sorted(ghost_name_map.keys(), key=len, reverse=True):
        if pts_name in name_clean:
            return ghost_name_map[pts_name]
    return name


def decode_zone(zone_obj):
    if isinstance(zone_obj, str):
        try:
            return json.loads(zone_obj)
        except:
            return {}
    return zone_obj if isinstance(zone_obj, dict) else {}


def coord_tuple(coord_str):
    try:
        if pd.isna(coord_str):
            return None
        coord_str = str(coord_str).replace("(", "").replace(")", "").strip()
        coord_str = re.sub(r"\s+", " ", coord_str)
        if "," not in coord_str and " " in coord_str:
            coord_str = coord_str.replace(" ", ",")
        parts = coord_str.split(",")
        if len(parts) != 2:
            return None
        return tuple(map(float, parts))
    except:
        return None


def is_hospital(address):
    address_lower = address.lower()
    keywords = [
        "hexagon",
        "christie",
        "salford royal",
        "hospital",
        "infirmary",
        "dialysis",
        "royal manchester",
        "ladywell building",
        "north west heart centre",
        "mft",
        "north manchester community diagnostic centre",
        "turnberg building",
        "irving building",
        "octagon house",
        "Radcliffe Primary Care",
    ]
    return any(k in address_lower for k in keywords)


def extract_middle_note(address):
    if not isinstance(address, str):
        return ""

    # split and clean empty chunks
    parts = [p.strip() for p in address.split(",") if p.strip()]
    if not parts:
        return ""

    flat_keywords = ("flat", "apartment", "apt", "unit", "suite")

    # helper: check if a string is just a number (optionally with a letter suffix)
    def is_bare_number(s: str) -> bool:
        return bool(re.fullmatch(r"\d+[A-Za-z]?", s.strip()))

    # 1) Prefer flat/apartment-style chunk (+ next segment if sensible)
    for i, part in enumerate(parts):
        if any(k in part.lower() for k in flat_keywords):
            note_parts = [part]
            if i + 1 < len(parts) and not POSTCODE_RE.match(
                parts[i + 1].replace(" ", "")
            ):
                note_parts.append(parts[i + 1])
            return ", ".join(note_parts)

    # 2) Fallbacks (but skip bare numbers)
    if POSTCODE_RE.match(parts[0].replace(" ", "")) and len(parts) >= 3:
        if not is_bare_number(parts[1]):
            return parts[1]

    if len(parts) == 3 and not is_bare_number(parts[1]):
        return parts[1]

    return ""


def clean_note(note: str) -> str:
    if not isinstance(note, str):
        return ""
    note = html.unescape(note)
    note = re.sub(r"[#&;]", "", note)  # remove special chars
    note = re.sub(r"\s+", " ", note)  # collapse whitespace
    return note.strip()


def normalize_phone(phone):
    if isinstance(phone, (float, int)):
        s = str(int(phone))
    else:
        s = str(phone).strip()
    # if it's numeric, normalise to 11 digits like before
    return s.zfill(11) if s.isdigit() and len(s) <= 11 else s


def build_metadata(passengers):
    names = " + ".join(
        [p.get("name", "").strip() for p in passengers if pd.notna(p.get("name"))]
    )
    has_escort = any(
        "R=1" in str(p.get("esc", "")) or "M=1" in str(p.get("esc", ""))
        for p in passengers
    )
    if has_escort:
        names += " + 1"

    # collect ALL phone numbers
    raw_phones = [p.get("phone_number") for p in passengers if pd.notna(p.get("phone_number"))]
    phones = [normalize_phone(p) for p in raw_phones]

    primary_phone = phones[0] if phones else ""
    extra_phones = phones[1:] if len(phones) > 1 else []

    job_note = "; ".join(
        [clean_note(p.get("notes", "")) for p in passengers if pd.notna(p.get("notes"))]
    )
    refs = " + ".join(
        [str(int(p.get("jrny_id"))) for p in passengers if pd.notna(p.get("jrny_id"))]
    )

    mob = [p.get("mob") for p in passengers]
    # now we return extra_phones as well
    return names, primary_phone, job_note, refs, extra_phones, mob


def adjust_pickup_time_next_year(
    passengers, pickup_is_hospital, pickup_coord, dest_coord, pickup_text
):
    # 1) Collect (passenger, time) pairs safely
    passenger_times = []
    for p in passengers:
        ft = p.get("formatted_time")
        if pd.notna(ft):
            passenger_times.append((p, pd.to_datetime(ft)))

    # 2) Filter times for hospital pickups to only those matching the pickup_text
    if pickup_is_hospital:
        times = [
            t
            for p, t in passenger_times
            if apply_ghost_name(str(p.get("from_address", ""))) == pickup_text
        ]
    else:
        times = [t for _, t in passenger_times]

    if not times:
        return None

    # Base time = earliest relevant time
    base_time = min(times)

    # --- NEW: always push year forward by +1 ---
    try:
        base_time = base_time.replace(year=base_time.year + 1)
    except ValueError:
        # handles leap-day (Feb 29 → Feb 28 next year if not leap year)
        base_time = base_time.replace(month=2, day=28, year=base_time.year + 1)

    # 3) Apply distance-based offset FIRST (only for non-hospital)
    if not pickup_is_hospital:
        distance = (
            geodesic(pickup_coord, dest_coord).miles
            if pickup_coord and dest_coord
            else 0
        )
        adjust_by = timedelta(minutes=60 if distance > 10 else 45)
        base_time = base_time - adjust_by

    # 4) Apply per-passenger time adjustment from time_adjust_map (name + from match)
    adjust_minutes = 0
    for p in passengers:
        p_name = str(p.get("name", "")).strip()
        p_from = str(p.get("from_address", "")).strip().lower()

        if p_name in time_adjust_map:
            mapped_addr, minutes = time_adjust_map[p_name]
            mapped_addr = str(mapped_addr).strip().lower()
            if mapped_addr == p_from:
                adjust_minutes += minutes
                break  # only apply first matching adjustment

    if adjust_minutes != 0:
        base_time = base_time + timedelta(minutes=adjust_minutes)

    # 5) Final ISO timestamp
    return base_time.isoformat()


def adjust_pickup_time(
    passengers, pickup_is_hospital, pickup_coord, dest_coord, pickup_text
):
    # 1) Collect (passenger, time) pairs safely
    passenger_times = []
    for p in passengers:
        ft = p.get("formatted_time")
        if pd.notna(ft):
            passenger_times.append((p, pd.to_datetime(ft)))

    # 2) Filter times for hospital pickups to only those matching the pickup_text
    if pickup_is_hospital:
        times = [
            t
            for p, t in passenger_times
            if apply_ghost_name(str(p.get("from_address", ""))) == pickup_text
        ]
    else:
        times = [t for _, t in passenger_times]

    if not times:
        return None

    # Base time = earliest relevant time
    base_time = min(times)

    # 3) Apply distance-based offset FIRST (only for non-hospital)
    if not pickup_is_hospital:
        distance = (
            geodesic(pickup_coord, dest_coord).miles
            if pickup_coord and dest_coord
            else 0
        )
        adjust_by = timedelta(minutes=60 if distance > 10 else 45)
        base_time = base_time - adjust_by

    # 4) Apply per-passenger time adjustment from time_adjust_map (name + from match)
    adjust_minutes = 0
    for p in passengers:
        p_name = str(p.get("name", "")).strip()
        p_from = str(p.get("from_address", "")).strip().lower()

        if p_name in time_adjust_map:
            mapped_addr, minutes = time_adjust_map[p_name]
            mapped_addr = str(mapped_addr).strip().lower()
            if mapped_addr == p_from:
                adjust_minutes += minutes
                break  # only apply first matching adjustment

    if adjust_minutes != 0:
        base_time = base_time + timedelta(minutes=adjust_minutes)

    # 5) Final ISO timestamp
    return base_time.isoformat()


def find_optimal_route_from_coords(run_df):
    HERE_API_KEY = os.getenv("HERE_API_KEY")
    if not HERE_API_KEY:
        raise ValueError("Missing HERE_API_KEY")

    passengers = run_df.to_dict(orient="records")

    waypoints, seen_keys = [], set()

    for p in passengers:
        for label, coord_key, addr_key in [
            ("pickup", "from_coord", "from_norm"),
            ("drop", "to_coord", "to_norm"),
        ]:
            coord = coord_tuple(p.get(coord_key))
            addr = p.get(addr_key)
            if coord and addr:
                # Deduplicate by full address string (not just coordinates)
                key = f"{label}:{addr.lower().strip()}"
                if key not in seen_keys:
                    waypoints.append(
                        {
                            "label": label,
                            "coord": coord,
                            "original": p.get(addr_key),
                            "zone": p.get(f"{coord_key.split('_')[0]}_zone_obj", {}),
                            "town": (
                                ""
                                if pd.isna(p.get(f"{coord_key.split('_')[0]}_town", ""))
                                else p.get(
                                    f"{coord_key.split('_')[0]}_town", ""
                                ).strip()
                            ),
                        }
                    )
                    seen_keys.add(key)

    if not waypoints or len(waypoints) < 2:
        return []

    pickups = [wp for wp in waypoints if wp["label"] == "pickup"]
    drops = [wp for wp in waypoints if wp["label"] == "drop"]

    zone = decode_zone(pickups[0]["zone"]) if pickups else {}
    # ➤ SHORT CIRCUIT if only one pickup and one drop
    if len(pickups) == 1 and len(drops) == 1:
        return [
            {
                "label": "pickup",
                "coord": pickups[0]["coord"],
                "type": "pickup",
                "original": pickups[0]["original"],
                "coordinate": {
                    "latitude": pickups[0]["coord"][0],
                    "longitude": pickups[0]["coord"][1],
                },
                "zone": pickups[0]["zone"],
                "zoneId": zone.get("id", 0),
                "town": pickups[0]["town"],
            },
            {
                "label": "drop",
                "coord": drops[0]["coord"],
                "type": "drop",
                "original": drops[0]["original"],
                "coordinate": {
                    "latitude": drops[0]["coord"][0],
                    "longitude": drops[0]["coord"][1],
                },
                "zone": drops[0]["zone"],
                "zoneId": decode_zone(drops[0]["zone"]).get("id", 0),
                "town": drops[0]["town"],
            },
        ]

    # Select start as furthest pickup from all drops
    start_wp = max(
        pickups,
        key=lambda p: max(geodesic(p["coord"], d["coord"]).miles for d in drops),
    )

    # Select end as furthest drop from all pickups
    end_wp = max(
        drops,
        key=lambda d: max(geodesic(d["coord"], p["coord"]).miles for p in pickups),
    )

    # All others are via points
    intermediate_wps = [wp for wp in waypoints if wp not in (start_wp, end_wp)]

    # Prepare HERE API parameters
    params = {
        "apikey": HERE_API_KEY,
        "mode": "fastest;car;traffic:disabled",
        "start": f"{start_wp['original']};{start_wp['coord'][0]},{start_wp['coord'][1]}",
        "end": f"{end_wp['original']};{end_wp['coord'][0]},{end_wp['coord'][1]}",
        "improveFor": "time",
    }

    for i, wp in enumerate(intermediate_wps, start=1):
        params[f"destination{i}"] = (
            f"{wp['original']};{wp['coord'][0]},{wp['coord'][1]}"
        )

    response = requests.get(API_URL, params=params)
    if response.status_code != 200:
        raise Exception(f"HERE API error: {response.status_code} — {response.text}")

    data = response.json()
    if isinstance(data, dict) and "results" in data:
        sequence = data["results"][0].get("waypoints", [])
    else:
        raise Exception(f"Unexpected API response format: {type(data)} — {data}")

    coord_index_map = {
        (round(wp["coord"][0], 7), round(wp["coord"][1], 7)): wp
        for wp in [start_wp, end_wp] + intermediate_wps
    }

    ordered_points = []
    for step in sequence:
        key = (round(step["lat"], 7), round(step["lng"], 7))
        wp = coord_index_map.get(key)
        if wp:
            ordered_points.append(
                {
                    "label": wp["label"],
                    "coord": wp["coord"],
                    "type": wp["label"],
                    "original": wp["original"],
                    "coordinate": {
                        "latitude": wp["coord"][0],
                        "longitude": wp["coord"][1],
                    },
                    "zone": wp["zone"],
                    "zoneId": decode_zone(wp["zone"]).get("id", 0),
                    "town": wp["town"],
                }
            )

    # Business rule: ensure every pickup is sequenced before any drop
    pickups_seq = [p for p in ordered_points if p["label"] == "pickup"]
    drops_seq = [p for p in ordered_points if p["label"] == "drop"]
    ordered_points = pickups_seq + drops_seq

    return ordered_points


def classify_run_addresses_with_corrected_vias(run_df):
    points = find_optimal_route_from_coords(run_df)
    if len(points) < 2:
        return {"pickup": {}, "destination": {}, "vias": []}

    # First pickup
    pickup = next((p for p in points if p["type"] == "pickup"), None)
    # Last drop
    destination = next((p for p in reversed(points) if p["type"] == "drop"), None)

    # All other points become vias (even if they are pickups or drops)
    vias = [p for p in points if p not in (pickup, destination)]

    def format_point(p):
        zone = decode_zone(p["zone"])
        return {
            "address": {
                "text": p["original"],
                "zone": zone,
                "zoneId": zone.get("id", 0),
                "coordinate": p["coordinate"],
                "town": p["town"],
            }
        }

    return {
        "pickup": format_point(pickup),
        "destination": format_point(destination),
        "vias": [
            {"type": "Via", "address": format_point(via)["address"], "note": ""}
            for via in vias
        ],
    }


def coords_close(coord1, coord2, tol=1e-5):
    """Return True if coordinates are within a small tolerance."""
    return abs(coord1[0] - coord2[0]) < tol and abs(coord1[1] - coord2[1]) < tol


def build_office_note(passengers, extra_phones, appt_time, pickup_is_hospital):
    # Build named extra phones from passengers, skipping the first phone-bearing passenger
    phone_bearing_passengers = []
    for p in passengers:
        phone = p.get("phone_number")
        if pd.notna(phone) and str(phone).strip():
            phone_bearing_passengers.append((str(p.get("name", "")), normalize_phone(phone)))

    named_extras = [
        f"{name}: {phone}"
        for name, phone in phone_bearing_passengers[1:]  # skip primary
        if phone
    ]

    # Fallback: if passenger phone mapping failed but extra_phones exists, use raw extras
    if not named_extras and extra_phones:
        named_extras = [normalize_phone(phone) for phone in extra_phones if str(phone).strip()]

    if pickup_is_hospital:
        return " | ".join(named_extras) if named_extras else f"APPT Time {appt_time}"

    parts = [f"APPT Time {appt_time}"]
    if named_extras:
        parts.extend(named_extras)

    return " | ".join(parts)

def get_capabilities(passengers, pickup_is_hospital):
    has_w1 = any(
        str(p.get("mob")).strip().upper() == "W1"
        for p in passengers
    )

    # Rule 1: W1 + hospital
    if has_w1 and pickup_is_hospital:
        return [35, 38]

    # (optional) W1 but NOT hospital
    if has_w1:
        return [38]  # N capability

    # fallback (your existing logic)
    if pickup_is_hospital:
        return CAPABILITIES # [35] default

    return []

def generate_json_from_df(df, db_name):
    df["unique_run"] = df["cost_center"].astype(str) + "_" + df["run"].astype(str)

    # Pre-parse coordinates once
    df["from_coord_parsed"] = df["from_coord"].map(coord_tuple)
    df["to_coord_parsed"] = df["to_coord"].map(coord_tuple)

    results = {}

    for unique_run, run_df in df.groupby("unique_run"):
        specials = run_df[run_df["esc"].astype(str).str.contains("R=1|M=1", na=False)]
        runs_to_process = (
            [
                (f"{unique_run}_{chr(65 + i)}", run_df.loc[[idx]])
                for i, idx in enumerate(specials.index)
            ]
            if len(specials) > 1
            else [(unique_run, run_df)]
        )

        for run_name, rdf in runs_to_process:
            try:
                routing = classify_run_addresses_with_corrected_vias(rdf)

                pickup = routing["pickup"]
                pickup_coord = (
                    pickup["address"]["coordinate"]["latitude"],
                    pickup["address"]["coordinate"]["longitude"],
                )
                matched_pickup = next(
                    (
                        row
                        for _, row in rdf.iterrows()
                        if row["from_coord_parsed"]
                        and coords_close(row["from_coord_parsed"], pickup_coord)
                    ),
                    None,
                )
                if matched_pickup is not None:
                    g_address = (
                        matched_pickup.get("g_from") or matched_pickup.get("from_address") or ""
                    )
                    pickup["address"]["text"] = apply_ghost_name(g_address)
                    pickup["note"] = extract_middle_note(
                        matched_pickup.get("from_address") or ""
                    )
                else:
                    fallback_text = f"{pickup['address']['town']}, {pickup_coord[0]:.6f}, {pickup_coord[1]:.6f}"
                    pickup["address"]["text"] = apply_ghost_name(fallback_text)
                    pickup["note"] = ""

                for via in routing["vias"]:
                    via_coord = (
                        via["address"]["coordinate"]["latitude"],
                        via["address"]["coordinate"]["longitude"],
                    )
                    matched_row = None
                    for _, row in rdf.iterrows():
                        row_from_coord = row["from_coord_parsed"]
                        row_to_coord = row["to_coord_parsed"]
                        if row_from_coord and coords_close(row_from_coord, via_coord):
                            matched_row = ("from_address", row)
                            break
                        if row_to_coord and coords_close(row_to_coord, via_coord):
                            matched_row = ("to_address", row)
                            break
                    if matched_row is not None:
                        direction, row = matched_row
                        g_address = (row.get(f"g_{direction.replace('_address','')}") or row.get(direction) or "")
                        via["address"]["text"] = apply_ghost_name(g_address)
                        via["note"] = extract_middle_note(row.get(direction, ""))
                    else:
                        fallback_text = f"{via['address']['town']}, {via_coord[0]:.6f}, {via_coord[1]:.6f}"
                        via["address"]["text"] = apply_ghost_name(fallback_text)
                        via["note"] = ""

                destination = routing["destination"]
                dest_coord = (
                    destination["address"]["coordinate"]["latitude"],
                    destination["address"]["coordinate"]["longitude"],
                )
                matched_dest = next(
                    (
                        row
                        for _, row in rdf.iterrows()
                        if row["to_coord_parsed"]
                        and coords_close(row["to_coord_parsed"], dest_coord)
                    ),
                    None,
                )
                if matched_dest is not None:
                    g_address = matched_dest.get("g_to") or matched_dest.get("to_address") or ""
                    destination["address"]["text"] = apply_ghost_name(g_address)
                    destination["note"] = extract_middle_note(
                        matched_dest.get("to_address") or ""
                    )
                else:
                    fallback_text = f"{destination['address']['town']}, {dest_coord[0]:.6f}, {dest_coord[1]:.6f}"
                    destination["address"]["text"] = apply_ghost_name(fallback_text)
                    destination["note"] = ""

                passengers = rdf.to_dict(orient="records")

                name, primary_phone, job_note, ref, extra_phones, mob = build_metadata(
                    passengers
                )

                pickup_text = routing["pickup"]["address"]["text"]
                pickup_coord = routing["pickup"]["address"]["coordinate"]
                dest_coord = routing["destination"]["address"]["coordinate"]
                pickup_is_hospital = is_hospital(pickup_text)
                pickup_due_time = (
                    adjust_pickup_time_next_year(
                        passengers,
                        pickup_is_hospital,
                        (pickup_coord["latitude"], pickup_coord["longitude"]),
                        (dest_coord["latitude"], dest_coord["longitude"]),
                        pickup_text,
                    )
                    if TEST_MODE
                    else adjust_pickup_time(
                        passengers,
                        pickup_is_hospital,
                        (pickup_coord["latitude"], pickup_coord["longitude"]),
                        (dest_coord["latitude"], dest_coord["longitude"]),
                        pickup_text,
                    )
                )

                # Fallback to first passenger's time if adjustment fails
                if not pickup_due_time:
                    try:
                        base = pd.to_datetime(passengers[0]["formatted_time"])
                    except Exception:
                        base = datetime.now()

                    if TEST_MODE:
                        # mirror test-mode behavior: push one year forward
                        try:
                            base = base.replace(year=base.year + 1)
                        except ValueError:
                            base = base.replace(month=2, day=28, year=base.year + 1)

                    pickup_due_time = base.isoformat()

                appt_time = pd.to_datetime(rdf["formatted_time"].iloc[0]).strftime(
                    "%H:%M"
                )

                office_note = build_office_note(
                    passengers=passengers,
                    extra_phones=extra_phones,
                    appt_time=appt_time,
                    pickup_is_hospital=pickup_is_hospital,
                )
                results[run_name] = {
                    "capabilities": get_capabilities(passengers, pickup_is_hospital),
                    "companyId": COMPANY_ID,
                    "customerId": CUSTOMER_ID,
                    "pickup": routing["pickup"],
                    "vias": routing["vias"],
                    "destination": {
                        "address": routing["destination"]["address"],
                        "completed": False,
                        "note": routing["destination"]["note"],
                    },
                    "driverNote": job_note,
                    "name": name,
                    "telephoneNumber": primary_phone,
                    "pickupDueTime": pickup_due_time,
                    "yourReferences": {"yourReference1": ref},
                    "officeNote": office_note,
                    "hold": False,
                    "driverConstraints": {
                    "forbiddenDrivers": FORBIDDEN_DRIVERS,
                    "forbiddenVehicles": FORBIDDEN_VEHICLES,
    },
                }

                try:
                    booking_response = make_booking(results[run_name])
                    # get booking status and if success or skipped, mark jrny_ids as booked in DB
                    status = booking_response.get("status")
                    if status in {"booked", "skipped"}:
                        mark_jrny_ids_booked(db_name ,booking_response["jrny_ids"], status=status)
                except Exception as e:
                    print(f"[ERROR] Booking failed for run {run_name}: {e}")
                    results[run_name]["booking_error"] = str(e)
                    results[run_name]["booking_trace"] = traceback.format_exc()

            except Exception as e:
                results[run_name] = {
                    "error": str(e),
                    "traceback": traceback.format_exc(),
                }

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)


def main():
    excel_path = "xl_data/nwas_logsheet.xlsx"
    xls = pd.ExcelFile(excel_path, engine="openpyxl")
    combined_df = pd.concat(
        [
            pd.read_excel(xls, sheet).assign(cost_center=sheet)
            for sheet in xls.sheet_names
        ],
        ignore_index=True,
    )
    generate_json_from_df(combined_df, db_name="NwasLogsheet")

    print(f"JSON file generated: {output_path}")


if __name__ == "__main__":
    main()
