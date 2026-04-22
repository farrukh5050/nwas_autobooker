import os
import requests
from get_address_from_ghost import fetch_address
from urllib.parse import quote

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

q = "21 Kestrel Drive, Bury, BL9 6JE, Bury"

url = f"{BASE_URL}{quote(q)}&companyId={COMPANY_ID}"

response = session.get(BASE_URL, params={"text":q})
data = response.json()

new_booking_payload = {
    "capabilities": [],
    "companyId": 1,
    "driverNote": "Notes for Driver",
    "officeNote": "office note",
    "name": "Test Job",
    "customerId": 11915,
    "telephoneNumber": "01613008206",
    "pickup": {
        "address": {
            "bookingPriority": 0,
            "coordinate": data["coordinate"],
            "id": data["id"],
            "isCustom": data["isCustom"],
            "postCode": data["postCode"],
            "source": data["source"],
            "text": data["text"],
            "town": data["town"],
            "zone": data["zone"],
            "zoneId": data["zoneId"]
        },
        "note": "Please go inside",
        "type": "Pickup"
    },
    "pickupDueTime": "2027-04-16T15:00:00.699Z",
    "pickupDueTimeUtc": "2027-04-16T15:00:00.699Z",
    "priority": 1,
    "priorityOverride": True,
    "hold": False
}

# print(f"payload: {new_booking_payload}")

response = session.post(NEW_BOOKING_URL, json=new_booking_payload)
print(response.text)