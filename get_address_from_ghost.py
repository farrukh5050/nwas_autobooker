import os
import requests
import json
import pandas as pd
import sqlite3
from pathlib import Path
import sys
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
BASE_URL = "https://autocab-api.azure-api.net/booking/v1/addressFromText"
ADDRESS_LOOKUP_URL = "https://autocab-api.azure-api.net/booking/v1/lookupAddress"
PLACE_ID_URL = "https://autocab-api.azure-api.net/booking/v1/address"

session = requests.Session()
session.headers.update(
    {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "Ocp-Apim-Subscription-Key": AUTOCAB_API_KEY,
    }
)

CACHE_DB = "xl_data/address_cache.db"

# Persistent connection in autocommit mode — writes hit disk immediately.
cache_conn = sqlite3.connect(CACHE_DB, isolation_level=None)
cache_conn.execute(
    """
    CREATE TABLE IF NOT EXISTS addresses (
        query TEXT PRIMARY KEY,
        text TEXT,
        lat REAL,
        lng REAL,
        zone_id INTEGER,
        zone_name TEXT,
        postCode TEXT,
        town TEXT,
        last_used DATE
    )
    """
)

# In-memory cache for the lifetime of this Python process.
# Under run_all.py this persists across scheduler cycles.
run_memo: dict[str, dict] = {}

def row_to_result(row):
    return {
        "text": row[0],
        "coordinate": {"latitude": row[1], "longitude": row[2]},
        "zone": {"id": row[3], "name": row[4]},
        "postCode": row[5],
        "town": row[6],
    }


def db_lookup(keys):
    """Return (result, set_of_keys_present_in_db) for the given candidate keys."""
    placeholders = ",".join("?" * len(keys))
    rows = cache_conn.execute(
        f"SELECT query, text, lat, lng, zone_id, zone_name, postCode, town "
        f"FROM addresses WHERE query IN ({placeholders})",
        keys,
    ).fetchall()
    if not rows:
        return None, set()
    return row_to_result(rows[0][1:]), {r[0] for r in rows}


def db_touch(keys):
    if not keys:
        return
    placeholders = ",".join("?" * len(keys))
    cache_conn.execute(
        f"UPDATE addresses SET last_used = DATE('now') WHERE query IN ({placeholders})",
        keys,
    )


def db_insert(query, result):
    coord = result.get("coordinate") or {}
    zone = result.get("zone") or {}
    cache_conn.execute(
        """
        INSERT OR REPLACE INTO addresses
        (query, text, lat, lng, zone_id, zone_name, postCode, town, last_used)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, DATE('now'))
        """,
        (
            query,
            result.get("text", ""),
            coord.get("latitude"),
            coord.get("longitude"),
            zone.get("id"),
            zone.get("name"),
            result.get("postCode", ""),
            result.get("town", ""),
        ),
    )


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


def apply_ghost_name(name: str):
    """Return a canonical ghost name if `name` matches/contains any PTS name."""
    if not isinstance(name, str):
        return name
    name_clean = name.strip().lower()
    if name_clean in ghost_name_map:
        return ghost_name_map[name_clean]
    for pts_name in sorted(ghost_name_map.keys(), key=len, reverse=True):
        if pts_name and pts_name in name_clean:
            return ghost_name_map[pts_name]
    return name


def normalize_place_payload(p):
    """
    Normalize payloads (from either placeId lookup or fullAddress snapshot)
    into the same shape your code already uses downstream.
    """
    if not isinstance(p, dict):
        return None

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
        "town": p.get("town") or "",
        "postCode": p.get("postCode") or "",
    }


def fetch_address_by_place_id(place_id: str):
    if not place_id:
        return None

    try:
        r = session.get(PLACE_ID_URL, params={"placeId": place_id}, timeout=10)
    except Exception as e:
        print(f"[WARN] placeId lookup failed: {type(e).__name__}: {e}")
        return None

    if r.status_code != 200:
        print(f"[WARN] placeId lookup got {r.status_code}: {r.text[:200]}")
        return None

    return normalize_place_payload(r.json())


def store_address_key(keys, result):
    for k in keys:
        db_insert(k, result)
        run_memo[k] = result


def fetch_address(db_name, query, jrny_id):
    q_original = (query or "").strip()
    if not q_original:
        return None

    q = apply_ghost_name(q_original)
    keys = [q_original] if q == q_original else [q_original, q]

    # within-run memo
    for k in keys:
        if k in run_memo:
            return run_memo[k]

    # SQLite cache
    cached, present = db_lookup(keys)
    if cached:
        db_touch(list(present))
        # backfill any missing alias so future runs hit on either key
        for k in keys:
            if k not in present:
                db_insert(k, cached)
            run_memo[k] = cached
        return cached

    def resolve(data):
        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if isinstance(item, dict) and item.get("coordinate"):
                return normalize_place_payload(item)
        for item in candidates:
            if isinstance(item, dict) and item.get("placeID"):
                result = fetch_address_by_place_id(item["placeID"])
                if result:
                    return result
        for item in candidates:
            if isinstance(item, dict) and item.get("fullAddress"):
                return normalize_place_payload(item["fullAddress"])
        return None

    # try addressFromText
    try:
        response = session.get(BASE_URL, params={"text": q}, timeout=10)
        if response.status_code == 200:
            result = resolve(response.json())
            if result:
                store_address_key(keys, result)
                return result
    except Exception as e:
        print(f"[WARN] addressFromText failed: {e}")

    # fallback to lookupAddress
    try:
        response = session.get(ADDRESS_LOOKUP_URL, params={"text": q}, timeout=10)
        if response.status_code == 200:
            result = resolve(response.json())
            if result:
                store_address_key(keys, result)
                return result
    except Exception as e:
        print(f"[WARN] lookupAddress failed: {e}")

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
    jobs_df["from_post_code"] = None
    jobs_df["to_post_code"] = None
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
            jobs_df.at[idx, "from_post_code"] = from_place.get("postCode", "")

        if to_place:
            jobs_df.at[idx, "g_to"] = to_place.get("text", "")
            jobs_df.at[idx, "to_coord"] = extract_coordinates(to_place)
            jobs_df.at[idx, "to_zone_obj"] = json.dumps(
                to_place.get("zone", {})
            )
            jobs_df.at[idx, "to_town"] = to_place.get("town", "")
            jobs_df.at[idx, "to_post_code"] = to_place.get("postCode", "")


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

if __name__ == "__main__":
    main()
