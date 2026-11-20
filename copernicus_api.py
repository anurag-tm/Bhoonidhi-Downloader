"""
Copernicus Data Space Ecosystem (CDSE) client used by the Copernicus page.

- Search:      OData catalogue (no login needed)
- Full download: OData $value with a user token (Copernicus account) or an API-client token
- Server-side processing: Sentinel Hub Process API (needs a free OAuth client from the
  CDSE dashboard). Only your area is processed and downloaded.

Docs: https://documentation.dataspace.copernicus.eu/APIs.html
"""
import json
import math
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timedelta

import requests

TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
ODATA_URL = "https://catalogue.dataspace.copernicus.eu/odata/v1"
DOWNLOAD_URL = "https://download.dataspace.copernicus.eu/odata/v1/Products({id})/$value"
PROCESS_URL = "https://sh.dataspace.copernicus.eu/api/v1/process"
MAX_PX = 2500          # Sentinel Hub limit per request (width / height)


# ============================================================ login
class CDSEAuth:
    """Two independent logins, both refreshed automatically:
    - account (username / password [+ 2FA code]) -> full product downloads
    - API client (client id / secret)           -> server-side processing (and downloads)"""

    def __init__(self):
        self.lock = threading.RLock()
        self.user = None
        self._pwd = None
        self.access = self.refresh = None
        self.exp = 0
        self.refresh_exp = 0
        self.client_id = self._client_secret = None
        self.c_access = None
        self.c_exp = 0

    # ---- account
    def login(self, user, pwd, totp=None):
        data = {"grant_type": "password", "client_id": "cdse-public",
                "username": user, "password": pwd}
        if totp:
            data["totp"] = totp
        r = requests.post(TOKEN_URL, data=data, timeout=60)
        if r.status_code != 200:
            return _err(r)
        with self.lock:
            self.user, self._pwd = user, pwd
            self._store(r.json())
        return None

    def _store(self, js):
        now = time.time()
        self.access = js["access_token"]
        self.refresh = js.get("refresh_token", self.refresh)
        self.exp = now + max(30, int(js.get("expires_in", 600)) - 60)
        self.refresh_exp = now + max(60, int(js.get("refresh_expires_in", 3600)) - 60)

    def user_token(self, force=False):
        with self.lock:
            if not self.access and not self.refresh:
                return None
            if not force and self.access and time.time() < self.exp:
                return self.access
            if self.refresh and time.time() < self.refresh_exp:
                try:
                    r = requests.post(TOKEN_URL, data={"grant_type": "refresh_token",
                                                       "client_id": "cdse-public",
                                                       "refresh_token": self.refresh}, timeout=60)
                    if r.status_code == 200:
                        self._store(r.json())
                        return self.access
                except requests.RequestException:
                    pass
            if self._pwd:                                 # refresh expired -> log in again
                try:
                    if self.login(self.user, self._pwd) is None:
                        return self.access
                except requests.RequestException:
                    pass
            self.access = self.refresh = None
            return None

    def logout(self):
        with self.lock:
            self.user = self._pwd = self.access = self.refresh = None
            self.exp = self.refresh_exp = 0

    # ---- API client
    last_client_error = None

    def set_client(self, cid, secret):
        """Returns None when the client works, else an error message."""
        with self.lock:
            self.client_id, self._client_secret = cid, secret
            self.c_access, self.c_exp = None, 0
        if self.client_token(force=True):
            return None
        err = self.last_client_error or "Login failed"
        self.clear_client()
        return err

    def client_token(self, force=False):
        with self.lock:
            if not self.client_id:
                return None
            if not force and self.c_access and time.time() < self.c_exp:
                return self.c_access
            try:
                r = requests.post(TOKEN_URL, data={"grant_type": "client_credentials",
                                                   "client_id": self.client_id,
                                                   "client_secret": self._client_secret}, timeout=60)
            except requests.RequestException as e:
                self.last_client_error = str(e)
                return None
            if r.status_code != 200:
                self.last_client_error = _err(r)
                self.c_access = None
                return None
            js = r.json()
            self.c_access = js["access_token"]
            self.c_exp = time.time() + max(30, int(js.get("expires_in", 600)) - 60)
            self.last_client_error = None
            return self.c_access

    def clear_client(self):
        with self.lock:
            self.client_id = self._client_secret = self.c_access = None
            self.c_exp = 0

    # ---- headers for downloads: account first, else API client
    def download_headers(self, force=False):
        tok = self.user_token(force) or self.client_token(force)
        return {"Authorization": f"Bearer {tok}"} if tok else {}


def _retry_after(v, default=20):
    """Seconds to wait. Sentinel Hub may send milliseconds."""
    try:
        w = float(v)
    except (TypeError, ValueError):
        return default
    return min(w / 1000 if w > 1000 else w, 120) or default


def _err(r):
    try:
        js = r.json()
        msg = js.get("error_description") or js.get("error") or js.get("detail") or js
    except ValueError:
        msg = r.text[:200]
    return f"HTTP {r.status_code}: {msg}"


# ============================================================ catalogue
# Friendly names -> (OData collection, text that the product name must contain, has cloud cover)
FULL_PRODUCTS = {
    "Sentinel-2 L2A – surface reflectance (10 m)": ("SENTINEL-2", "MSIL2A", True),
    "Sentinel-2 L1C – top of atmosphere (10 m)": ("SENTINEL-2", "MSIL1C", True),
    "Sentinel-1 GRD – SAR, ground range (IW)": ("SENTINEL-1", "_IW_GRDH_", False),
    "Sentinel-1 SLC – SAR, single look complex (IW, for InSAR)": ("SENTINEL-1", "_IW_SLC__", False),
    "Sentinel-1 RTC – SAR, terrain corrected": ("SENTINEL-1-RTC", "", False),
    "Sentinel-3 OLCI full resolution L1 (300 m)": ("SENTINEL-3", "OL_1_EFR", False),
    "Sentinel-3 OLCI land L2 (300 m)": ("SENTINEL-3", "OL_2_LFR", False),
    "Sentinel-3 SLSTR land surface temperature L2": ("SENTINEL-3", "SL_2_LST", False),
    "Sentinel-5P NO2 L2 (air quality)": ("SENTINEL-5P", "L2__NO2___", False),
    "Copernicus DEM": ("COP-DEM", "", False),
    "Landsat 8 (CDSE copy)": ("LANDSAT-8", "", True),
    "Landsat 9 (CDSE copy)": ("LANDSAT-9", "", True),
    "Global mosaics": ("GLOBAL-MOSAICS", "", False),
}
ALL_COLLECTIONS = ["SENTINEL-1", "SENTINEL-1-RTC", "SENTINEL-2", "SENTINEL-3", "SENTINEL-5P",
                   "SENTINEL-6", "GLOBAL-MOSAICS", "SMOS", "ENVISAT", "LANDSAT-5", "LANDSAT-7",
                   "LANDSAT-8", "LANDSAT-9", "COP-DEM", "TERRA", "AQUA", "TERRAAQUA", "S2GLC",
                   "CLMS", "CCM"]


def _wkt(geom):
    """GeoJSON polygon -> WKT for OData (outer ring, max ~200 points)."""
    from shapely.geometry import shape
    g = shape(geom)
    if g.geom_type == "MultiPolygon":
        g = g.convex_hull
    tol = 0.0005
    while len(g.exterior.coords) > 200 and tol < 0.5:
        g = g.simplify(tol)
        tol *= 2
    pts = ",".join(f"{x:.5f} {y:.5f}" for x, y in g.exterior.coords)
    return f"POLYGON(({pts}))"


def build_filter(collection, name_contains, geom, start, end, max_cloud=None):
    f = [f"Collection/Name eq '{collection}'",
         f"OData.CSC.Intersects(area=geography'SRID=4326;{_wkt(geom)}')",
         f"ContentDate/Start gt {start:%Y-%m-%d}T00:00:00.000Z",
         f"ContentDate/Start lt {end:%Y-%m-%d}T23:59:59.999Z"]
    if name_contains:
        f.append(f"contains(Name,'{name_contains}')")
    if max_cloud is not None:
        f.append("Attributes/OData.CSC.DoubleAttribute/any(att:att/Name eq 'cloudCover' and "
                 f"att/OData.CSC.DoubleAttribute/Value le {float(max_cloud):.2f})")
    return " and ".join(f)


def search(collection, name_contains, geom, start, end, max_cloud=None, online_only=True,
           max_results=2000, session=None, progress=None):
    """Search the catalogue (split into 1-year pieces as CDSE recommends). Returns products."""
    http = session or requests
    chunks, a = [], start
    while a <= end:
        b = min(a + timedelta(days=364), end)
        chunks.append((a, b))
        a = b + timedelta(days=1)
    out, seen = [], set()
    for i, (a, b) in enumerate(chunks, 1):
        if progress:
            progress(i, len(chunks), a, b)
        flt = build_filter(collection, name_contains, geom, a, b, max_cloud)
        if online_only:
            flt += " and Online eq true"
        params = {"$filter": flt, "$orderby": "ContentDate/Start desc", "$top": "200",
                  "$expand": "Attributes"}
        url = f"{ODATA_URL}/Products"
        while url and len(out) < max_results:
            r = http.get(url, params=params, timeout=120)
            if r.status_code != 200:
                raise RuntimeError(f"Search failed - {_err(r)}")
            js = r.json()
            for p in js.get("value", []):
                if p.get("Id") not in seen:
                    seen.add(p.get("Id"))
                    out.append(parse_product(p))
            url = js.get("@odata.nextLink")
            params = None                    # nextLink already has the query
            time.sleep(0.2)
    return out


def parse_product(p):
    attrs = {a.get("Name"): a.get("Value") for a in p.get("Attributes", []) or []}
    start = (p.get("ContentDate") or {}).get("Start", "")
    return {
        "id": p.get("Id"),
        "name": p.get("Name", ""),
        "date": start[:10],
        "datetime": start,
        "size": p.get("ContentLength") or 0,
        "online": p.get("Online", True),
        "footprint": p.get("GeoFootprint"),
        "cloud": _num(attrs.get("cloudCover")),
        "orbit_dir": attrs.get("orbitDirection", ""),
        "rel_orbit": attrs.get("relativeOrbitNumber", ""),
        "tile": attrs.get("tileId", "") or _tile_from_name(p.get("Name", "")),
        "platform": attrs.get("platformShortName", "") + attrs.get("platformSerialIdentifier", ""),
        "product_type": attrs.get("productType", ""),
    }


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _tile_from_name(name):
    m = re.search(r"_T(\d{2}[A-Z]{3})_", name)
    return m.group(1) if m else ""


def location_of(p):
    """Same ground area: S2 tile, or S1 relative orbit + pass direction."""
    if p["tile"]:
        return f"Tile {p['tile']}"
    if p["rel_orbit"]:
        d = (p["orbit_dir"] or "")[:3].title()
        return f"Orbit {p['rel_orbit']} {d}".strip()
    return "-"


def quicklook(product_id, token=None, session=None):
    """Quick-look image bytes, or None."""
    http = session or requests
    try:
        r = http.get(f"{ODATA_URL}/Products({product_id})", params={"$expand": "Assets"}, timeout=40)
        if r.status_code != 200:
            return None
        for a in r.json().get("Assets", []) or []:
            if str(a.get("Type", "")).upper() == "QUICKLOOK":
                url = a.get("DownloadLink") or f"{ODATA_URL}/Assets({a.get('Id')})/$value"
                for hdr in ({}, {"Authorization": f"Bearer {token}"} if token else None):
                    if hdr is None:
                        continue
                    q = http.get(url, headers=hdr, timeout=40)
                    if q.status_code == 200 and q.content[:3] in (b"\xff\xd8\xff", b"\x89PN"):
                        return q.content
    except requests.RequestException:
        pass
    return None


# ============================================================ server-side processing
S2_L2A_BANDS = ["B01", "B02", "B03", "B04", "B05", "B06", "B07", "B08", "B8A", "B09", "B11", "B12"]
S2_L1C_BANDS = ["B01", "B02", "B03", "B04", "B05", "B06", "B07", "B08", "B8A", "B09", "B10", "B11", "B12"]


def _rgb_script(bands, gain):
    r, g, b = bands
    return f"""//VERSION=3
function setup() {{
  return {{ input: [{{ bands: ["{r}", "{g}", "{b}", "dataMask"] }}],
           output: {{ bands: 3, sampleType: "UINT8" }} }};
}}
function enc(v) {{ return Math.max(1, Math.min(255, Math.round(v * {gain} * 255))); }}
function evaluatePixel(s) {{
  if (s.dataMask == 0) return [0, 0, 0];
  return [enc(s.{r}), enc(s.{g}), enc(s.{b})];
}}"""


def _nd_script(a, b):
    return f"""//VERSION=3
function setup() {{
  return {{ input: [{{ bands: ["{a}", "{b}", "dataMask"] }}],
           output: {{ bands: 1, sampleType: "FLOAT32" }} }};
}}
function evaluatePixel(s) {{
  if (s.dataMask == 0) return [-9999];
  var d = (s.{a} - s.{b}) / (s.{a} + s.{b});
  return [isFinite(d) ? d : -9999];
}}"""


def _bands_script(bands):
    lst = ", ".join(f'"{b}"' for b in bands)
    vals = ", ".join(f"s.{b}" for b in bands)
    return f"""//VERSION=3
function setup() {{
  return {{ input: [{{ bands: [{lst}, "dataMask"], units: "REFLECTANCE" }}],
           output: {{ bands: {len(bands)}, sampleType: "FLOAT32" }} }};
}}
function evaluatePixel(s) {{
  if (s.dataMask == 0) return [{", ".join(["-9999"] * len(bands))}];
  return [{vals}];
}}"""


SCL_SCRIPT = """//VERSION=3
function setup() {
  return { input: [{ bands: ["SCL", "dataMask"] }], output: { bands: 1, sampleType: "UINT8" } };
}
function evaluatePixel(s) { return [s.dataMask == 0 ? 0 : s.SCL]; }"""


def _s1_db_script(p1, p2):
    return f"""//VERSION=3
function setup() {{
  return {{ input: [{{ bands: ["{p1}", "{p2}", "dataMask"] }}],
           output: {{ bands: 2, sampleType: "FLOAT32" }} }};
}}
function db(v) {{ return v > 0 ? 10 * Math.log(v) / Math.LN10 : -9999; }}
function evaluatePixel(s) {{
  if (s.dataMask == 0) return [-9999, -9999];
  return [db(s.{p1}), db(s.{p2})];
}}"""


def _s1_rgb_script(p1, p2):
    return f"""//VERSION=3
function setup() {{
  return {{ input: [{{ bands: ["{p1}", "{p2}", "dataMask"] }}],
           output: {{ bands: 3, sampleType: "UINT8" }} }};
}}
function db(v) {{ return v > 0 ? 10 * Math.log(v) / Math.LN10 : -40; }}
function sc(v, lo, hi) {{ return Math.max(1, Math.min(255, Math.round((v - lo) / (hi - lo) * 254 + 1))); }}
function evaluatePixel(s) {{
  if (s.dataMask == 0) return [0, 0, 0];
  var a = db(s.{p1}), b = db(s.{p2});
  return [sc(a, -25, 0), sc(b, -32, -5), sc(a - b, 0, 15)];
}}"""


DEM_SCRIPT = """//VERSION=3
function setup() {
  return { input: [{ bands: ["DEM", "dataMask"] }], output: { bands: 1, sampleType: "FLOAT32" } };
}
function evaluatePixel(s) { return [s.dataMask == 0 ? -9999 : s.DEM]; }"""


def _s2_products(bands):
    p = {
        "True colour (TCC)": ("rgb", ("B04", "B03", "B02")),
        "False colour (FCC: NIR-Red-Green)": ("rgb", ("B08", "B04", "B03")),
        "Snow / ice & geology (SWIR: B12-B8A-B4)": ("rgb", ("B12", "B8A", "B04")),
        "NDVI – vegetation": ("nd", ("B08", "B04")),
        "NDSI – snow & glaciers": ("nd", ("B03", "B11")),
        "NDWI – water & glacial lakes": ("nd", ("B03", "B08")),
        f"All {len(bands)} bands (reflectance, Float32)": ("bands", tuple(bands)),
    }
    return p


SERVER_DATA = {
    "Sentinel-2 L2A – surface reflectance": {
        "type": "sentinel-2-l2a", "res": 10, "cloud": True, "search": ("SENTINEL-2", "MSIL2A"),
        "products": {**_s2_products(S2_L2A_BANDS), "Scene classification (SCL: cloud, snow, water…)": ("scl", ())},
    },
    "Sentinel-2 L1C – top of atmosphere": {
        "type": "sentinel-2-l1c", "res": 10, "cloud": True, "search": ("SENTINEL-2", "MSIL1C"),
        "products": _s2_products(S2_L1C_BANDS),
    },
    "Sentinel-1 GRD – SAR backscatter": {
        "type": "sentinel-1-grd", "res": 10, "cloud": False, "search": ("SENTINEL-1", "_IW_GRDH_"),
        "products": {"Backscatter in dB (2 bands, Float32)": ("s1db", ()),
                     "SAR colour composite (co-pol, cross-pol, ratio)": ("s1rgb", ())},
    },
    "Copernicus DEM 30 m – elevation": {
        "type": "dem", "res": 30, "cloud": False, "search": None,
        "products": {"Elevation in metres (Float32)": ("dem", ())},
    },
}


def evalscript(kind, args, gain=2.5, pol=("VV", "VH")):
    if kind == "rgb":
        return _rgb_script(args, gain)
    if kind == "nd":
        return _nd_script(*args)
    if kind == "bands":
        return _bands_script(args)
    if kind == "scl":
        return SCL_SCRIPT
    if kind == "s1db":
        return _s1_db_script(*pol)
    if kind == "s1rgb":
        return _s1_rgb_script(*pol)
    if kind == "dem":
        return DEM_SCRIPT
    raise ValueError(kind)


def nodata_for(kind):
    return 0 if kind in ("rgb", "s1rgb", "scl") else -9999


def plan_tiles(bounds_utm, res, max_px=MAX_PX):
    """Split a UTM box into tiles of at most max_px x max_px pixels (snapped to res)."""
    x0, y0, x1, y1 = bounds_utm
    x0, y0 = math.floor(x0 / res) * res, math.floor(y0 / res) * res
    x1, y1 = math.ceil(x1 / res) * res, math.ceil(y1 / res) * res
    step = max_px * res
    tiles = []
    y = y0
    while y < y1:
        x = x0
        while x < x1:
            tiles.append((x, y, min(x + step, x1), min(y + step, y1)))
            x += step
        y += step
    return tiles


def build_request(data_type, bbox, epsg, res, time_from, time_to, script, max_cloud=None,
                  s1=None, mosaic="leastCC"):
    df = {"timeRange": {"from": time_from, "to": time_to}}
    item = {"type": data_type, "dataFilter": df}
    if data_type.startswith("sentinel-2"):
        if max_cloud is not None:
            df["maxCloudCoverage"] = int(max_cloud)
        df["mosaickingOrder"] = mosaic
    elif data_type == "sentinel-1-grd":
        s1 = s1 or {}
        df.update({"acquisitionMode": "IW", "polarization": s1.get("polarization", "DV"),
                   "resolution": "HIGH", "mosaickingOrder": "mostRecent"})
        if s1.get("orbit"):
            df["orbitDirection"] = s1["orbit"]
        proc = {"backCoeff": s1.get("backCoeff", "GAMMA0_TERRAIN"), "orthorectify": True,
                "demInstance": "COPERNICUS", "downsampling": "BILINEAR", "upsampling": "BILINEAR"}
        if s1.get("speckle"):
            proc["speckleFilter"] = {"type": "LEE", "windowSizeX": 5, "windowSizeY": 5}
        item["processing"] = proc
    elif data_type == "dem":
        item["dataFilter"] = {"demInstance": "COPERNICUS_30"}
        item["processing"] = {"upsampling": "BILINEAR", "downsampling": "BILINEAR"}
    return {
        "input": {"bounds": {"bbox": list(bbox),
                             "properties": {"crs": f"http://www.opengis.net/def/crs/EPSG/0/{epsg}"}},
                  "data": [item]},
        "output": {"resx": res, "resy": res,
                   "responses": [{"identifier": "default", "format": {"type": "image/tiff"}}]},
        "evalscript": script,
    }


def process_area(auth, aoi_geom, data_key, product, time_from, time_to, out_path, res=None,
                 max_cloud=None, gain=2.5, s1=None, mosaic="leastCC", sink=None, session=None,
                 clip=True):
    """Ask Copernicus to make the product for the AOI (tiled if large), then join the
    tiles and cut to the AOI. Returns (ok, message)."""
    from pyproj import Transformer
    from shapely.geometry import shape
    import geo_common as gc

    http = session or requests
    spec = SERVER_DATA[data_key]
    kind, args = spec["products"][product]
    res = res or spec["res"]
    g = shape(aoi_geom)
    c = g.centroid
    epsg = gc.utm_epsg(c.x, c.y)
    to_utm = Transformer.from_crs(4326, epsg, always_xy=True).transform
    tiles = plan_tiles(gc.reproject(g, to_utm).bounds, res)
    pol = ("HH", "HV") if (s1 or {}).get("polarization") == "DH" else ("VV", "VH")
    script = evalscript(kind, args, gain, pol)
    tmp = tempfile.mkdtemp(prefix="cdse_")
    files = []
    for i, bb in enumerate(tiles, 1):
        body = build_request(spec["type"], bb, epsg, res, time_from, time_to, script,
                             max_cloud if spec["cloud"] else None, s1, mosaic)
        for attempt in range(6):
            tok = auth.client_token(force=attempt > 0 and attempt % 2 == 1)
            if not tok:
                return False, ("Server-side processing needs an API client (client id + secret) - "
                               "add it in the sidebar. " + (auth.last_client_error or ""))
            if sink:
                sink.write(f"processing on Copernicus server: tile {i}/{len(tiles)}")
            try:
                r = http.post(PROCESS_URL, json=body, timeout=300,
                              headers={"Authorization": f"Bearer {tok}", "Accept": "image/tiff"})
            except requests.RequestException as e:
                time.sleep(10)
                err = str(e)
                continue
            if r.status_code == 200:
                p = os.path.join(tmp, f"t{i}.tif")
                with open(p, "wb") as fh:
                    fh.write(r.content)
                files.append(p)
                break
            err = _err(r)
            if r.status_code == 401:
                continue
            if r.status_code == 429:
                time.sleep(_retry_after(r.headers.get("Retry-After")))
                continue
            if r.status_code >= 500:
                time.sleep(15)
                continue
            return False, f"Copernicus refused the request - {err}"
        else:
            return False, f"Failed after retries - {err}"
        if sink:
            sink.progress(i / (len(tiles) + 1))
    cut = gc.write_aoi(aoi_geom, tmp) if clip else None
    gc.mosaic_tiles(files, out_path, cutline=cut, nodata=nodata_for(kind))
    if sink:
        sink.progress(1.0)
    return True, f"Saved: {out_path}"


# ============================================================ full-product processing (SAFE)
S2_RECIPES = {
    "TCC": ("rgb", ("B04", "B03", "B02")),
    "FCC": ("rgb", ("B08", "B04", "B03")),
    "SWIR": ("rgb", ("B12", "B8A", "B04")),
    "NDVI": ("nd", ("B08", "B04")),
    "NDSI": ("nd", ("B03", "B11")),
    "NDWI": ("nd", ("B03", "B08")),
}
S2_STACK = ["B02", "B03", "B04", "B08", "B11", "B12"]


def s2_bands(folder):
    """Find Sentinel-2 band files (finest resolution of each) inside an unzipped .SAFE."""
    best = {}
    for root, _, files in os.walk(folder):
        for n in files:
            m = re.search(r"_(B\d{2}|B8A)(?:_(10|20|60)m)?\.jp2$", n, re.I)
            if m:
                band, res = m.group(1).upper(), int(m.group(2) or 0)
                if band not in best or (res and res < best[band][0]):
                    best[band] = (res or 999, os.path.join(root, n))
    return {b: p for b, (_, p) in best.items()}


def s2_scale(product_name):
    """Digital number -> reflectance. Processing baseline 04.00+ (from 2022) adds +1000."""
    m = re.search(r"_N(\d{4})_", product_name)
    offset = -0.1 if (m and int(m.group(1)) >= 400) else 0.0
    return {b: (1e-4, offset) for b in S2_L1C_BANDS}
