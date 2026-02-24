import re
import os
import pandas as pd
import requests
from get_address_from_ghost import fetch_address

AUTOCAB_API_KEY = str(os.getenv("AUTOCAB_API_KEY"))
#COMPANY_ID = "2281"  # Replace with your actual company ID
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

#df = pd.read_excel("xl_data/nwas_logsheet.xlsx")

# df_postcode = df["to"].unique()

q = "M34 5LJ, 6, Dunstar Avenue, Audenshaw, Manchester"
fetched_address = fetch_address(q, 21380921)
print(fetched_address)

#url = f"{BASE_URL}{requests.utils.quote(df_postcode[0])}&companyId={COMPANY_ID}"

# url = f"{BASE_URL}{q}"

# response = session.get(BASE_URL, params={"text":q})
# data = response.json()

# print(data)

#post_code_regex = r"[A-Z]{1,2}[0-9][0-9A-Z]?\s?[0-9][A-Z]{2}"

# post_codes = [
#     re.search(post_code_regex, address.upper()).group()
#     for address in df_postcode
#     if re.search(post_code_regex, address.upper())
# ]

# for addr in df_postcode:
#     url = f"{BASE_URL}{requests.utils.quote(addr)}&companyId={COMPANY_ID}"

#     response = session.get(url)
#     if response.status_code == 200:
#         data = response.json()
#         if data.get("postCode") in post_codes:
#             print(data["postCode"])
#     else:
#         print(f"Postcode {data.get('postCode')} not found in the list. {addr}")
