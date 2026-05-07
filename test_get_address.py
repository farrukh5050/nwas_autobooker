import os
import requests
from urllib.parse import quote
import pandas as pd
from get_address_from_ghost import process_file
from database.models import NwasLogsheet

AUTOCAB_API_KEY = str(os.getenv("AUTOCAB_API_KEY"))
COMPANY_ID = "3162"  # Replace with your actual company ID
BASE_URL = "https://autocab-api.azure-api.net/booking/v1/addressFromText?text="
NEW_BOOKING_URL = "https://autocab-api.azure-api.net/booking/v1/booking"

session = requests.Session()
session.headers.update(
    {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "Ocp-Apim-Subscription-Key": AUTOCAB_API_KEY,
    }
)

df = pd.DataFrame(columns=[
    "id",
    "run",
    "jrny_id",
    "name",
    "from_address",
    "to_address",
    "esc",
    "mob",
    "notes",
    "phone_number",
    "formatted_time",
    "cost_center",
    "status"
])

data = [{
    "id": 1,
    "run": "Run 1",
    "jrny_id": 12345,
    "name": "Mr Nasir Butt",
    "from_address": "M13 0WN, Birch House Nursing Home 98-100, Birch Lane, Manchester",
    "to_address": "Rochdale Infirmary, MRI Scan, OL12 0NB",
    "esc": "",
    "mob": "C1",
    "notes": "does not speak much english",
    "phone_number": "07383 558123",
    "formatted_time": "2026-04-23T08:30:00+01:00",
    "cost_center": "STCDAM",
    "status": ""
}]

df = pd.DataFrame(data=data)

process_file(jobs_df=df, db_name=NwasLogsheet)