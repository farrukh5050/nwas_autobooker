import os
import re
import time
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from datetime import date, datetime
from io import StringIO
import pandas as pd
from openpyxl import Workbook
from dotenv import load_dotenv
from pathlib import Path
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    StaleElementReferenceException,
    TimeoutException,
)
import sys
import base64
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from database.db_conn import save_to_db, save_updates_to_db, save_rebooks_to_db
from database.database import init_db, init_sqlite
from selenium.webdriver.chrome.service import Service

# Constants
PAGE_TIMEOUT = 20  # seconds to wait for an element to appear/become clickable
CLICK_ATTEMPTS = 3
JRNY_ID_COLUMN = "jrny id"
PHONE_COLUMN = "phone no"
RUN_COLUMN = "run"  # Ensure this matches the column containing cost centers
COLS_TO_DROP = ["age", "cat", "description", "time", "cost_center"]
UPDATE_COLS_TO_DROP = ["time", "cost_center"]

# Always prefer a .env file NEXT TO the exe (or script when not frozen)
if getattr(sys, "frozen", False):
    app_dir = Path(sys.executable).parent  # folder containing the .exe
else:
    app_dir = Path(__file__).parent  # folder containing the .py

# Try these locations in order
candidate_env_files = [
    app_dir / "json_data" / "bookings_log" / ".env",  # <— folder containing the .exe
    Path.cwd() / "json_data" / "bookings_log" / ".env",  # if launched from elsewhere
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
password = base64.b64decode(str(os.getenv("HERE_PASSWORD"))).decode("utf-8")

if not username or not password:
    print(
        "NWAS_USERNAME or NWAS_PASSWORD not set. Put them in a .env next to the .exe."
    )
    sys.exit(1)


def wait_for_page_ready(driver, timeout=PAGE_TIMEOUT):
    """Wait until the document has finished loading (ASP.NET postbacks included)."""
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
    except TimeoutException:
        pass  # carry on; the per-element waits below will report a real problem


def find_visible(driver, by, value, timeout=PAGE_TIMEOUT):
    """Wait for an element to exist and be visible, then return it."""
    return WebDriverWait(driver, timeout).until(
        EC.visibility_of_element_located((by, value))
    )


def safe_click(driver, by, value, timeout=PAGE_TIMEOUT):
    """
    Click an element, tolerating anything floating over it (calendar popups, postbacks).

    Retries a normal click while something is covering the element, then falls back
    to a JS click (which ignores hit-testing) so a stray overlay can't kill the run.
    """
    wait_for_page_ready(driver, timeout)
    last_error = None

    for _ in range(CLICK_ATTEMPTS):
        try:
            element = WebDriverWait(driver, timeout).until(
                EC.element_to_be_clickable((by, value))
            )
            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});", element
            )
            element.click()
            return element
        except (
            ElementClickInterceptedException,
            ElementNotInteractableException,
            StaleElementReferenceException,
        ) as e:
            last_error = e
            time.sleep(1)  # let the overlay/postback settle before retrying

    # Last resort: dispatch the click directly on the node.
    try:
        element = find_visible(driver, by, value, timeout)
        driver.execute_script("arguments[0].click();", element)
        print(f"Clicked {value} via JS fallback ({type(last_error).__name__}).")
        return element
    except Exception as fallback_error:
        raise (last_error or fallback_error)


def close_datepicker(driver, timeout=5):
    """
    Dismiss the jQuery UI calendar that opens on txtPlanDate.

    While open it floats over the Options checkboxes and intercepts their clicks
    (how tall it is — and therefore what it covers — varies by month).
    """
    try:
        driver.find_element(By.ID, "txtPlanDate").send_keys(Keys.ESCAPE)
    except Exception:
        pass
    try:
        WebDriverWait(driver, timeout).until(
            EC.invisibility_of_element_located((By.ID, "ui-datepicker-div"))
        )
    except TimeoutException:
        print("Datepicker did not close; relying on click fallbacks.")


def set_checkbox(driver, checkbox_id, checked=True):
    """
    Force a checkbox into the desired state.

    The inputs are display:none and styled via their <label>, so the state has to
    be read with JS and changed by clicking the label. Clicking blindly toggles —
    and the site remembers the last submitted state — so check before clicking.
    """
    current = driver.execute_script(
        "var el = document.getElementById(arguments[0]); return el ? el.checked : null;",
        checkbox_id,
    )
    if current is None:
        print(f"Checkbox {checkbox_id} not found; skipping.")
        return
    if current == checked:
        return

    safe_click(driver, By.CSS_SELECTOR, f"label[for='{checkbox_id}']")

    new_state = driver.execute_script(
        "var el = document.getElementById(arguments[0]); return el ? el.checked : null;",
        checkbox_id,
    )
    if new_state != checked:
        print(f"Warning: {checkbox_id} is {new_state}, expected {checked}.")


def open_chrome_and_login():

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

    try:
        driver.get("https://ptsed.nwas.nhs.uk/")
        find_visible(driver, By.ID, "txtUsername").send_keys(str(username))
        find_visible(driver, By.ID, "txtPassword").send_keys(str(password))
        safe_click(driver, By.ID, "cmdSubmit")

        # The login POST must finish before we navigate away, or the session
        # cookie is never set and frmLogsheets bounces us back to the login page.
        try:
            WebDriverWait(driver, PAGE_TIMEOUT).until(
                EC.invisibility_of_element_located((By.ID, "txtUsername"))
            )
        except TimeoutException:
            print("Login page did not clear after submit; continuing to check login.")

        driver.get("https://ptsed.nwas.nhs.uk/frmLogsheets.aspx")

        try:
            # An element that should only exist if login worked
            date_input = find_visible(driver, By.ID, "txtPlanDate")
        except TimeoutException:
            raise SystemExit(
                "WRONG PASSWORD or expired login — please update your .env file."
            )

        # continue normal flow if successful
        date_input.clear()
        date_input.send_keys(date.today().strftime("%d%m%Y"))
        close_datepicker(driver)

        # Aborted/cancelled journeys are what drive the update + rebook steps,
        # so make sure both are included rather than toggling whatever was set.
        set_checkbox(driver, "chkIncAbort", True)
        set_checkbox(driver, "chkIncCancel", True)

        safe_click(driver, By.ID, "cmdSubmit")

        # Wait for the results to be rendered instead of a blind sleep.
        # divLogsheetHTML is always in the DOM but never "visible", so wait on content.
        try:
            WebDriverWait(driver, PAGE_TIMEOUT).until(
                lambda d: d.find_elements(By.CSS_SELECTOR, "#divLogsheetHTML table")
            )
        except TimeoutException:
            print("Logsheet table did not appear in time; continuing anyway.")

        return driver
    except BaseException:
        # Never leak the headless Chrome process if login/setup fails
        close_driver(driver)
        raise


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
    return datetime.now().strftime("%Y-%m-%d")


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
        current_date = datetime.now().date()  # <-- always “today”
        dt = datetime.combine(current_date, datetime.min.time()).replace(
            hour=hour, minute=minute
        )
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
            .astype("string")
        )

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
