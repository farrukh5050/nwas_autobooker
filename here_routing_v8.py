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


def shift_datetime_one_year(value):
    try:
        return value.replace(year=value.year + 1)
    except ValueError:
        return value.replace(month=2, day=28, year=value.year + 1)


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


def trim_address_text(text, town="", post_code=""):
    if not text:
        return ""

    trimmed_text = str(text).strip()
    parts = [part.strip() for part in trimmed_text.split(",")]

    while parts and post_code and parts[-1].lower() == str(post_code).strip().lower():
        parts.pop()

    while parts and town and parts[-1].lower() == str(town).strip().lower():
        parts.pop()

    if not parts:
        return ""

    return ", ".join(parts)


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
        "Rochdale Infirmary"
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
    clean_note(p.get("notes"))
    for p in passengers
        if p.get("notes") 
        and str(p.get("notes")).strip().lower() != "nan")

    if not job_note:
        job_note = "Please go inside"

    refs = " + ".join(
        [str(int(p.get("jrny_id"))) for p in passengers if pd.notna(p.get("jrny_id"))]
    )

    # now we return extra_phones as well
    return names, primary_phone, job_note, refs, extra_phones


def adjust_pickup_time(passengers, pickup_is_hospital, destination_is_hospital, pickup_coord, dest_coord, pickup_text, shift_year=False,):
    passenger_times = [
        (p, pd.to_datetime(p.get("formatted_time")))
        for p in passengers
        if pd.notna(p.get("formatted_time"))
    ]

    times = [
        t for p, t in passenger_times
        if not pickup_is_hospital
        or apply_ghost_name(str(p.get("from_address", ""))) == pickup_text
    ]

    if not times:
        return None

    base_time = min(times)

    if shift_year:
        base_time = shift_datetime_one_year(base_time)

    if pickup_is_hospital and destination_is_hospital:
        base_time -= timedelta(minutes=45)

    elif not pickup_is_hospital:
        distance = (
            geodesic(pickup_coord, dest_coord).miles
            if pickup_coord and dest_coord
            else 0
        )
        base_time -= timedelta(minutes=60 if distance > 10 else 45)

    for p in passengers:
        p_name = str(p.get("name", "")).strip()
        p_from = str(p.get("from_address", "")).strip().lower()

        mapped = time_adjust_map.get(p_name)
        if mapped:
            mapped_addr, minutes = mapped
            if str(mapped_addr).strip().lower() == p_from:
                base_time += timedelta(minutes=minutes)
                break

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


def get_capabilities(passengers, pickup_is_hospital, destination_is_hospital=False):
    has_w1 = any(
        str(p.get("mob", "")).strip().upper() == "W1"
        for p in passengers
    )

    if pickup_is_hospital and destination_is_hospital:
        return [38] if has_w1 else []

    capabilities = []

    if pickup_is_hospital:
        capabilities.extend(CAPABILITIES)

    if has_w1:
        capabilities.append(38)

    return capabilities


def build_runs_to_process(unique_run, run_df):
    specials = run_df[run_df["esc"].astype(str).str.contains("R=1|M=1", na=False)]
    if len(specials) <= 1:
        return [(unique_run, run_df)]

    return [
        (f"{unique_run}_{chr(65 + i)}", run_df.loc[[idx]])
        for i, idx in enumerate(specials.index)
    ]


def find_matching_row_by_coord(rdf, coord, coord_column):
    return next(
        (
            row
            for _, row in rdf.iterrows()
            if row[coord_column] and coords_close(row[coord_column], coord)
        ),
        None,
    )


def format_fallback_text(point):
    coord = point["address"]["coordinate"]
    return (
        f"{point['address']['town']}, "
        f"{coord['latitude']:.6f}, {coord['longitude']:.6f}"
    )


def update_point_from_row(point, row, direction):
    if row is not None:
        ghost_key = f"g_{direction.replace('_address', '')}"
        g_address = row.get(ghost_key) or row.get(direction) or ""
        postcode_key = "from_post_code" if "from" in direction else "to_post_code"
        town_key = "from_town" if "from" in direction else "to_town"
        town = row.get(town_key, "")
        post_code = row.get(postcode_key, "")
        point["address"]["text"] = trim_address_text(
            apply_ghost_name(g_address),
            town=town,
            post_code=post_code,
        )
        point["note"] = extract_middle_note(row.get(direction) or "")
        point["address"]["postCode"] = post_code
        return

    point["address"]["text"] = apply_ghost_name(format_fallback_text(point))
    point["note"] = ""
    point["address"]["postCode"] = ""


def enrich_pickup_point(rdf, pickup):
    pickup_coord = (
        pickup["address"]["coordinate"]["latitude"],
        pickup["address"]["coordinate"]["longitude"],
    )
    matched_pickup = find_matching_row_by_coord(rdf, pickup_coord, "from_coord_parsed")
    update_point_from_row(pickup, matched_pickup, "from_address")


def enrich_via_points(rdf, vias):
    for via in vias:
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
            update_point_from_row(via, row, direction)
        else:
            update_point_from_row(via, None, "to_address")


def enrich_destination_point(rdf, destination):
    dest_coord = (
        destination["address"]["coordinate"]["latitude"],
        destination["address"]["coordinate"]["longitude"],
    )
    matched_dest = find_matching_row_by_coord(rdf, dest_coord, "to_coord_parsed")
    update_point_from_row(destination, matched_dest, "to_address")


def enrich_routing_points(rdf, routing):
    enrich_pickup_point(rdf, routing["pickup"])
    enrich_via_points(rdf, routing["vias"])
    enrich_destination_point(rdf, routing["destination"])
    return routing


def get_pickup_due_time(passengers, routing):
    pickup_text = routing["pickup"]["address"]["text"]
    pickup_coord = routing["pickup"]["address"]["coordinate"]
    dest_coord = routing["destination"]["address"]["coordinate"]
    pickup_is_hospital = is_hospital(pickup_text)
    destination_text = routing["destination"]["address"]["text"]
    destination_is_hospital = is_hospital(destination_text)

    pickup_due_time = adjust_pickup_time(passengers, 
        pickup_is_hospital, 
        destination_is_hospital, 
        (pickup_coord["latitude"], pickup_coord["longitude"]),
        (dest_coord["latitude"], dest_coord["longitude"]),
        pickup_text,
        shift_year=TEST_MODE,
    )

    if pickup_due_time:
        return pickup_due_time, pickup_is_hospital

    try:
        base = pd.to_datetime(passengers[0]["formatted_time"])
    except Exception:
        base = datetime.now()

    if TEST_MODE:
        base = shift_datetime_one_year(base)

    return base.isoformat(), pickup_is_hospital


def build_booking_payload(routing, passengers):
    (
        name,
        primary_phone,
        job_note,
        ref,
        extra_phones,
    ) = build_metadata(passengers)

    pickup_due_time, pickup_is_hospital = get_pickup_due_time(passengers, routing)
    destination_text = routing["destination"]["address"]["text"]
    destination_is_hospital = is_hospital(destination_text)
    capabilities = get_capabilities(
        passengers,
        pickup_is_hospital,
        destination_is_hospital,
    )
    appt_time = pd.to_datetime(passengers[0]["formatted_time"]).strftime("%H:%M")
    office_note = build_office_note(
        passengers=passengers,
        extra_phones=extra_phones,
        appt_time=appt_time,
        pickup_is_hospital=pickup_is_hospital,
    )

    return {
        "capabilities": capabilities,
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


def book_run(payload, db_name):
    booking_response = make_booking(payload)
    status = booking_response.get("status")
    jrny_ids = booking_response.get("jrny_ids", [])

    if status:
        mark_jrny_ids_booked(db_name, jrny_ids=jrny_ids, status=status)
    return booking_response


def generate_json_from_df(df, db_name):
    # create a column with unique run names e.g STCPLPM2_Run 1
    df["unique_run"] = df["cost_center"].astype(str) + "_" + df["run"].astype(str)

    # Pre-parse coordinates once
    df["from_coord_parsed"] = df["from_coord"].map(coord_tuple)
    df["to_coord_parsed"] = df["to_coord"].map(coord_tuple)

    results = {}

    for unique_run, run_df in df.groupby("unique_run"):
        runs_to_process = build_runs_to_process(unique_run, run_df)

        for run_name, rdf in runs_to_process:
            try:
                routing = enrich_routing_points(
                    rdf,
                    classify_run_addresses_with_corrected_vias(rdf),
                )
                passengers = rdf.to_dict(orient="records")
                results[run_name] = build_booking_payload(routing, passengers)

                try:
                    book_run(results[run_name], db_name)
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
