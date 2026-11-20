"""
USGS EarthExplorer Machine-to-Machine (M2M) API client used by the USGS page.

Covers every dataset the M2M API offers (Landsat 1-9, ASTER, SRTM, declassified
imagery, aerial photos, ...). Login uses your ERS username + an application token.
Docs: https://m2m.cr.usgs.gov/
"""
import re
import threading
import time

import requests

API = "https://m2m.cr.usgs.gov/api/api/json/stable/"

# Friendly names for the most used datasets (any other dataset can be searched)
DATASETS = {
    "Landsat 8-9 OLI/TIRS · Level-2 (surface reflectance & temperature)": "landsat_ot_c2_l2",
    "Landsat 8-9 OLI/TIRS · Level-1 (incl. 15 m panchromatic)": "landsat_ot_c2_l1",
    "Landsat 7 ETM+ · Level-2": "landsat_etm_c2_l2",
    "Landsat 7 ETM+ · Level-1": "landsat_etm_c2_l1",
    "Landsat 4-5 TM · Level-2": "landsat_tm_c2_l2",
    "Landsat 4-5 TM · Level-1": "landsat_tm_c2_l1",
    "Landsat 1-5 MSS · Level-1": "landsat_mss_c2_l1",
}


class M2MError(RuntimeError):
    pass


class M2M:
    """Thread-safe M2M session. Logs in again by itself when the API key expires."""

    def __init__(self):
        self.lock = threading.RLock()
        self.user = None
        self._token = None
        self.key = None
        self.key_time = 0

    # ---- login
    def login(self, user, token):
        r = requests.post(API + "login-token", json={"username": user, "token": token}, timeout=60)
        js = _json(r)
        if js.get("errorCode") or not js.get("data"):
            return _msg(js, r)
        with self.lock:
            self.user, self._token = user, token
            self.key, self.key_time = js["data"], time.time()
        return None

    def logout(self):
        with self.lock:
            if self.key:
                try:
                    requests.post(API + "logout", json={}, headers={"X-Auth-Token": self.key}, timeout=30)
                except requests.RequestException:
                    pass
            self.user = self._token = self.key = None

    @property
    def ok(self):
        return bool(self.key)

    def _relogin(self, old_key):
        with self.lock:
            if self.key and self.key != old_key:
                return True                      # another thread already did it
            if not self._token:
                return False
            return self.login(self.user, self._token) is None

    # ---- generic call
    def call(self, endpoint, payload, tries=4):
        last = "no response"
        for attempt in range(tries):
            key = self.key
            if not key:
                raise M2MError("Not logged in to USGS.")
            try:
                r = requests.post(API + endpoint, json=payload, headers={"X-Auth-Token": key}, timeout=180)
            except requests.RequestException as e:
                last = str(e)
                time.sleep(5 * (attempt + 1))
                continue
            js = _json(r)
            code = str(js.get("errorCode") or "")
            if r.status_code == 200 and not code:
                return js.get("data")
            last = _msg(js, r)
            if code.startswith("AUTH") or r.status_code == 401:
                if self._relogin(key):
                    continue
                raise M2MError("USGS session expired - log in again. " + last)
            if code in ("RATE_LIMIT", "RATE_LIMIT_USER_DL") or r.status_code in (429, 500, 502, 503, 504):
                time.sleep(min(60, 10 * (attempt + 1)))
                continue
            raise M2MError(last)
        raise M2MError(last)

    # ---- datasets
    def dataset_search(self, text):
        data = self.call("dataset-search", {"datasetName": text}) or []
        out = []
        for d in data:
            alias = d.get("datasetAlias") or d.get("datasetName")
            if alias:
                out.append({"alias": alias, "name": d.get("collectionName") or alias,
                            "start": (d.get("temporalCoverage") or {}).get("startDate", "") or d.get("acquisitionStart", ""),
                            "end": (d.get("temporalCoverage") or {}).get("endDate", "") or d.get("acquisitionEnd", "")})
        return out

    # ---- scenes
    def scene_search(self, dataset, bounds, start, end, max_cloud=None, max_results=5000, progress=None):
        x0, y0, x1, y1 = bounds
        flt = {"spatialFilter": {"filterType": "mbr",
                                 "lowerLeft": {"latitude": y0, "longitude": x0},
                                 "upperRight": {"latitude": y1, "longitude": x1}},
               "acquisitionFilter": {"start": f"{start:%Y-%m-%d}", "end": f"{end:%Y-%m-%d}"}}
        if max_cloud is not None:
            flt["cloudCoverFilter"] = {"min": 0, "max": int(max_cloud), "includeUnknown": True}
        out, start_no = [], 1
        while len(out) < max_results:
            data = self.call("scene-search", {"datasetName": dataset, "maxResults": 500,
                                              "startingNumber": start_no, "metadataType": "summary",
                                              "sceneFilter": flt}) or {}
            res = data.get("results") or []
            out += [parse_scene(s) for s in res]
            if progress:
                progress(len(out), data.get("totalHits") or len(out))
            nxt = data.get("nextRecord")
            if not res or not nxt or nxt <= start_no or nxt > (data.get("totalHits") or 0):
                break
            start_no = nxt
        return out

    # ---- downloads
    def download_options(self, dataset, entity_ids):
        """{entityId: [product dict, ...]} with secondary (band) files listed too."""
        out = {}
        for i in range(0, len(entity_ids), 100):
            data = self.call("download-options", {"datasetName": dataset,
                                                  "entityIds": entity_ids[i:i + 100],
                                                  "includeSecondaryFileGroups": True}) or []
            for p in data:
                out.setdefault(p.get("entityId"), []).append(p)
        return out

    def request_url(self, entity_id, product_id, label, wait=900, sink=None):
        """Ask USGS for one file's download link; waits while USGS prepares it."""
        data = self.call("download-request", {"downloads": [{"entityId": entity_id, "productId": product_id}],
                                              "label": label}) or {}
        for d in data.get("availableDownloads") or []:
            if d.get("url"):
                return d["url"]
        if data.get("failed"):
            raise M2MError(f"USGS could not prepare the download: {data['failed']}")
        t0 = time.time()
        while time.time() - t0 < wait:
            if sink:
                sink.write(f"USGS is preparing the file... ({int(time.time() - t0)} s)")
            time.sleep(15)
            got = self.call("download-retrieve", {"label": label}) or {}
            for d in got.get("available") or []:
                if d.get("url"):
                    return d["url"]
        raise M2MError("USGS did not prepare the file in time - try again later.")


def _json(r):
    try:
        return r.json()
    except ValueError:
        return {"errorCode": f"HTTP_{r.status_code}", "errorMessage": r.text[:200]}


def _msg(js, r):
    return f"{js.get('errorCode') or 'HTTP ' + str(r.status_code)}: {js.get('errorMessage') or ''}".strip()


def parse_scene(s):
    browse = s.get("browse") or []
    tc = s.get("temporalCoverage") or {}
    try:
        cloud = float(s.get("cloudCover"))
        cloud = None if cloud < 0 else cloud
    except (TypeError, ValueError):
        cloud = None
    return {"entity": s.get("entityId"), "display": s.get("displayId") or s.get("entityId"),
            "date": str(tc.get("startDate") or s.get("publishDate") or "")[:10],
            "cloud": cloud, "footprint": s.get("spatialCoverage"),
            "browse": (browse[0].get("browsePath") or browse[0].get("thumbnailPath")) if browse else None,
            "thumb": (browse[0].get("thumbnailPath") or browse[0].get("browsePath")) if browse else None}


def wrs(display):
    """'LC09_L2SP_147036_20230715_...' -> 'Path 147 / Row 036'."""
    m = re.match(r"^L[COTEM]\d{2}_\w{4}_(\d{3})(\d{3})_", display or "")
    return f"Path {m.group(1)} / Row {m.group(2)}" if m else "-"


def pick_bundle(products):
    """The full product (bundle) from download-options, or the first available product."""
    avail = [p for p in products if p.get("available")]
    for p in avail:
        if "bundle" in str(p.get("productName", "")).lower():
            return p
    return avail[0] if avail else None


def band_files(products):
    """{file suffix: (entityId, productId, displayId)} for band-level downloads."""
    out = {}
    for p in products:
        for s in p.get("secondaryDownloads") or []:
            name = s.get("displayId") or ""
            if not s.get("available") or not name:
                continue
            m = re.search(r"_((?:SR_|ST_)?B\d{1,2}|QA_PIXEL|QA_RADSAT|ANG|MTL)\.(TIF|txt|xml|json)$", name, re.I)
            suffix = m.group(0)[1:] if m else name.split("_")[-1]
            out[suffix] = (s.get("entityId"), s.get("id"), name)
    return out


# ============================================================ processing recipes
def sensor(display):
    d = (display or "").upper()
    if d.startswith(("LC08", "LC09", "LO08", "LO09")):
        return "OLI"
    if d.startswith("LE07"):
        return "ETM"
    if d.startswith(("LT04", "LT05")):
        return "TM"
    if d.startswith("LM"):
        return "MSS"
    return None


RECIPES = {
    "OLI": {"TCC": ("rgb", ("B4", "B3", "B2")), "FCC": ("rgb", ("B5", "B4", "B3")),
            "SWIR": ("rgb", ("B7", "B5", "B4")), "NDVI": ("nd", ("B5", "B4")),
            "NDSI": ("nd", ("B3", "B6")), "NDWI": ("nd", ("B3", "B5"))},
    "ETM": {"TCC": ("rgb", ("B3", "B2", "B1")), "FCC": ("rgb", ("B4", "B3", "B2")),
            "SWIR": ("rgb", ("B7", "B4", "B3")), "NDVI": ("nd", ("B4", "B3")),
            "NDSI": ("nd", ("B2", "B5")), "NDWI": ("nd", ("B2", "B4"))},
    "MSS": {"FCC": ("rgb", ("B6", "B5", "B4")), "NDVI": ("nd", ("B6", "B5"))},
}
RECIPES["TM"] = RECIPES["ETM"]
STACK = {"OLI": ["B1", "B2", "B3", "B4", "B5", "B6", "B7"], "ETM": ["B1", "B2", "B3", "B4", "B5", "B7"],
         "TM": ["B1", "B2", "B3", "B4", "B5", "B7"], "MSS": ["B4", "B5", "B6", "B7"]}


def landsat_bands(folder):
    """{'B4': path, ...} from Landsat file names (*_SR_B4.TIF or *_B4.TIF)."""
    import os
    out = {}
    for root, _, files in os.walk(folder):
        for n in files:
            if "_clip" in n.lower():
                continue
            m = re.search(r"_(?:SR_)?B(\d{1,2})\.TIF$", n, re.I)
            if m and "_ST_" not in n.upper():
                out[f"B{int(m.group(1))}"] = os.path.join(root, n)
    return out


def landsat_scale(display):
    """Level-2 surface reflectance: DN * 0.0000275 - 0.2. Level-1: digital numbers."""
    if "_L2SP_" in (display or "").upper() or "_L2SR_" in (display or "").upper():
        return {f"B{i}": (2.75e-5, -0.2) for i in range(1, 10)}
    return {}
