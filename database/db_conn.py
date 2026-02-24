import pandas as pd
import re
from datetime import datetime
import pytz
from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from database.database import session_scope
from database.models import NwasLogsheet, UpdateLogsheet, RebookJobs
from typing import cast

JRNY_ID_COLUMN = "jrny id"
UNWANTED_TEXT = ">>>>>"
RUN_COLUMN = "run"  
PHONE_COLUMN = "phone no"
uk_tz = pytz.timezone("Europe/London")

def extract_departure_time(t):
    try:
        # Convert to string and strip whitespace
        t = str(t).strip()

        # Extract the first valid HH:MM pattern (e.g., from '10:30 08:40 R')
        match = re.search(r"\b\d{1,2}:\d{2}\b", t)
        if match:
            time_str = match.group()
            dt = datetime.strptime(f"{get_today_date()} {time_str}", "%Y-%m-%d %H:%M")
            dt = pytz.timezone("Europe/London").localize(dt)
            return dt.isoformat()
    except Exception as e:
        print(f"Failed to parse time '{t}': {e}")
    return None


def extract_phone_numbers(value):
    text = str(value)

    # Match UK-style numbers with optional spaces
    numbers = re.findall(r"0\d(?:\s?\d){9,10}", text)

    if not numbers:
        return ""

    numbers = [num.replace(" ", "") for num in numbers]

    # Prefer mobile numbers (start with 07)
    mobiles = [num for num in numbers if num.startswith("07")]
    if mobiles:
        return mobiles[0]  # return the first mobile

    # Otherwise return the first valid number
    return numbers[0]


def format_time_string(raw_time):
    if not isinstance(raw_time, str):
        return None
    times = re.findall(r"\b\d{1,2}:\d{2}\b", raw_time)
    if not times:
        return None
    try:
        hour, minute = map(int, times[-1].split(":"))  # grab the last time
        current_date = datetime.now(uk_tz).date()  # <-- always “today”
        dt = datetime.combine(current_date, datetime.min.time()).replace(
            hour=hour, minute=minute
        )
        dt = uk_tz.localize(dt)
        return dt.isoformat()
    except Exception as e:
        print(f"Time parse error for '{raw_time}': {e}")
        return None


def get_today_date():
    return datetime.now(uk_tz).strftime("%Y-%m-%d")


def save_to_db(df: pd.DataFrame,) -> int:
    """
    Save logsheet rows to DB grouped by cost_center.
    - Dedupes per cost_center on (jrny_id, formatted_time)
    """
    if df is None or df.empty:
        return 0

    df = df.copy()
    df.columns = df.columns.str.strip().str.lower()
    df = df[~df.apply(lambda row: row.astype(str).str.contains(UNWANTED_TEXT, case=False).any(), axis=1)]

    # Ensure formatted_time
    if "formatted_time" not in df.columns and "time" in df.columns:
        df["formatted_time"] = df["time"].apply(extract_departure_time)

    # Normalise keys
    df[JRNY_ID_COLUMN] = df[JRNY_ID_COLUMN].astype(str).str.strip()
    df["formatted_time"] = df["formatted_time"].astype(str).str.strip()
    df["cost_center"] = df["cost_center"].astype(str).str.strip()

    inserted = 0

    with session_scope() as db:
        # Group cost centers and insert rows
        for cost_center, group in df.groupby("cost_center"):
            group = group.iloc[1:].reset_index(drop=True)

            # Ensure status exists
            if "status" not in group.columns:
                group["status"] = ""

            # Formatted_time cleanup
            group["formatted_time"] = group["formatted_time"].str.replace(
                r"\s*R:00\s*", "", regex=True
            )
            group["formatted_time"] = group["formatted_time"].astype(str).str.extract(r"^(\S+)")

            # Insert rows for this cost_center
            for _, row in group.iterrows():
                jrny = str(row.get(JRNY_ID_COLUMN, "")).strip()
                ftime = str(row.get("formatted_time", "")).strip()

                jrny_id = int(jrny)

                # Deduplicate like Excel: (jrny_id, formatted_time) per cost_center
                exists = db.execute(
                    select(NwasLogsheet.id).where(
                        NwasLogsheet.cost_center == str(cost_center),
                        NwasLogsheet.jrny_id == jrny_id,
                        NwasLogsheet.formatted_time == ftime,
                    )
                ).first()

                if exists:
                    continue

                entry = NwasLogsheet(
                    run=str(row.get("run", "") or ""),
                    jrny_id=jrny_id,
                    name=str(row.get("name", "") or ""),
                    from_address=str(row.get("from", "") or ""),
                    to_address=str(row.get("to", "") or ""),
                    esc=str(row.get("esc", "") or ""),
                    notes=str(row.get("notes", "") or ""),
                    phone_number=str(row.get("phone no", "") or ""),
                    formatted_time=ftime,
                    cost_center=str(cost_center),
                    status=str(row.get("status", "") or ""),
                )
                db.add(entry)
                inserted += 1
        return inserted
    

def save_updates_to_db(df: pd.DataFrame) -> int:
    """
    Extract update rows (CANCELLED/ABORTED + R-times) and store them in UpdateLogsheet.
    Dedupes per cost_center on (jrny_id, formatted_time).
    Returns number of rows inserted.
    """
    if df is None or df.empty:
        return 0

    df = df.copy()
    df.columns = df.columns.str.strip().str.lower()

    # Fill down run column
    if "run" in df.columns:
        df["run"] = df["run"].where(df["run"].astype(str).str.contains("Run", na=False)).ffill()

    # Build matched_rows (same logic as save_update_excel)
    matched_rows = []

    inserted = 0

    # Step 1: CANCELLED / ABORTED detected in the *next row* after a jrny id row
    for i in range(len(df) - 1):
        current_row = df.iloc[i]
        next_row = df.iloc[i + 1]

        jrny_id = str(current_row.get(JRNY_ID_COLUMN, "")).strip()
        next_jrny_id = str(next_row.get(JRNY_ID_COLUMN, "")).lower()

        if jrny_id.isdigit():
            if "cancelled" in next_jrny_id:
                row_type = "CANCELLED"
            elif "aborted" in next_jrny_id:
                row_type = "ABORTED"
            else:
                continue

            full_row = current_row.copy()
            full_row["type"] = row_type
            full_row["formatted_time"] = format_time_string(current_row.get("time", ""))
            matched_rows.append(full_row)
            inserted += 1

    # Step 2: R-times
    for _, row in df.iterrows():
        jrny_id = str(row.get(JRNY_ID_COLUMN, "")).strip()
        time_val = str(row.get("time", "")).lower()

        if jrny_id.isdigit() and "r" in time_val:
            full_row = row.copy()
            full_row["type"] = "R"
            full_row["formatted_time"] = format_time_string(row.get("time", ""))
            matched_rows.append(full_row)

    updates_df = pd.DataFrame(matched_rows)
    if updates_df.empty or "cost_center" not in updates_df.columns:
        return 0

    # Normalize keys
    updates_df[JRNY_ID_COLUMN] = updates_df[JRNY_ID_COLUMN].astype(str).str.strip()
    updates_df["formatted_time"] = updates_df["formatted_time"].astype(str).str.strip()
    updates_df["cost_center"] = updates_df["cost_center"].astype(str).str.strip()


    with session_scope() as db:
        for cost_center, group in updates_df.groupby("cost_center"):
            # formatted_time cleanup (match your other save funcs)
            group["formatted_time"] = group["formatted_time"].str.replace(
                r"\s*R:00\s*", "", regex=True
            )
            group["formatted_time"] = group["formatted_time"].astype(str).str.extract(r"^(\S+)")

            for _, row in group.iterrows():
                jrny = str(row.get(JRNY_ID_COLUMN, "")).strip()
                ftime = str(row.get("formatted_time", "")).strip()
                if not jrny.isdigit() or not ftime:
                    continue

                jrny_id = int(jrny)

                # dedupe
                exists = db.execute(
                    select(UpdateLogsheet.id).where(
                        UpdateLogsheet.cost_center == str(cost_center),
                        UpdateLogsheet.jrny_id == jrny_id,
                        UpdateLogsheet.formatted_time == ftime,
                    )
                ).first()

                if exists:
                    continue

                entry = UpdateLogsheet(
                    run=str(row.get("run", "") or ""),
                    jrny_id=jrny_id,
                    name=str(row.get("name", "") or ""),
                    from_address=str(row.get("from", "") or ""),
                    to_address=str(row.get("to", "") or ""),
                    esc=str(row.get("esc", "") or ""),
                    notes=str(row.get("notes", "") or ""),
                    phone_number=str(row.get("phone no", "") or ""),
                    formatted_time=ftime,
                    cost_center=str(cost_center),
                    type=str(row.get("type", "") or ""),
                    status=str(row.get("status", "") or ""),
                )
                db.add(entry)
                inserted += 1

        return inserted


def save_rebooks_to_db(df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0

    df = df.copy()
    df.columns = df.columns.str.strip().str.lower()

    # Clean + fill Run
    df.loc[:, RUN_COLUMN] = df[RUN_COLUMN].replace(r"(?i)^ack$", "", regex=True)
    df.loc[:, RUN_COLUMN] = df[RUN_COLUMN].replace(r"^\s*$", pd.NA, regex=True)
    df.loc[:, RUN_COLUMN] = df[RUN_COLUMN].where(
        df[RUN_COLUMN].astype(str).str.contains("Run", na=False)
    ).ffill()

    # Normalise IDs
    df[JRNY_ID_COLUMN] = df[JRNY_ID_COLUMN].astype(str).str.strip()

    # Ensure cost_center (you already do this elsewhere)
    if "cost_center" not in df.columns:
        df["cost_center"] = (
            df[JRNY_ID_COLUMN]
            .str.extract(r"((?:STC|SPH)[A-Z0-9]+)", expand=False)
            .ffill()
            .infer_objects(copy=False)
            .astype("string")
        )

    inserted = 0

    with session_scope() as db:
        # IMPORTANT: group by cost_center + run (so Run 1 and Run 3 are separate)
        for (cost_center, run_name), group in df.groupby(["cost_center", RUN_COLUMN]):
            group = group.reset_index(drop=True)

            # Identify journey rows (8 digit id)
            is_journey = group[JRNY_ID_COLUMN].str.match(r"^\d{8}$", na=False)

            # For each journey row, look at next row for cancelled/aborted
            next_text = group.shift(-1).astype(str).agg(" ".join, axis=1)
            journey_is_cancelled = is_journey & next_text.str.contains(r"\b(?:cancelled|aborted)\b", case=False, na=False, regex=True)

            # If NO cancellations in this run, skip
            if not journey_is_cancelled.any():
                continue

            # We want journeys that are NOT cancelled (but only within runs that contain cancellations)
            keep = group[is_journey & ~journey_is_cancelled].copy()
            if keep.empty:
                continue

            # Phone comes from next row (same trick as Excel)
            keep[PHONE_COLUMN] = next_text.loc[keep.index].apply(extract_phone_numbers)

            # formatted_time from time
            if "time" not in keep.columns:
                # if time column ever missing, just skip these (or raise if you prefer)
                continue
            keep["formatted_time"] = keep["time"].apply(format_time_string)

            # Insert each kept journey
            for _, row in keep.iterrows():
                jrny = str(row.get(JRNY_ID_COLUMN, "")).strip()
                ftime = str(row.get("formatted_time", "")).strip()

                if not jrny.isdigit() or not ftime:
                    continue

                jrny_id = int(jrny)

                exists = db.execute(
                    select(RebookJobs.id).where(
                        RebookJobs.cost_center == str(cost_center),
                        RebookJobs.jrny_id == jrny_id,
                        RebookJobs.formatted_time == ftime,
                    )
                ).first()
                if exists:
                    continue

                db.add(
                    RebookJobs(
                        run=str(run_name or ""),
                        jrny_id=jrny_id,
                        name=str(row.get("name", "") or ""),
                        from_address=str(row.get("from", "") or ""),
                        to_address=str(row.get("to", "") or ""),
                        esc=str(row.get("esc", "") or ""),
                        notes=str(row.get("notes", "") or ""),
                        phone_number=str(row.get("phone no", "") or ""),
                        formatted_time=ftime,
                        cost_center=str(cost_center),
                        status=str(row.get("status", "") or ""),
                    )
                )
                inserted += 1

    return inserted


def mark_jrny_ids_booked(db_name, jrny_ids: list[int | str], status: str) -> int:
    """Mark the given jrny_ids as booked in the database."""
    if not jrny_ids:
        return 0
    
    cleaned = []
    for x in jrny_ids:
        try:
            cleaned.append(int(str(x).strip()))
        except Exception:
            pass

    if not cleaned:
        return 0
    
    with session_scope() as session:
        stmt = (
            update(db_name)
            .where(db_name.jrny_id.in_(cleaned))
            .values(status=status)
        )
        
        res = cast(CursorResult, session.execute(stmt))
        return res.rowcount