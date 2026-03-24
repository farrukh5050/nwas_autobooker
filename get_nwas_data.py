import os
import re
import time
import pytz
from selenium import webdriver
from selenium.webdriver.common.by import By
from datetime import date, datetime
from io import StringIO
import pandas as pd
from openpyxl import Workbook
from dotenv import load_dotenv
from pathlib import Path
from selenium.common.exceptions import NoSuchElementException
import sys
from selenium.webdriver.chrome.options import Options
from database.db_conn import save_to_db, save_updates_to_db, save_rebooks_to_db
from database.database import init_db, init_sqlite, session_scope

# Constants
JRNY_ID_COLUMN = "jrny id"
PHONE_COLUMN = "phone no"
UNWANTED_TEXT = ">>>>>"
RUN_COLUMN = "run"  # Ensure this matches the column containing cost centers
COLS_TO_DROP = ["age", "cat", "description", "time", "cost_center"]
UPDATE_COLS_TO_DROP = ["time", "cost_center"]
FILENAME = "xl_data/nwas_logsheet.xlsx"
UPDATE_FILENAME = "xl_data/nwas_logsheet_update.xlsx"
REBOOK_JOBS_FILENAME = "xl_data/rebook_jobs.xlsx"
uk_tz = pytz.timezone("Europe/London")

# Always prefer a .env file NEXT TO the exe (or script when not frozen)
if getattr(sys, "frozen", False):
    app_dir = Path(sys.executable).parent  # folder containing the .exe
else:
    app_dir = Path(__file__).parent  # folder containing the .py

# Try these locations in order
candidate_env_files = [
    app_dir / ".env",  # <— folder containing the .exe
    Path.cwd() / ".env",  # if launched from elsewhere
]

loaded = False
for p in candidate_env_files:
    if p.exists():
        load_dotenv(p)  # load this file
        loaded = True
        print(f"Loaded environment from: {p}")
        break

if not loaded:
    # Fallback: respect real OS env vars if user set them in Windows
    load_dotenv()  # no path — doesn’t override OS env
    print("No .env file found next to the app; relying on OS environment variables.")

username = os.getenv("NWAS_USERNAME")
password = os.getenv("NWAS_PASSWORD")

if not username or not password:
    print(
        "NWAS_USERNAME or NWAS_PASSWORD not set. Put them in a .env next to the .exe."
    )
    sys.exit(1)


def open_chrome_and_login():
    from selenium.webdriver.chrome.service import Service

    os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"

    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--disable-logging")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-logging"])

    service = Service(log_path=os.devnull)  # suppress logs
    driver = webdriver.Chrome(service=service, options=chrome_options)

    driver.get("https://ptsed.nwas.nhs.uk/")
    driver.find_element(By.ID, "txtUsername").send_keys(str(username))
    driver.find_element(By.ID, "txtPassword").send_keys(str(password))
    driver.find_element(By.ID, "cmdSubmit").click()
    time.sleep(3)

    try:
        driver.get("https://ptsed.nwas.nhs.uk/frmLogsheets.aspx")
        # Try to interact with an element that should only exist if login worked
        date_input = driver.find_element(By.ID, "txtPlanDate")
    except NoSuchElementException:
        driver.quit()
        raise SystemExit(
            "WRONG PASSWORD or expired login — please update your .env file."
        )

    # continue normal flow if successful
    date_input.clear()
    date_input.send_keys(date.today().strftime("%d%m%Y"))
    driver.find_element(By.CSS_SELECTOR, "label[for='chkIncAbort']").click()
    driver.find_element(By.CSS_SELECTOR, "label[for='chkIncCancel']").click()
    driver.find_element(By.ID, "cmdSubmit").click()
    time.sleep(3)

    return driver


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


def get_today_date():
    return datetime.now(uk_tz).strftime("%Y-%m-%d")


def close_driver(driver):
    try:
        driver.quit()
        print("WebDriver closed.")
    except Exception:
        print("WebDriver closing encountered an issue.")


def check_today_date(existing_data, filename):
    """Delete the Excel file if any date is not today's date."""
    if existing_data:
        first_sheet = list(existing_data.keys())[0]
        first_sheet_df = existing_data[first_sheet]

        if not first_sheet_df.empty and "formatted_time" in first_sheet_df.columns:
            try:
                formatted_dates = pd.to_datetime(
                    first_sheet_df["formatted_time"], errors="coerce"
                ).dt.date.dropna()

                today = date.today()

                if not all(d == today for d in formatted_dates):

                    if os.path.exists(filename):
                        os.remove(filename)
                    wb = Workbook()
                    wb.save(filename)
                    print("Excel file successfully reset.")
                    return True  # File was reset

            except Exception as e:
                print(f"Error checking dates in formatted_time: {e}")
                return False

    return False  # No reset needed


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


def clean_table(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = df.columns.str.strip().str.lower()
    try:
        df = df[
            ~df["jrny id"]
            .fillna("")
            .astype(str)
            .str.contains(r"(?i)^\s*[^a-z0-9]*notes?[^a-z0-9]*$|^\s*last\s*updated\b")
        ]
        # Remove only "Ack" from the RUN_COLUMN
        df.loc[:, RUN_COLUMN] = df[RUN_COLUMN].replace(
            r"(?i)^ack$", "", regex=True
        )  # remove "Ack"
        df.loc[:, RUN_COLUMN] = df[RUN_COLUMN].replace(
            r"^\s*$", pd.NA, regex=True
        )  # convert empty to NaN

        df.loc[:, PHONE_COLUMN] = (
            df[JRNY_ID_COLUMN].apply(extract_phone_numbers).shift(-1)
        )

        df.loc[:, RUN_COLUMN] = df[RUN_COLUMN].ffill(axis=0)
        df = df.dropna(subset=[JRNY_ID_COLUMN], how="all").reset_index(drop=True)

        df["to"] = (
            df["to"]
            .str.replace(r",\s*,", ", ", regex=True)
            .str.replace(r"\s+", " ", regex=True)
            .str.strip()
        )
        df["from"] = (
            df["from"]
            .str.replace(r",\s*,", ", ", regex=True)
            .str.replace(r"\s+", " ", regex=True)
            .str.strip()
        )

        df["cost_center"] = (
            df[JRNY_ID_COLUMN]
            .str.extract(r"((?:STC|SPH)[A-Z0-9]+)", expand=False)
            .ffill()
            .infer_objects(copy=False)
            .astype("string")
        )
        
        # for cost_center, group in df.groupby("cost_center"):
        #     group = group.drop(columns=COLS_TO_DROP, errors="ignore")

        # df = df[~df.apply(lambda row: row.astype(str).str.contains(UNWANTED_TEXT, case=False).any(), axis=1)]

        return df

    except Exception as e:
        print("Failed to extract table:")
        raise e


def main():
    init_sqlite()
    init_db()
    driver = open_chrome_and_login()

    try:
        html_content = driver.find_element(By.ID, "divLogsheetHTML").get_attribute(
            "outerHTML"
        )
        tables = pd.read_html(StringIO(str(html_content)), flavor="lxml")
        df: pd.DataFrame = tables[0].dropna(how="all")
        clean_data: pd.DataFrame = clean_table(df)
        count = save_to_db(clean_data)
        print(f"Successfully inserted {count} records into the database.")
        update_count = save_updates_to_db(clean_data)
        print(f"Successfully updated status for {update_count} records in the database.")
        rebook_count = save_rebooks_to_db(clean_data)
        print(f"Successfully processed {rebook_count} rebookable journeys in the database.")
    except ValueError:
        print("No tables found.")
    except Exception as e:
        print(f"Unexpected error: {e}")
    finally:
        close_driver(driver)


if __name__ == "__main__":
    main()
