import os
import requests
import json
import pandas as pd
import sqlite3
from pathlib import Path
import sys
import time
import re
from database.models import NwasLogsheet, RebookJobs
from dotenv import load_dotenv
from sqlalchemy import select, func
from send_email import send_mail
from database.database import session_scope
from here_routing_v8 import generate_json_from_df
from database.db_conn import mark_jrny_ids_booked

# Ensure dotenv works inside PyInstaller .exe
if getattr(sys, "frozen", False):
    base_path = Path(sys.executable).parent
else:
    base_path = Path(__file__).parent

dotenv_path = base_path / ".env"

load_dotenv(dotenv_path)

AUTOCAB_API_KEY = str(os.getenv("AUTOCAB_API_KEY"))
BASE_URL = "https://autocab-api.azure-api.net/booking/v1/addressFromText?text="
ADDRESS_LOOKUP_URL = "https://autocab-api.azure-api.net/booking/v1/lookupAddress?text="
PLACE_ID_URL = "https://autocab-api.azure-api.net/booking/v1/address?placeId="

session = requests.Session()
session.headers.update(
    {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "Ocp-Apim-Subscription-Key": AUTOCAB_API_KEY,
    }
)

CACHE_DB = "xl_data/address_cache.db"


def load_cache():
    conn = sqlite3.connect(CACHE_DB)
    conn.execute(
        """
    CREATE TABLE IF NOT EXISTS addresses (
        query TEXT PRIMARY KEY,
        text TEXT,
        lat REAL,
        lng REAL,
        zone_id INTEGER,
        zone_name TEXT,
        town TEXT,
        last_used DATE
    )"""
    )
    cursor = conn.cursor()
    cursor.execute(
        "SELECT query, text, lat, lng, zone_id, zone_name, town, last_used FROM addresses"
    )
    rows = cursor.fetchall()
    conn.close()

    return {
        row[0]: {
            "text": row[1],
            "coordinate": {
                "latitude": row[2],
                "longitude": row[3],
            },
            "zone": {
                "id": row[4],
                "name": row[5],
            },
            "town": row[6],
            "last_used": row[7],
        }
        for row in rows
    }

# Load the address cache from the database
address_cache = load_cache()

# Load ghost names Excel and build map (once)
hospital_names_path = base_path / "xl_data" / "hospital names.xlsx"
try:
    ghost_name_df = pd.read_excel(hospital_names_path, engine="openpyxl")
    ghost_name_map = {
        str(row["pts name"]).strip().lower(): str(row["ghost name"]).strip()
        for _, row in ghost_name_df.iterrows()
        if pd.notna(row.get("pts name")) and pd.notna(row.get("ghost name"))
    }
except Exception as e:
    print(f"[WARN] Failed to load ghost names from {hospital_names_path}: {e}")
    ghost_name_map = {}


def save_cache():
    if not address_cache:
        return
    conn = sqlite3.connect(CACHE_DB)
    cursor = conn.cursor()

    for query, result in address_cache.items():
        coord = result.get("coordinate", {})
        zone = result.get("zone", {})
        cursor.execute(
            """
        INSERT OR IGNORE INTO addresses (query, text, lat, lng, zone_id, zone_name, town, last_used)
        VALUES (?, ?, ?, ?, ?, ?, ?, DATE('now'))
        """,
            (
                query,
                result.get("text", ""),
                coord.get("latitude"),
                coord.get("longitude"),
                zone.get("id"),
                zone.get("name"),
                result.get("town", ""),
            ),
        )
    conn.commit()
    conn.close()


def apply_ghost_name(name: str):
    """Return a canonical ghost name if `name` matches/contains any PTS name."""
    if not isinstance(name, str):
        return name
    name_clean = name.strip().lower()
    # exact match
    if name_clean in ghost_name_map:
        return ghost_name_map[name_clean]
    # substring match (longest keys first)
    for pts_name in sorted(ghost_name_map.keys(), key=len, reverse=True):
        if pts_name and pts_name in name_clean:
            return ghost_name_map[pts_name]
    return name


def _normalize_place_payload(p):
    """
    Normalize payloads (from either placeId lookup or fullAddress snapshot)
    into the same shape your code already uses downstream.
    """
    if not isinstance(p, dict):
        return None

    # Prefer provided text; fall back to a simple join
    text = p.get("text")
    if not text:
        parts = [p.get("house"), p.get("street"), p.get("town"), p.get("postCode")]
        text = ", ".join([str(x) for x in parts if x])

    coord = p.get("coordinate") or {}
    zone = p.get("zone") or {}

    return {
        "text": text or "",
        "coordinate": {
            "latitude": coord.get("latitude"),
            "longitude": coord.get("longitude"),
        },
        "zone": {
            "id": zone.get("id"),
            "name": zone.get("name"),
        },
        # Some payloads use town merged in text; keep a separate town field for your cache
        "town": p.get("town") or "",
    }


def fetch_address_by_place_id(place_id: str):
    """
    Calls the Autocab placeId endpoint and returns a normalized address dict,
    or None on failure.
    """
    if not place_id:
        return None

    try:
        r = session.get(url=PLACE_ID_URL , params={"text": place_id}, timeout=10)
    except Exception as e:
        print(f"[WARN] placeId lookup failed: {type(e).__name__}: {e}")
        return None

    if r.status_code != 200:
        print(f"[WARN] placeId lookup got {r.status_code}: {r.text[:200]}")
        return None

    payload = r.json()
    return _normalize_place_payload(payload)


def fetch_address(db_name, query, jrny_id):
    q_original = (query or "").strip()
    if not q_original:
        print("[WARN] Empty query received.")
        return None

    # apply ghost-name mapping before cache/API
    q_mapped = apply_ghost_name(q_original)

    # Prefer cache on either key
    if q_original in address_cache:
        # UPDATE last used in DB since this address was actually used
        with sqlite3.connect(CACHE_DB) as conn:
            conn.execute(
                "UPDATE addresses SET last_used = DATE('now') WHERE query = ?",
                (q_original,),
            )
            conn.commit()
        return address_cache[q_original]
    if q_mapped in address_cache:
        # also seed the original key to avoid future misses
        address_cache[q_original] = address_cache[q_mapped]
        with sqlite3.connect(CACHE_DB) as conn:
            conn.execute(
                "UPDATE addresses SET last_used = DATE('now') WHERE query IN (?, ?)",
                (q_original, q_mapped),
            )
            conn.commit()
        return address_cache[q_mapped]

    # Use the mapped query when calling the API
    q = q_mapped

    for attempt in range(2):
        try:
            response = session.get(BASE_URL, params={"text": q}, timeout=10)
        except Exception as e:
            print(f"[WARN] Attempt {attempt+1} raised {type(e).__name__}: {e}")
            time.sleep(min(5, 2**attempt))
            continue

        if response.status_code == 200:
            data = response.json()
            result = (
                data[0]
                if isinstance(data, list) and data
                else data if isinstance(data, dict) and "coordinate" in data else None
            )
            if result:
                # cache under BOTH the original and mapped keys
                address_cache[q] = result
                address_cache[q_original] = result
                return result
            # 200 but no usable payload → no point retrying the same query
            break

        if response.status_code in (400, 404):
            # try address lookup instead
            response = session.get(ADDRESS_LOOKUP_URL, params={"text": q}, timeout=10)
            if response.status_code == 200:
                data = response.json()
                # Expect either a list of candidates or a single object
                candidates = data if isinstance(data, list) else [data]

                # 1) Prefer a candidate with a non-null placeID
                place_item = next(
                    (c for c in candidates if c and c.get("placeID")), None
                )
                if place_item:
                    place_id = place_item["placeID"]
                    result = fetch_address_by_place_id(place_id)
                    if result:
                        # cache under BOTH the original and mapped keys
                        address_cache[q] = result
                        address_cache[q_original] = result
                        return result

                # 2) Fall back to any candidate that has a fullAddress snapshot
                fa_item = next(
                    (c for c in candidates if c and c.get("fullAddress")), None
                )
                if fa_item:
                    normalized = _normalize_place_payload(fa_item["fullAddress"])
                    if normalized:
                        address_cache[q] = normalized
                        address_cache[q_original] = normalized
                        return normalized

                # Debug to help future tuning
                print(
                    f"[INFO] lookupAddress returned no usable placeID/fullAddress: {data}"
                )
                # no further retry here; let outer loop continue/backoff

        if response.status_code == 429:
            retry_after = int(response.headers.get("retry-after", 5))
            print(f"[WARN] Rate limit hit (429). Retrying after {retry_after}s...")
            time.sleep(retry_after)
            continue

        # Treat transient server/network states as retryable
        if response.status_code in (500, 502, 503, 504):
            wait = 0.75 * (attempt + 1)  # 0.75s, 1.5s, 2.25s, 3.0s, ...
            print(
                f"[WARN] Attempt {attempt+1} got {response.status_code}. Retrying in {wait:.2f}s..."
            )

            time.sleep(wait)
            continue

        # Non-retryable status → stop trying
        print(
            f"[ERROR] Attempt {attempt+1} got {response.status_code}: {response.text}"
        )
        break

    print(f"[WARN] Could not resolve address: {q_original}")
    mark_jrny_ids_booked(db_name, [jrny_id], status="skipped")
    send_mail(q_original, jrny_id)

    return None


def extract_coordinates(address_json):
    try:
        coord = address_json.get("coordinate", {})
        lat = coord.get("latitude")
        lon = coord.get("longitude")
        if lat is not None and lon is not None:
            return f"{lat}, {lon}"
    except Exception:
        pass
    return ""


def normalize_address(addr):
    if not isinstance(addr, str):
        return ""
    # addr = addr.lower().strip()
    addr = re.sub(
        r"(flat|apt|apartment|room|suite)[^,]*,", "", addr, flags=re.IGNORECASE
    )
    addr = re.sub(r"\s+", " ", addr)
    addr = re.sub(r",\s*", ", ", addr)
    return addr


def parse_coord(s):
    if not isinstance(s, str) or "," not in s:
        return None
    a, b = [x.strip() for x in s.split(",", 1)]
    try:
        return (float(a), float(b))
    except ValueError:
        return None


def process_file(jobs_df, db_name):
    jobs_df = jobs_df.copy()

    # Step 2: Enrich from and to
    jobs_df["g_from"] = None
    jobs_df["g_to"] = None
    jobs_df["from_coord"] = None
    jobs_df["to_coord"] = None
    jobs_df["from_zone_obj"] = None
    jobs_df["to_zone_obj"] = None
    jobs_df["from_town"] = None
    jobs_df["to_town"] = None
    jobs_df["from_norm"] = jobs_df["from_address"].apply(normalize_address)
    jobs_df["to_norm"] = jobs_df["to_address"].apply(normalize_address)

    for idx, row in jobs_df.iterrows():
        from_place = fetch_address(db_name, row["from_norm"], row.get("jrny_id"))
        to_place = fetch_address(db_name, row["to_norm"], row.get("jrny_id"))
        if from_place:
            jobs_df.at[idx, "g_from"] = from_place.get("text", "")
            jobs_df.at[idx, "from_coord"] = extract_coordinates(from_place)
            jobs_df.at[idx, "from_zone_obj"] = json.dumps(
                from_place.get("zone", {})
            )
            jobs_df.at[idx, "from_town"] = from_place.get("town", "")

        if to_place:
            jobs_df.at[idx, "g_to"] = to_place.get("text", "")
            jobs_df.at[idx, "to_coord"] = extract_coordinates(to_place)
            jobs_df.at[idx, "to_zone_obj"] = json.dumps(
                to_place.get("zone", {})
            )
            jobs_df.at[idx, "to_town"] = to_place.get("town", "")

    jobs_df["from_coord_parsed"] = jobs_df["from_coord"].apply(parse_coord)
    jobs_df["to_coord_parsed"] = jobs_df["to_coord"].apply(parse_coord)
       
    # Route to JSON generator
    generate_json_from_df(jobs_df, db_name)


def load_jobs_to_process(db_name):
    with session_scope() as session:
        status_norm = func.lower(func.trim(func.coalesce(db_name.status, "")))
        columns = [c for c in db_name.__table__.columns]
        stmt = select(*columns).where(status_norm.notin_(["booked", "error", "skipped"]))
        results = session.execute(stmt).mappings().all()
        return pd.DataFrame(results)
    

def main():
    jobs_df = load_jobs_to_process(NwasLogsheet)
    if jobs_df.empty:
        print("No unbooked jobs found in the database.")
        return
    process_file(jobs_df, db_name=NwasLogsheet)  # Pass the DataFrame to be processed and enriched

    jobs_to_rebook_df = load_jobs_to_process(RebookJobs)
    if jobs_to_rebook_df.empty:
        print("No jobs to rebook found in the database.")
        return     
    process_file(jobs_to_rebook_df, db_name=RebookJobs)  # Process rebook jobs as well

    save_cache()

if __name__ == "__main__":
    main()
