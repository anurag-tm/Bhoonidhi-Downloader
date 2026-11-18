"""
Quick check that your Bhoonidhi account works with the API (no browser needed).

    python test_connection.py

It logs in, searches LISS-IV scenes for a small test area, prints them and logs out.
Edit BBOX / DATES below to test your own area.
"""
import getpass

import requests

BASE = "https://bhoonidhi-api.nrsc.gov.in"

# ---- edit these if you like ----
BBOX = [77.4, 34.0, 77.8, 34.3]                     # [min lon, min lat, max lon, max lat]
DATES = "2023-07-01T00:00:00Z/2023-10-31T23:59:59Z"  # max 366 days per search
COLLECTIONS = ["ResourceSat-2_LISS4-MX70_L2", "ResourceSat-2A_LISS4-MX70_L2"]
# --------------------------------


def main():
    user = input("Bhoonidhi user ID: ").strip()
    pwd = getpass.getpass("Password (hidden while typing): ")

    r = requests.post(f"{BASE}/auth/token",
                      json={"userId": user, "password": pwd, "grant_type": "password"}, timeout=60)
    print("Login:", r.status_code, "OK" if r.status_code == 200 else r.text[:200])
    if r.status_code != 200:
        return
    tok = r.json()
    try:
        body = {"collections": COLLECTIONS, "datetime": DATES, "bbox": BBOX, "limit": 50,
                "filter": {"args": [{"property": "Online"}, "Y"], "op": "eq"},
                "filter-lang": "cql2-json"}
        r = requests.post(f"{BASE}/data/search", json=body,
                          headers={"Authorization": f"Bearer {tok['access_token']}"}, timeout=120)
        print("Search:", r.status_code)
        if r.status_code == 200:
            feats = r.json().get("features", [])
            print(f"\nFound {len(feats)} online scenes:")
            for f in feats:
                print("  ", f["properties"].get("datetime", "")[:10], "|", f["collection"], "|", f["id"])
            print("\nEverything works - you can now run the app.")
        elif r.status_code == 404:
            print("Login works, but no scenes found for the test area/dates.")
        else:
            print(r.text[:300])
    finally:
        requests.post(f"{BASE}/auth/logout", json={"userId": user},
                      headers={"Authorization": f"Bearer {tok['refresh_token']}"}, timeout=60)
        print("Logged out.")


if __name__ == "__main__":
    main()
