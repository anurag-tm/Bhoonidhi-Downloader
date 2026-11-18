"""
Bhoonidhi Downloader - a simple desktop-style app (Streamlit) to search, preview,
download and clip ISRO Resourcesat LISS-IV / LISS-III / AWiFS data from the
NRSC Bhoonidhi API.

Run:  streamlit run app.py      (or double-click run_windows.bat)
Repo: https://github.com/anurag-tm/Bhoonidhi-Downloader
License: MIT. Not affiliated with NRSC / ISRO. You need your own Bhoonidhi account.
"""
import glob
import io
import json
import os
import re
import shutil
import tempfile
import threading
import time
import zipfile
from datetime import date

import pandas as pd
import pydeck as pdk
import requests
import streamlit as st

BASE = "https://bhoonidhi-api.nrsc.gov.in"

COLLECTIONS = [
    "ResourceSat-2_LISS4-MX70_L2",
    "ResourceSat-2A_LISS4-MX70_L2",
    "ResourceSat-2_LISS3_L2",
    "ResourceSat-2A_LISS3_L2",
    "ResourceSat-2_LISS3_BOA",
    "ResourceSat-2A_LISS3_BOA",
    "ResourceSat-2_AWIFS_L2",
    "ResourceSat-2A_AWIFS_L2",
]
DEFAULT_COLS = COLLECTIONS[:2]

CLOUD_KEYS = ["eo:cloud_cover", "cloud_cover", "CloudCover", "Cloud_Cover",
              "cloudCover", "CLOUD_COVER", "Cloud_Percentage", "cloud_percentage"]

ERRORS = {
    400: "Bad request - check inputs.",
    401: "Session expired - log in again.",
    403: "Max sessions active - log out other sessions or wait.",
    404: "Product not on online storage.",
    412: "Too many concurrent downloads - wait for running ones.",
    429: "Rate limit hit - wait a while before retrying.",
    500: "Server error - try again later.",
    503: "Catalogue unavailable - try again later.",
    504: "Download interrupted - try again.",
}
RASTER_EXT = (".tif", ".tiff", ".img")
CHUNK_DAYS = 360          # Bhoonidhi max is 366 days per search
REFRESH_AFTER = 18 * 60   # refresh access token after 18 min (it lasts 20)
INDEX_FILE = "_downloaded.json"

st.set_page_config(page_title="Bhoonidhi Downloader", page_icon="🛰️", layout="wide")
ss = st.session_state
for k, v in {"feats": [], "aoi": None}.items():
    ss.setdefault(k, v)


class AuthState:
    """Login state shared by the page and the parallel download threads (thread-safe)."""
    def __init__(self):
        self.lock = threading.RLock()
        self.token = self.refresh = self.pwd_keep = None
        self.exp = 0
        self.user = ""


@st.cache_resource
def _shared_auth():
    """One login for the whole running app, so reloading the page or opening a new tab
    does not log you out. Cleared only by Logout or by closing the app."""
    return AuthState()


AUTH = _shared_auth()

# remembered settings (download folder, clip options, parallel downloads)
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bhoonidhi_settings.json")


def load_settings():
    try:
        with open(SETTINGS_FILE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_settings(**kw):
    cur = load_settings()
    if any(cur.get(k) != v for k, v in kw.items()):
        cur.update(kw)
        try:
            with open(SETTINGS_FILE, "w") as fh:
                json.dump(cur, fh, indent=1)
        except OSError:
            pass


CFG = load_settings()

# Last area + last search are saved next to the app, so a dropped browser connection,
# PC sleep or app restart does not lose them.
APP_DIR = os.path.dirname(SETTINGS_FILE)
LAST_AOI_FILE = os.path.join(APP_DIR, "bhoonidhi_last_aoi.json")
LAST_SEARCH_FILE = os.path.join(APP_DIR, "bhoonidhi_last_search.json")


def _save_json(path, obj):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(obj, fh)
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        pass


def _load_json(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _cfg_date(key, default):
    try:
        d = date.fromisoformat(CFG[key])
        return min(max(d, date(2011, 1, 1)), date.today())
    except (KeyError, TypeError, ValueError):
        return default


# restore the last search when a new page session starts
if not ss.feats and "restored_search" not in ss:
    ss.restored_search = None
    _last = _load_json(LAST_SEARCH_FILE)
    if _last and _last.get("feats") and _last.get("aoi"):
        ss.feats = _last["feats"]
        ss.aoi = (_last["aoi"][0], tuple(_last["aoi"][1]))
        ss.restored_search = _last.get("time", "")


# ---------------- Auth ----------------
def err_text(r):
    return f"HTTP {r.status_code}: {ERRORS.get(r.status_code, '')} {r.text[:300]}"


def do_login(user, pwd):
    r = requests.post(f"{BASE}/auth/token",
                      json={"userId": user, "password": pwd, "grant_type": "password"},
                      timeout=60)
    if r.status_code != 200:
        return err_text(r)
    with AUTH.lock:
        AUTH.user = user
        set_tokens(r.json())
    return None


def set_tokens(js):
    """Store tokens; plan the next refresh at 18 min (or 2 min before expiry if shorter)."""
    with AUTH.lock:
        AUTH.token = js["access_token"]
        AUTH.refresh = js.get("refresh_token", AUTH.refresh)
        life = int(js.get("expires_in", 1200))
        AUTH.exp = time.time() + max(60, min(REFRESH_AFTER, life - 120))


def ensure_token(force=False, failed_token=None):
    """Valid access token? If not: refresh -> auto re-login (if allowed) -> give up.
    Only one thread refreshes at a time; others reuse the new token."""
    with AUTH.lock:
        if failed_token and AUTH.token and AUTH.token != failed_token:
            return True                      # another thread already got a new token
        if not force and AUTH.token and time.time() < AUTH.exp:
            return True
        if AUTH.refresh:
            try:
                r = requests.post(f"{BASE}/auth/token",
                                  json={"userId": AUTH.user, "refresh_token": AUTH.refresh,
                                        "grant_type": "refresh_token"}, timeout=60)
                if r.status_code == 200:
                    set_tokens(r.json())
                    return True
            except requests.RequestException:
                pass
        if AUTH.pwd_keep:
            try:
                if do_login(AUTH.user, AUTH.pwd_keep) is None:
                    return True
            except requests.RequestException:
                pass
        AUTH.token = AUTH.refresh = None
        return False


def do_logout():
    with AUTH.lock:
        if AUTH.refresh:
            try:
                requests.post(f"{BASE}/auth/logout", json={"userId": AUTH.user},
                              headers={"Authorization": f"Bearer {AUTH.refresh}"}, timeout=30)
            except requests.RequestException:
                pass
        AUTH.token = AUTH.refresh = AUTH.pwd_keep = None
        AUTH.exp = 0


def auth_header():
    with AUTH.lock:
        return {"Authorization": f"Bearer {AUTH.token}"}


def _keep_alive(auth_obj):
    """Background timer: refresh the token every 18 min even while the page is idle."""
    while True:
        time.sleep(30)
        try:
            if auth_obj.token and time.time() >= auth_obj.exp:
                ensure_token()
        except Exception:
            pass


@st.cache_resource
def _start_keep_alive():
    t = threading.Thread(target=_keep_alive, args=(AUTH,), daemon=True)
    t.start()
    return t


_start_keep_alive()

# "Remember me on this PC": password stored in the OS password store
# (Windows Credential Manager / macOS Keychain) via the keyring package - never in a text file.
KEYRING_SERVICE = "bhoonidhi-downloader"


def _keyring():
    try:
        import keyring
        keyring.get_keyring()
        return keyring
    except Exception:
        return None


def remember_login(user, pwd):
    kr = _keyring()
    if kr is None:
        return False
    try:
        kr.set_password(KEYRING_SERVICE, user, pwd)
        save_settings(remember_user=user)
        return True
    except Exception:
        return False


def forget_login():
    user = load_settings().get("remember_user")
    kr = _keyring()
    if user and kr is not None:
        try:
            kr.delete_password(KEYRING_SERVICE, user)
        except Exception:
            pass
    save_settings(remember_user=None)


def try_auto_login():
    """On app start: log in with the remembered password, once per app run."""
    user = load_settings().get("remember_user")
    if AUTH.token or not user or getattr(AUTH, "auto_tried", False):
        return None
    AUTH.auto_tried = True
    kr = _keyring()
    if kr is None:
        return None
    try:
        pwd = kr.get_password(KEYRING_SERVICE, user)
    except Exception:
        pwd = None
    if not pwd:
        return None
    err = do_login(user, pwd)
    if err is None:
        AUTH.pwd_keep = pwd
    return err


# ---------------- AOI ----------------
def point_aoi(lat, lon, size_km, shape_kind="Circle"):
    """Circle (size = radius) or square (size = side length), true km on the ground."""
    from pyproj import CRS, Transformer
    from shapely.geometry import Point, Polygon, mapping
    from shapely.ops import transform

    aeqd = CRS.from_proj4(f"+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m")
    to_ll = Transformer.from_crs(aeqd, "EPSG:4326", always_xy=True).transform
    if shape_kind == "Square":
        h = size_km * 1000 / 2
        local = Polygon([(-h, -h), (h, -h), (h, h), (-h, h)]).segmentize(h / 10)
    else:
        local = Point(0, 0).buffer(size_km * 1000, 64)
    geom = transform(to_ll, local)
    return json.loads(json.dumps(mapping(geom))), geom.bounds


def read_shapefile(files):
    import geopandas as gpd
    from shapely.geometry import mapping

    tmp = tempfile.mkdtemp()
    for f in files:
        data = f.getvalue()
        if f.name.lower().endswith(".zip"):
            zipfile.ZipFile(io.BytesIO(data)).extractall(tmp)
        else:
            with open(os.path.join(tmp, f.name), "wb") as fh:
                fh.write(data)
    found = [os.path.join(r, n) for r, _, fs in os.walk(tmp) for n in fs
             if n.lower().endswith((".shp", ".geojson", ".json", ".gpkg"))]
    if not found:
        raise ValueError("No .shp / .geojson / .gpkg found in upload.")
    gdf = gpd.read_file(found[0])
    note = ""
    if gdf.crs is None:
        note = "Shapefile has no .prj - assumed WGS84 (EPSG:4326)."
        gdf = gdf.set_crs(4326)
    else:
        gdf = gdf.to_crs(4326)
    geom = gdf.union_all() if hasattr(gdf, "union_all") else gdf.unary_union
    if geom.geom_type not in ("Polygon", "MultiPolygon"):
        geom = geom.convex_hull if geom.geom_type != "Point" else geom.buffer(0.01)
    tol = 0.0005
    while len(json.dumps(mapping(geom))) > 40000 and tol < 0.1:
        geom = geom.simplify(tol, preserve_topology=True)
        tol *= 2
    return json.loads(json.dumps(mapping(geom))), geom.bounds, note


def bbox_geojson(b):
    x0, y0, x1, y1 = b
    return {"type": "Polygon",
            "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}


# ---------------- Search ----------------
def to_number(v):
    """12.5 / "12.5" / "12%" / "Cloud: 12" -> 12.5; anything else -> None."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d+(?:\.\d+)?", str(v))
    return float(m.group()) if m else None


def flat_props(props, prefix=""):
    out = {}
    for k, v in (props or {}).items():
        if isinstance(v, dict):
            out.update(flat_props(v, prefix + k + "."))
        else:
            out[prefix + k] = v
    return out


def numeric_fields(feats):
    """Property names that hold a number (or number-like text) in at least one scene."""
    keys = {}
    for f in feats:
        for k, v in flat_props(f.get("properties")).items():
            if k != "datetime" and to_number(v) is not None:
                keys[k] = keys.get(k, 0) + 1
    return sorted(keys)


EST_FIELD = "estimate.cloud_snow_pct_from_quicklook"


def guess_cloud_field(keys):
    """Prefer a real cloud field (search result or .meta file); fall back to the estimate."""
    for k in CLOUD_KEYS:
        if k in keys:
            return k
    for k in keys:
        if k != EST_FIELD and ("cloud" in k.lower() or k.lower().split(".")[-1] == "cc"):
            return k
    return EST_FIELD if EST_FIELD in keys else None


# ---------------- Cloud info from .meta file / quick-look ----------------
def parse_meta(text):
    """Pick numeric values out of a .meta file (key=value / key: value lines or XML tags)."""
    out = {}
    for m in re.finditer(r"<([A-Za-z_][\w.:-]*)[^>/]*>\s*([^<]{1,40}?)\s*</\1>", text):
        out[m.group(1)] = m.group(2)
    for m in re.finditer(r"(?m)^\s*([A-Za-z][\w .()/-]{0,60}?)\s*[=:]\s*(\S[^\r\n]{0,40})$", text):
        out.setdefault(m.group(1).strip().replace(" ", "_"), m.group(2).strip())
    return {k: v for k, v in out.items() if to_number(v) is not None}


def quicklook_white_pct(jpg_bytes):
    """% of scene pixels that are bright and grey/white (cloud OR snow) in the quick-look."""
    import numpy as np
    from PIL import Image
    im = np.asarray(Image.open(io.BytesIO(jpg_bytes)).convert("RGB")).astype("int16")
    valid = im.max(axis=2) > 12                      # skip black fill outside the scene
    if valid.sum() < 100:
        return None
    bright = im.min(axis=2) > 180                     # all bands bright
    grey = (im.max(axis=2) - im.min(axis=2)) < 45     # little colour = white/grey
    return round(float((bright & grey & valid).sum()) / float(valid.sum()) * 100, 1)


def _get(url, hdr):
    for h in (hdr, {}):
        try:
            r = requests.get(url, headers=h, timeout=40)
            if r.status_code == 200 and r.content:
                return r
        except requests.RequestException:
            pass
    return None


def scene_cloud_info(item, hdr):
    """Returns dict of extra numeric properties for one scene (runs in a worker thread)."""
    extra = {}
    meta = ((item.get("assets") or {}).get("metadata") or {}).get("href")
    if meta:
        r = _get(meta, hdr)
        if r is not None:
            extra.update({f"meta.{k}": v for k, v in parse_meta(r.text).items()})
    if not any("cloud" in k.lower() for k in extra):
        for u in find_quicklooks(item):
            r = _get(u, hdr)
            if r is not None and r.content[:3] == b"\xff\xd8\xff" or (r is not None and r.content[:3] == b"\x89PN"):
                try:
                    pct = quicklook_white_pct(r.content)
                except Exception:
                    pct = None
                if pct is not None:
                    extra[EST_FIELD] = pct
                    break
    return extra


def add_cloud_info(feats, hdr, prog):
    from concurrent.futures import ThreadPoolExecutor, as_completed
    cache = ss.setdefault("cloud_cache", {})
    todo = [f for f in feats if f["id"] not in cache]
    if todo:
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = {ex.submit(scene_cloud_info, f, hdr): f["id"] for f in todo}
            for i, fu in enumerate(as_completed(futs), 1):
                try:
                    cache[futs[fu]] = fu.result()
                except Exception:
                    cache[futs[fu]] = {}
                prog.progress(i / len(todo), text=f"Reading cloud info {i}/{len(todo)}...")
    for f in feats:
        f.setdefault("properties", {}).update(cache.get(f["id"], {}))


def location_key(f):
    """Same ground footprint = same satellite + path + row + subscene."""
    p = f.get("properties") or {}
    path, row, sub = p.get("Path"), p.get("Row"), p.get("Subscene_ID")
    if not (path and row):
        parts = str(p.get("Scene_Identifier", "")).split("_")
        if len(parts) >= 5:
            path, row, sub = parts[-3], parts[-2], parts[-1]
    if not (path and row):
        return "Unknown"
    sat = "RS2A" if "2A" in f.get("collection", "") else "RS2"
    return f"Path {int(path) if str(path).isdigit() else path} / Row {int(row) if str(row).isdigit() else row}" \
           + (f" / {sub}" if sub else "") + f" ({sat})"


def aoi_cover_pct(f, aoi_shp):
    """% of the AOI inside this scene's footprint."""
    try:
        from shapely.geometry import shape
        return round(shape(f["geometry"]).intersection(aoi_shp).area / aoi_shp.area * 100)
    except Exception:
        return None


def get_cloud(props, field):
    if not field:
        return None
    return to_number(flat_props(props).get(field))


def search(body, max_pages=20):
    feats, url, method, payload = [], f"{BASE}/data/search", "POST", body
    page, retried = 0, False
    while page < max_pages:
        if not ensure_token():
            raise RuntimeError("Session expired - log in again.")
        if method == "POST":
            r = requests.post(url, json=payload, headers=auth_header(), timeout=120)
        else:
            r = requests.get(url, headers=auth_header(), timeout=120)
        if r.status_code == 401 and not retried:
            retried = True
            ensure_token(force=True)
            continue
        if r.status_code == 404 and "NO RESULTS" in r.text.upper():
            break
        if r.status_code != 200:
            raise RuntimeError(f"Search failed - HTTP {r.status_code}: {r.text[:300]}")
        retried = False
        page += 1
        js = r.json()
        feats += js.get("features", [])
        nxt = [l for l in js.get("links", []) if l.get("rel") == "next"]
        if not nxt or not js.get("features"):
            break
        url = nxt[0]["href"]
        method = nxt[0].get("method", "GET").upper()
        payload = nxt[0].get("body", payload) if method == "POST" else None
        time.sleep(0.4)
    seen, out = set(), []
    for f in feats:
        if f["id"] not in seen:
            seen.add(f["id"])
            out.append(f)
    return out


# ---------------- Download ----------------
def load_index(out_dir):
    try:
        with open(os.path.join(out_dir, INDEX_FILE)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


INDEX_LOCK = threading.Lock()


def save_index(out_dir, scene_id, path):
    with INDEX_LOCK:
        idx = load_index(out_dir)
        idx[scene_id] = path
        with open(os.path.join(out_dir, INDEX_FILE), "w") as fh:
            json.dump(idx, fh, indent=1)


def find_existing(out_dir, scene_id):
    p = load_index(out_dir).get(scene_id)
    if p and os.path.isfile(p):
        return p
    for p in glob.glob(os.path.join(out_dir, scene_id + "*")):
        if os.path.isfile(p) and not p.endswith(".part"):
            return p
    return None


def download_item(item, out_dir, bar, label, max_tries=8):
    """Download with auto re-login and resume (HTTP Range). Returns (ok, message, path)."""
    os.makedirs(out_dir, exist_ok=True)
    sid = item["id"]
    have = find_existing(out_dir, sid)
    if have:
        bar.progress(1.0)
        return True, f"Already downloaded: {have}", have
    params = {"id": sid, "collection": item["collection"]}
    part = os.path.join(out_dir, sid + ".part")   # kept between runs, so a later click resumes
    relogs, last_err = 0, "Failed after retries."
    for attempt in range(1, max_tries + 1):
        if not ensure_token():
            return False, "Session expired and could not log in again - log in from the sidebar.", None
        start = os.path.getsize(part) if os.path.exists(part) else 0
        hdr = auth_header()
        if start:
            hdr["Range"] = f"bytes={start}-"
            label.write(f"{sid}: resuming from {start / 1e6:.1f} MB...")
        try:
            with requests.get(f"{BASE}/download", params=params, headers=hdr,
                              stream=True, timeout=(30, 300)) as r:
                if r.status_code == 401 and relogs < 3:
                    relogs += 1
                    label.write(f"{sid}: session expired, logging in again...")
                    ensure_token(force=True, failed_token=hdr["Authorization"][7:])
                    continue
                if r.status_code == 416:            # bad range -> start this file again
                    os.remove(part)
                    continue
                if r.status_code in (412, 429, 500, 502, 503, 504):
                    last_err = err_text(r)
                    label.write(f"{sid}: {ERRORS.get(r.status_code, '')} retry {attempt}/{max_tries} in 30 s...")
                    time.sleep(30)
                    continue
                if r.status_code not in (200, 206):
                    return False, err_text(r), None

                if r.status_code == 206:
                    mode, done = "ab", start
                    m = re.search(r"/(\d+)", r.headers.get("Content-Range", ""))
                    total = int(m.group(1)) if m else 0
                else:                                # server sent the whole file
                    if start:
                        label.write(f"{sid}: server can't resume, restarting this file...")
                    mode, done = "wb", 0
                    total = int(r.headers.get("Content-Length", 0))
                cd = r.headers.get("Content-Disposition", "")
                m = re.search(r'filename="?([^";]+)', cd)
                name = m.group(1) if m else f"{sid}.zip"

                with open(part, mode) as fh:
                    for chunk in r.iter_content(1024 * 1024):
                        fh.write(chunk)
                        done += len(chunk)
                        if total:
                            bar.progress(min(done / total, 1.0))
                        label.write(f"{sid}: {done / 1e6:.1f} MB"
                                    + (f" / {total / 1e6:.1f} MB" if total else ""))
                        if time.time() >= AUTH.exp:  # 18 min reached -> refresh token now
                            ensure_token()
                if total and done < total:
                    raise requests.ConnectionError(f"stopped at {done / 1e6:.1f} of {total / 1e6:.1f} MB")
            path = os.path.join(out_dir, name)
            os.replace(part, path)
            save_index(out_dir, sid, path)
            bar.progress(1.0)
            return True, f"Saved: {path}", path
        except requests.RequestException as e:
            last_err = f"Connection lost ({e})"
            got = os.path.getsize(part) / 1e6 if os.path.exists(part) else 0
            label.write(f"{sid}: connection lost at {got:.1f} MB - resuming in 15 s "
                        f"(try {attempt}/{max_tries})")
            time.sleep(15)
    return False, last_err + " - partial file kept; click Download again to resume.", None


class _Sink:
    """Stand-in for Streamlit progress/label used inside worker threads."""
    def __init__(self, st_dict, key):
        self.d, self.k = st_dict, key

    def progress(self, v):
        self.d[self.k]["pct"] = float(v)

    def write(self, msg):
        self.d[self.k]["msg"] = str(msg)


def _job(item, out_dir, do_clip, do_stack, keep_full, aoi_geom, state):
    sid = item["id"]
    sink = _Sink(state, sid)
    state[sid]["status"] = "downloading"
    ok, msg, path = download_item(item, out_dir, sink, sink)
    clip_msg = ""
    if ok and do_clip:
        state[sid]["status"] = "clipping"
        sink.write(f"{sid}: clipping...")
        try:
            clip_msg = clip_product(path, aoi_geom, do_stack, keep_full)
        except Exception as e:
            clip_msg = f"Clip failed: {e}"
    state[sid].update(status="done" if ok else "failed", pct=1.0 if ok else state[sid]["pct"],
                      msg=msg + (f" | {clip_msg}" if clip_msg else ""), dl=msg, clip=clip_msg)


def run_downloads(items, out_dir, n_par, do_clip, do_stack, keep_full, aoi_geom):
    """Download (and clip) scenes, n_par at a time, with live progress for each."""
    from concurrent.futures import ThreadPoolExecutor
    os.makedirs(out_dir, exist_ok=True)
    state = {f["id"]: {"status": "waiting", "pct": 0.0, "msg": "waiting...", "dl": "", "clip": ""}
             for f in items}
    overall = st.progress(0.0, text="Starting...")
    rows = {}
    for f in items:
        c1, c2 = st.columns([1, 3])
        rows[f["id"]] = (c1.empty(), c2.progress(0.0), c2.empty())
    icon = {"waiting": "⏳", "downloading": "⬇️", "clipping": "✂️", "done": "✅", "failed": "❌"}
    with ThreadPoolExecutor(max_workers=max(1, min(5, n_par))) as ex:
        futs = [ex.submit(_job, f, out_dir, do_clip, do_stack, keep_full, aoi_geom, state)
                for f in items]
        while True:
            for sid, (head, bar, line) in rows.items():
                s_ = state[sid]
                head.markdown(f"{icon[s_['status']]} `{sid}`")
                bar.progress(min(max(s_["pct"], 0.0), 1.0))
                line.caption(s_["msg"])
            done = sum(s_["status"] in ("done", "failed") for s_ in state.values())
            active = sum(s_["status"] in ("downloading", "clipping") for s_ in state.values())
            overall.progress(done / len(items),
                             text=f"{done}/{len(items)} finished, {active} running")
            if all(fu.done() for fu in futs):
                break
            time.sleep(0.7)
        for fu in futs:
            exc = fu.exception()
            if exc:
                st.error(f"Unexpected error: {exc}")
    for sid, (head, bar, line) in rows.items():
        s_ = state[sid]
        head.markdown(f"{icon[s_['status']]} `{sid}`")
        bar.progress(min(max(s_["pct"], 0.0), 1.0))
        line.caption(s_["msg"])
    n_ok = sum(s_["status"] == "done" for s_ in state.values())
    (st.success if n_ok == len(items) else st.warning)(
        f"Done: {n_ok} of {len(items)} scene(s) finished."
        + ("" if n_ok == len(items) else " Click Download again to resume the failed ones."))
    st.dataframe(pd.DataFrame([{"ID": k, "Result": v["status"], "Download": v["dl"], "Clip": v["clip"]}
                               for k, v in state.items()]), hide_index=True, use_container_width=True)


# ---------------- Unzip + clip (GDAL) ----------------
def gdal_available():
    try:
        from osgeo import gdal  # noqa: F401
        return True
    except ImportError:
        return False


def clip_product(path, aoi_geom, stack, keep_full):
    """Unzip product, clip every band GeoTIFF to the AOI, optionally stack. Returns message."""
    from osgeo import gdal
    gdal.UseExceptions()

    folder = os.path.dirname(path)
    base = re.sub(r"\.(zip|tar\.gz|tgz|tar)$", "", os.path.basename(path), flags=re.I)
    ex_dir = os.path.join(folder, base)
    unpacked = False
    if os.path.isfile(path) and path.lower().endswith(RASTER_EXT):
        rasters = [path]
    else:
        if not os.path.isdir(ex_dir):
            shutil.unpack_archive(path, ex_dir)
        unpacked = True
        rasters = sorted(os.path.join(r, n) for r, _, fs in os.walk(ex_dir) for n in fs
                         if n.lower().endswith(RASTER_EXT))
    if not rasters:
        return "No GeoTIFF bands found inside the product."

    clip_dir = os.path.join(folder, base + "_clip")
    os.makedirs(clip_dir, exist_ok=True)
    cut = os.path.join(clip_dir, "aoi.geojson")
    with open(cut, "w") as fh:
        json.dump({"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {}, "geometry": aoi_geom}]}, fh)

    outs, skipped = [], []
    co = ["COMPRESS=DEFLATE", "TILED=YES", "BIGTIFF=IF_SAFER"]
    for src in rasters:
        out = os.path.join(clip_dir, os.path.splitext(os.path.basename(src))[0] + "_clip.tif")
        try:
            ds = gdal.Warp(out, src, cutlineDSName=cut, cropToCutline=True,
                           dstNodata=0, creationOptions=co)
            ds.GetRasterBand(1).ComputeRasterMinMax(False)  # raises if no valid pixel (no overlap)
            ds = None
            outs.append(out)
        except RuntimeError:
            ds = None                                # release file (Windows locks it)
            skipped.append(os.path.basename(src))   # AOI does not overlap this band/scene
            if os.path.exists(out):
                os.remove(out)

    msg = f"Clipped {len(outs)} band(s) -> {clip_dir}"
    if stack and len(outs) > 1:
        vrt = os.path.join(clip_dir, "_stack.vrt")
        gdal.BuildVRT(vrt, outs, separate=True)
        stacked = os.path.join(clip_dir, base + "_stack_clip.tif")
        gdal.Translate(stacked, vrt, creationOptions=co)
        os.remove(vrt)
        msg += f"; stacked: {os.path.basename(stacked)} (band order: " \
               + ", ".join(os.path.basename(o).replace("_clip.tif", "") for o in outs) + ")"
    if skipped:
        msg += f"; no overlap: {', '.join(skipped)}"
    if not keep_full:
        if unpacked:
            shutil.rmtree(ex_dir, ignore_errors=True)
        if not path.lower().endswith(RASTER_EXT):
            os.remove(path)
        msg += "; full scene deleted"
    return msg


# ---------------- Compare viewer ----------------
IMG_EXT = (".jpg", ".jpeg", ".png")


def find_quicklooks(item):
    """Browse / thumbnail image URLs from the STAC item, best first."""
    from urllib.parse import urljoin
    cands = []
    for k, a in (item.get("assets") or {}).items():
        href = a.get("href", "")
        txt = (k + " " + " ".join(a.get("roles") or []) + " " + (a.get("title") or "")).lower()
        typ = (a.get("type") or "").lower()
        if any(w in txt for w in ("browse", "quick", "overview", "preview", "thumb")) \
                or typ.startswith(("image/jpeg", "image/png")) \
                or href.lower().split("?")[0].endswith(IMG_EXT):
            rank = 2 if "thumb" in txt else (0 if any(w in txt for w in ("browse", "quick", "overview")) else 1)
            cands.append((rank, href))
    for l in item.get("links") or []:
        if l.get("rel") in ("browse", "preview", "thumbnail"):
            cands.append((2 if l["rel"] == "thumbnail" else 0, l.get("href", "")))
    for v in (item.get("properties") or {}).values():
        if isinstance(v, str) and v.lower().split("?")[0].endswith(IMG_EXT):
            cands.append((1, v))
    seen, out = set(), []
    for _, h in sorted(cands):
        u = urljoin(BASE + "/", h)
        if h and u not in seen:
            seen.add(u)
            out.append(u)
    return out


@st.cache_data(show_spinner=False, max_entries=64)
def fetch_image(url, _hdr):
    """Fetch an image (with token, then without) -> data URL, or None."""
    import base64
    for h in (_hdr, {}):
        try:
            r = requests.get(url, headers=h, timeout=60)
        except requests.RequestException:
            continue
        ctype = r.headers.get("Content-Type", "").split(";")[0]
        if r.status_code == 200 and (ctype.startswith("image/") or r.content[:3] in (b"\xff\xd8\xff", b"\x89PN")):
            if not ctype.startswith("image/"):
                ctype = "image/png" if r.content[:3] == b"\x89PN" else "image/jpeg"
            return f"data:{ctype};base64," + base64.b64encode(r.content).decode()
    return None


def scene_corners(geom):
    """Top-left, top-right, bottom-left corners [lat, lon] of the scene footprint (approx.)."""
    try:
        from shapely.geometry import shape
        rect = shape(geom).minimum_rotated_rectangle
        pts = list(rect.exterior.coords)[:4]
        tl = max(pts, key=lambda p: p[1] - p[0])
        tr = max(pts, key=lambda p: p[1] + p[0])
        bl = min(pts, key=lambda p: p[1] + p[0])
        if len({tl, tr, bl}) < 3:
            return None
        return [[tl[1], tl[0]], [tr[1], tr[0]], [bl[1], bl[0]]]
    except Exception:
        return None


def geom_bounds(geom):
    xs, ys = [], []
    for r in rings(geom):
        xs += [c[0] for c in r]
        ys += [c[1] for c in r]
    return [[min(ys), min(xs)], [max(ys), max(xs)]] if xs else None


def local_raster(out_dir, scene_id):
    """Best local raster for a scene: stacked clip > clipped bands > full-scene bands."""
    p = find_existing(out_dir, scene_id)
    if not p:
        return None
    folder = os.path.dirname(p)
    base = re.sub(r"\.(zip|tar\.gz|tgz|tar)$", "", os.path.basename(p), flags=re.I)
    stack = glob.glob(os.path.join(folder, base + "_clip", "*_stack_clip.tif"))
    if stack:
        return stack[0]
    bands = sorted(glob.glob(os.path.join(folder, base + "_clip", "*_clip.tif")))
    if not bands:
        bands = sorted(os.path.join(r, n) for r, _, fs in os.walk(os.path.join(folder, base))
                       for n in fs if n.lower().endswith(RASTER_EXT))
    if not bands:
        return None
    if len(bands) == 1:
        return bands[0]
    from osgeo import gdal
    vrt = os.path.join(tempfile.gettempdir(), f"{scene_id}_view.vrt")
    gdal.BuildVRT(vrt, bands[:4], separate=True)
    return vrt


@st.cache_data(show_spinner=False, max_entries=32)
def render_raster(path, mtime, max_px=1400):
    """GeoTIFF -> (PNG data URL, [[s,w],[n,e]]). 3+ bands: false colour (NIR, Red, Green)."""
    import base64
    import numpy as np
    from osgeo import gdal
    from PIL import Image
    gdal.UseExceptions()
    src = gdal.Open(path)
    scale = max(src.RasterXSize, src.RasterYSize) / max_px
    w = max(1, int(src.RasterXSize / max(scale, 1)))
    ds = gdal.Warp("", src, format="MEM", dstSRS="EPSG:4326", width=w, height=0,
                   dstNodata=0, resampleAlg="average")
    n = ds.RasterCount
    order = [3, 2, 1] if n >= 3 else [1, 1, 1]          # B4,B3,B2 for a B2-B3-B4 stack
    arrs = [ds.GetRasterBand(b).ReadAsArray().astype("float32") for b in order]
    valid = np.any([a > 0 for a in arrs], axis=0)
    out = []
    for a in arrs:
        v = a[valid]
        lo, hi = (np.percentile(v, 2), np.percentile(v, 98)) if v.size else (0, 1)
        out.append(np.clip((a - lo) / max(hi - lo, 1e-6) * 255, 0, 255).astype("uint8"))
    rgba = np.dstack(out + [(valid * 255).astype("uint8")])
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, "PNG")
    gt = ds.GetGeoTransform()
    w_, n_ = gt[0], gt[3]
    e_, s_ = w_ + gt[1] * ds.RasterXSize, n_ + gt[5] * ds.RasterYSize
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(), [[s_, w_], [n_, e_]]


def compare_viewer(panels, aoi_geom, height, opacity):
    """Synced Leaflet maps (same centre + zoom), one per panel, with a shared cursor."""
    import streamlit.components.v1 as components
    n = len(panels)
    cols = 2 if n in (2, 4) else n
    rows = (n + cols - 1) // cols
    cells = "".join(f'<div class="cell"><div class="ttl">{p["title"]}</div><div id="m{i}" class="map"></div></div>'
                    for i, p in enumerate(panels))
    html = f"""
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/leaflet-imageoverlay-rotated@0.2.1/Leaflet.ImageOverlay.Rotated.js"></script>
<style>
 body{{margin:0;font-family:sans-serif}}
 .grid{{display:grid;grid-template-columns:repeat({cols},1fr);gap:6px}}
 .ttl{{font-size:12px;padding:2px 2px 4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
 .map{{height:{height}px;border-radius:6px}}
 .nodata{{position:absolute;z-index:500;top:6px;right:6px;background:#fffd;padding:2px 6px;border-radius:4px;font-size:11px}}
</style>
<div class="grid">{cells}</div>
<script>
const P={json.dumps(panels)}, AOI={json.dumps(aoi_geom)}, OP={opacity};
const maps=[], cursors=[]; let syncing=false;
const AOIB=L.geoJSON(AOI).getBounds();
P.forEach((p,i)=>{{
  const m=L.map('m'+i,{{zoomSnap:0.25}});
  m.fitBounds(AOIB,{{padding:[10,10]}});   // view must exist before layers are added
  L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}',
              {{maxZoom:19,attribution:'Esri'}}).addTo(m);
  if(p.img){{
    if(p.corners && L.imageOverlay.rotated)
      L.imageOverlay.rotated(p.img,L.latLng(p.corners[0]),L.latLng(p.corners[1]),L.latLng(p.corners[2]),{{opacity:OP}}).addTo(m);
    else if(p.bounds) L.imageOverlay(p.img,p.bounds,{{opacity:OP}}).addTo(m);
  }} else {{
    const d=document.createElement('div'); d.className='nodata'; d.textContent=p.note||'No image';
    document.getElementById('m'+i).appendChild(d);
  }}
  if(p.footprint) L.geoJSON(p.footprint,{{style:{{color:'#3b82f6',weight:1,fill:false}}}}).addTo(m);
  L.geoJSON(AOI,{{style:{{color:'#ef4444',weight:2,fill:false}}}}).addTo(m);
  cursors.push(L.circleMarker(AOIB.getCenter(),{{radius:5,color:'#facc15',weight:2,fill:false,interactive:false}}).addTo(m));
  maps.push(m);
}});
maps.forEach(m=>{{
  m.on('move',()=>{{ if(syncing) return; syncing=true;
    maps.forEach(o=>{{ if(o!==m) o.setView(m.getCenter(),m.getZoom(),{{animate:false}}); }}); syncing=false; }});
  m.on('mousemove',e=>cursors.forEach(c=>c.setLatLng(e.latlng)));
}});
</script>"""
    components.html(html, height=rows * (height + 26) + 10)


# ---------------- Map ----------------
def rings(geom):
    if not geom:
        return []
    if geom["type"] == "Polygon":
        return [geom["coordinates"][0]]
    if geom["type"] == "MultiPolygon":
        return [p[0] for p in geom["coordinates"]]
    return []


def show_map(aoi_geom, feats, bounds):
    fp = [{"path": r, "name": f["id"]} for f in feats for r in rings(f.get("geometry"))]
    aoi = [{"path": r} for r in rings(aoi_geom)]
    layers = [
        pdk.Layer("PolygonLayer", fp, get_polygon="path", get_fill_color=[30, 120, 220, 40],
                  get_line_color=[30, 120, 220, 200], line_width_min_pixels=1, pickable=True),
        pdk.Layer("PolygonLayer", aoi, get_polygon="path", get_fill_color=[230, 60, 30, 50],
                  get_line_color=[230, 60, 30, 255], line_width_min_pixels=2),
    ]
    x0, y0, x1, y1 = bounds
    span = max(x1 - x0, y1 - y0, 0.01)
    zoom = max(1, min(12, 8 - (span ** 0.5) * 2))
    view = pdk.ViewState(longitude=(x0 + x1) / 2, latitude=(y0 + y1) / 2, zoom=zoom)
    st.pydeck_chart(pdk.Deck(layers=layers, initial_view_state=view,
                             tooltip={"text": "{name}"}))


# ================= UI =================
APP_VERSION = "1.1.0"
REPO_URL = "https://github.com/anurag-tm/Bhoonidhi-Downloader"
st.title("🛰️ Bhoonidhi Downloader")
st.caption(f"Search, preview, download and clip Resourcesat LISS-IV data from NRSC Bhoonidhi · "
           f"v{APP_VERSION} · [Help & updates]({REPO_URL}) · Not affiliated with NRSC/ISRO.")

auto_err = try_auto_login()
with st.sidebar:
    st.header("Login")
    if AUTH.token:
        st.success(f"Logged in as {AUTH.user}")
        mins = max(0, int((AUTH.exp - time.time()) / 60))
        st.caption(f"Session refreshes automatically (next in ~{mins} min). "
                   "Reloading the page keeps you logged in.")
        if load_settings().get("remember_user") == AUTH.user:
            st.caption("🔑 Remembered on this PC - logs in automatically when the app starts.")
        if st.button("Logout"):
            do_logout()
            st.rerun()
        if load_settings().get("remember_user") and st.button("Logout and forget me on this PC"):
            forget_login()
            do_logout()
            st.rerun()
    else:
        if auto_err:
            st.warning(f"Automatic login failed: {auto_err}")
        has_kr = _keyring() is not None
        with st.form("login"):
            u = st.text_input("User ID", value=load_settings().get("remember_user") or "")
            p = st.text_input("Password", type="password")
            keep = st.checkbox("Log in again automatically if the session expires", value=True,
                               help="Password is kept in this app's memory while it runs.")
            remember = st.checkbox("Remember me on this PC", value=False, disabled=not has_kr,
                                   help="Saves the password in Windows Credential Manager, so the "
                                        "app logs you in by itself next time. Use only on your own PC.")
            if st.form_submit_button("Login"):
                e = do_login(u.strip(), p)
                if e:
                    st.error(e)
                else:
                    AUTH.pwd_keep = p if (keep or remember) else None
                    if remember:
                        remember_login(u.strip(), p)
                    st.rerun()
        if not has_kr:
            st.caption("To enable 'Remember me': conda install -c conda-forge keyring "
                       "(then restart the app).")
    st.caption("Log out when done - Bhoonidhi limits active sessions. "
               "Login is limited to 20 requests/hour.")

# --- AOI ---
st.subheader("1. Area of interest")
_modes = ["Point + size", "Bounding box", "Shapefile"]
mode = st.radio("Define AOI by", _modes, horizontal=True,
                index=_modes.index(CFG.get("aoi_mode")) if CFG.get("aoi_mode") in _modes else 0)
aoi_geom, aoi_bounds = None, None
if mode == "Point + size":
    c1, c2, c3 = st.columns([3, 1, 1])
    txt = c1.text_input("Latitude, Longitude", value=CFG.get("latlon", "34.1667, 77.5833"))
    shp_kind = c2.radio("Clip shape", ["Circle", "Square"], horizontal=True,
                        index=1 if CFG.get("clip_shape") == "Square" else 0)
    size_lbl = "Radius (km)" if shp_kind == "Circle" else "Side length (km)"
    rad = c3.number_input(size_lbl, min_value=0.5, max_value=400.0,
                          value=float(CFG.get("clip_size", 20.0)), step=1.0)
    save_settings(latlon=txt, clip_shape=shp_kind, clip_size=float(rad))
    area = 3.141593 * rad ** 2 if shp_kind == "Circle" else rad ** 2
    st.caption(f"Clip area: about {area:,.0f} km²"
               + (f" (circle, {2 * rad:g} km across)" if shp_kind == "Circle"
                  else f" (square, {rad:g} km x {rad:g} km)"))
    try:
        lat, lon = [float(v) for v in re.split(r"[,\s]+", txt.strip()) if v][:2]
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError
        aoi_geom, aoi_bounds = point_aoi(lat, lon, rad, shp_kind)
        st.caption("Search box: " + ", ".join(f"{v:.4f}" for v in aoi_bounds))
    except ImportError:
        st.error("pyproj/shapely missing. Run: conda install -c conda-forge geopandas")
    except ValueError:
        st.error("Enter latitude first, then longitude, e.g. 28.1722, 89.8478")
elif mode == "Bounding box":
    c1, c2, c3, c4 = st.columns(4)
    _bb = CFG.get("bbox") or [76.0, 33.5, 76.6, 34.0]
    x0 = c1.number_input("Min longitude", value=float(_bb[0]), format="%.5f")
    y0 = c2.number_input("Min latitude", value=float(_bb[1]), format="%.5f")
    x1 = c3.number_input("Max longitude", value=float(_bb[2]), format="%.5f")
    y1 = c4.number_input("Max latitude", value=float(_bb[3]), format="%.5f")
    save_settings(bbox=[x0, y0, x1, y1])
    if x0 >= x1 or y0 >= y1:
        st.error("Min values must be smaller than max values.")
    else:
        aoi_bounds = (x0, y0, x1, y1)
        aoi_geom = bbox_geojson(aoi_bounds)
else:
    up = st.file_uploader("Upload a zipped shapefile, or select .shp + .shx + .dbf + .prj together "
                          "(GeoJSON / GPKG also work)",
                          type=["zip", "shp", "shx", "dbf", "prj", "cpg", "geojson", "json", "gpkg"],
                          accept_multiple_files=True)
    last_aoi = _load_json(LAST_AOI_FILE)
    if up:
        try:
            aoi_geom, aoi_bounds, note = read_shapefile(up)
            if note:
                st.warning(note)
            st.success("AOI bounds: " + ", ".join(f"{v:.4f}" for v in aoi_bounds))
            name = ", ".join(f.name for f in up)
            if not last_aoi or last_aoi.get("geometry") != aoi_geom:
                _save_json(LAST_AOI_FILE, {"name": name, "geometry": aoi_geom,
                                           "bounds": list(aoi_bounds)})
        except ImportError:
            st.error("geopandas not installed. Run: conda install -c conda-forge geopandas")
        except Exception as e:
            st.error(f"Could not read file: {e}")
    elif last_aoi and last_aoi.get("geometry"):
        aoi_geom, aoi_bounds = last_aoi["geometry"], tuple(last_aoi["bounds"])
        c1, c2 = st.columns([4, 1])
        c1.info(f"Using your last shapefile: **{last_aoi.get('name', 'saved area')}** "
                f"(bounds {', '.join(f'{v:.4f}' for v in aoi_bounds)}). "
                "Upload another file to replace it.")
        if c2.button("Forget it"):
            try:
                os.remove(LAST_AOI_FILE)
            except OSError:
                pass
            st.rerun()
save_settings(aoi_mode=mode)

# --- Filters ---
st.subheader("2. Filters")
c1, c2, c3 = st.columns([1, 1, 2])
d_start = c1.date_input("Start date", value=_cfg_date("d_start", date(2023, 7, 1)), key="d_start",
                        min_value=date(2011, 1, 1), max_value=date.today(), format="DD/MM/YYYY")
d_end = c2.date_input("End date", value=_cfg_date("d_end", date(2023, 10, 31)), key="d_end",
                      min_value=date(2011, 1, 1), max_value=date.today(), format="DD/MM/YYYY")
dates_ok = d_start <= d_end
if not dates_ok:
    st.error("Start date must be on or before the end date.")
cols = c3.multiselect("Collections", COLLECTIONS,
                      default=[c for c in CFG.get("collections", DEFAULT_COLS) if c in COLLECTIONS])
c1, c2 = st.columns(2)
max_cloud = c1.slider("Max cloud cover (%)", 0, 100, int(CFG.get("max_cloud", 20)))
keep_unknown = c2.checkbox("Keep scenes with no cloud value", value=CFG.get("keep_unknown", True))
online_only = c2.checkbox("Online scenes only (API can only download these)",
                          value=CFG.get("online_only", True))
get_cloud_info = c1.checkbox("Read cloud cover for each scene (.meta file, else quick-look estimate)",
                             value=CFG.get("read_cloud", True), help="Bhoonidhi's search results have no cloud field, so the "
                             "app reads each scene's .meta file. If that has no cloud value, it "
                             "estimates from the quick-look (counts snow too).")

save_settings(d_start=d_start.isoformat(), d_end=d_end.isoformat(), collections=cols,
              max_cloud=int(max_cloud), keep_unknown=bool(keep_unknown),
              online_only=bool(online_only), read_cloud=bool(get_cloud_info))

ready = AUTH.token and aoi_geom and dates_ok and cols
if st.button("🔍 Search", type="primary", disabled=not ready):
    # Bhoonidhi refuses searches longer than 366 days -> search in yearly pieces and merge
    from datetime import timedelta
    chunks, a = [], d_start
    while a <= d_end:
        b = min(a + timedelta(days=CHUNK_DAYS - 1), d_end)
        chunks.append((a, b))
        a = b + timedelta(days=1)
    feats, failed = [], []
    prog = st.progress(0.0, text="Searching catalogue...")
    for i, (a, b) in enumerate(chunks, 1):
        body = {
            "collections": cols,
            "datetime": f"{a:%Y-%m-%d}T00:00:00Z/{b:%Y-%m-%d}T23:59:59Z",
            "bbox": [round(v, 6) for v in aoi_bounds],
            "limit": 500,
        }
        if online_only:
            body["filter"] = {"args": [{"property": "Online"}, "Y"], "op": "eq"}
            body["filter-lang"] = "cql2-json"
        prog.progress((i - 1) / len(chunks),
                      text=f"Searching {a:%d/%m/%Y} - {b:%d/%m/%Y} ({i}/{len(chunks)})...")
        try:
            feats += search(body)
        except Exception as e:
            failed.append(f"{a:%d/%m/%Y}-{b:%d/%m/%Y}: {e}")
        time.sleep(0.4)                       # search limit: 3 requests/second
    prog.empty()
    seen, uniq = set(), []
    for f in feats:
        if f["id"] not in seen:
            seen.add(f["id"])
            uniq.append(f)
    feats = uniq
    try:
        from shapely.geometry import shape
        aoi_shp = shape(aoi_geom)
        feats = [f for f in feats
                 if not f.get("geometry") or shape(f["geometry"]).intersects(aoi_shp)]
    except Exception:
        pass
    if get_cloud_info and feats and ensure_token():
        cprog = st.progress(0.0, text="Reading cloud info...")
        add_cloud_info(feats, auth_header(), cprog)
        cprog.empty()
    ss.feats = feats
    ss.aoi = (aoi_geom, aoi_bounds)
    ss.restored_search = None
    _save_json(LAST_SEARCH_FILE, {"time": time.strftime("%d/%m/%Y %H:%M"), "feats": feats,
                                  "aoi": [aoi_geom, list(aoi_bounds)]})
    if failed:
        st.error("Some periods could not be searched:\n\n" + "\n\n".join(failed))
    if len(chunks) > 1:
        st.caption(f"Searched {len(chunks)} periods of up to {CHUNK_DAYS} days each "
                   "(Bhoonidhi allows max 366 days per search).")
    if not feats and not failed:
        st.warning("No scenes found. Try a wider date range, a bigger area, "
                   "or untick 'Online scenes only'.")
if not AUTH.token:
    st.info("Log in from the sidebar to search.")
if ss.feats and ss.get("restored_search"):
    st.caption(f"↩️ Showing your last search from {ss.restored_search} (restored automatically). "
               "Click Search to refresh it.")

# --- Results ---
if ss.feats:
    num_keys = numeric_fields(ss.feats)
    guess = guess_cloud_field(num_keys)
    opts = ["(no cloud field)"] + num_keys
    cloud_field = st.selectbox(
        "Cloud cover field (from Bhoonidhi's scene info)", opts,
        index=opts.index(guess) if guess else 0,
        help="Picked automatically if a field name contains 'cloud'. "
             "Change it if the Cloud % column looks wrong.")
    if cloud_field == EST_FIELD:
        st.info("Cloud % is an estimate from the quick-look: it counts bright white pixels, "
                "so snow and glaciers are counted too. Check doubtful scenes in the Compare viewer.")
    elif cloud_field and cloud_field.startswith("meta."):
        st.caption(f"Cloud % read from each scene's .meta file ({cloud_field[5:]}).")
    if cloud_field == "(no cloud field)":
        cloud_field = None
        st.warning("No cloud cover field found in these scenes, so the cloud slider can't filter. "
                   "Open 'Raw properties' at the bottom to see which fields Bhoonidhi sent.")
    try:
        from shapely.geometry import shape as _shape
        _aoi_shp = _shape(ss.aoi[0])
    except Exception:
        _aoi_shp = None
    rows = []
    for f in ss.feats:
        p = f.get("properties", {})
        cc = get_cloud(p, cloud_field)
        if cc is None and not keep_unknown:
            continue
        if cc is not None and cc > max_cloud:
            continue
        rows.append({"Select": False, "Date": str(p.get("datetime", ""))[:10],
                     "Location": location_key(f),
                     "AOI covered %": aoi_cover_pct(f, _aoi_shp) if _aoi_shp is not None else None,
                     "Cloud %": cc, "Collection": f["collection"],
                     "Online": p.get("Online", ""), "ID": f["id"]})
    shown_ids = {r["ID"] for r in rows}
    shown = [f for f in ss.feats if f["id"] in shown_ids]
    by_id_all = {f["id"]: f for f in shown}

    st.subheader(f"3. Results - {len(rows)} of {len(ss.feats)} scenes match")
    if rows:
        show_map(ss.aoi[0], shown, ss.aoi[1])
        df = pd.DataFrame(rows).sort_values(["Location", "Date"], ascending=[True, False])
        if st.checkbox("Select all"):
            df["Select"] = True
        edited = st.data_editor(df, hide_index=True, use_container_width=True,
                                disabled=["Date", "Location", "AOI covered %", "Cloud %", "Collection", "Online", "ID"])
        chosen = edited[edited["Select"]]["ID"].tolist()

        st.subheader("4. Compare scenes")
        out_dir = st.text_input("Download folder",
                                value=CFG.get("out_dir", os.path.join(os.getcwd(), "liss4_downloads")))
        by_row = {r["ID"]: r for r in rows}
        # group scenes by location so every window shows the same ground
        loc_stats = (df.groupby("Location")
                       .agg(n=("ID", "size"), cover=("AOI covered %", "max"))
                       .reset_index().sort_values(["cover", "n"], ascending=False))
        loc_list = list(loc_stats["Location"])
        loc_lbl = {r.Location: f"{r.Location} - {r.n} scene(s), covers {r.cover:.0f}% of AOI"
                   if pd.notna(r.cover) else f"{r.Location} - {r.n} scene(s)"
                   for r in loc_stats.itertuples()}
        ticked_locs = [by_row[i]["Location"] for i in chosen if i in by_row]
        loc_default = max(set(ticked_locs), key=ticked_locs.count) if ticked_locs else loc_list[0]
        c1, c2 = st.columns([2, 1])
        mix = c2.checkbox("Allow different locations", value=False,
                          help="Off = only scenes of the same path/row/subscene, so all windows "
                               "show the same area. Your AOI is the same in every window anyway.")
        if mix:
            pool = list(df["ID"])
        else:
            loc = c1.selectbox("Location (same path / row / subscene = same area)", loc_list,
                               index=loc_list.index(loc_default), format_func=lambda l: loc_lbl[l])
            pool = list(df[df["Location"] == loc]["ID"])
        default_cmp = [i for i in chosen if i in pool][:4] or pool[:4]
        cmp_ids = st.multiselect("Pick 2-4 dates to compare", pool, default=default_cmp,
                                 max_selections=4, key=f"cmp_{'all' if mix else loc}",
                                 format_func=lambda i: f'{by_row[i]["Date"]} | cloud '
                                 + (f'{by_row[i]["Cloud %"]:.0f}%' if by_row[i]["Cloud %"] is not None else "?")
                                 + ("" if not mix else f' | {by_row[i]["Location"]}'))
        if len(pool) < 2 and not mix:
            st.caption("Only one scene at this location in your date range - widen the dates "
                       "to compare it with other dates.")
        c1, c2, c3 = st.columns(3)
        src_kind = c1.radio("Show", ["Quick-look (before download)", "Downloaded / clipped image"])
        fit_rot = c1.checkbox("Quick-look looks tilted: fit it to scene corners", value=False)
        vh = c2.slider("Window height (px)", 250, 700, 380, 10)
        op = c3.slider("Image opacity", 0.2, 1.0, 1.0, 0.05)
        if st.toggle("Open comparison viewer", value=False, disabled=len(cmp_ids) < 1):
            by_id = {f["id"]: f for f in shown}
            panels, missing = [], []
            with st.spinner("Loading images..."):
                for sid in cmp_ids:
                    f, r = by_id[sid], by_row[sid]
                    cc = "" if r["Cloud %"] is None else f' | cloud {r["Cloud %"]:.0f}%'
                    panel = {"title": f'{r["Date"]} | {r["Location"]}{cc}',
                             "footprint": f.get("geometry"), "img": None, "bounds": None,
                             "corners": None, "note": ""}
                    if src_kind.startswith("Quick"):
                        urls = find_quicklooks(f)
                        if urls and ensure_token():
                            for u in urls:
                                panel["img"] = fetch_image(u, auth_header())
                                if panel["img"]:
                                    break
                        panel["bounds"] = geom_bounds(f.get("geometry"))
                        panel["corners"] = scene_corners(f.get("geometry")) if fit_rot else None
                        if not panel["img"]:
                            panel["note"] = "No quick-look from Bhoonidhi"
                            missing.append(sid)
                    else:
                        try:
                            pth = local_raster(out_dir, sid)
                            if pth:
                                panel["img"], panel["bounds"] = render_raster(pth, os.path.getmtime(pth))
                            else:
                                panel["note"] = "Not downloaded yet"
                                missing.append(sid)
                        except ImportError:
                            panel["note"] = "Needs GDAL + Pillow"
                        except Exception as e:
                            panel["note"] = f"Could not read: {e}"[:80]
                    panels.append(panel)
            compare_viewer(panels, ss.aoi[0], vh, op)
            st.caption("Maps move together: pan or zoom any window. Yellow ring = same point in all "
                       "windows. Red = your AOI, blue = scene footprint. Quick-looks are low-resolution "
                       "and placed approximately; downloaded images are shown as false colour "
                       "(NIR-Red-Green) and placed exactly.")
            if missing and src_kind.startswith("Quick"):
                st.info("Bhoonidhi gave no quick-look for some scenes. Open 'Raw properties' below "
                        "to check the 'assets' links, or report it on the GitHub Issues page.")

        st.subheader("5. Download & clip")
        has_gdal = gdal_available()
        c1, c2, c3, c4 = st.columns(4)
        do_clip = c1.checkbox("Unzip and clip to AOI (GDAL)", value=has_gdal and CFG.get("clip", True),
                              disabled=not has_gdal)
        do_stack = c2.checkbox("Stack clipped bands into one GeoTIFF", value=CFG.get("stack", True),
                               disabled=not do_clip)
        keep_full = c3.checkbox("Keep full scene after clipping", value=CFG.get("keep_full", True),
                                disabled=not do_clip)
        n_par = c4.number_input("Parallel downloads", min_value=1, max_value=5,
                                value=int(CFG.get("parallel", 4)), step=1,
                                help="How many scenes download at the same time. "
                                     "Bhoonidhi allows max 5. Use 1-2 on slow internet.")
        save_settings(out_dir=out_dir, stack=bool(do_stack), keep_full=bool(keep_full),
                      parallel=int(n_par), **({"clip": bool(do_clip)} if has_gdal else {}))
        if not has_gdal:
            st.caption("Clipping needs GDAL: conda install -c conda-forge gdal (then restart the app).")
        st.caption("The Bhoonidhi API only serves full scenes, so the full scene is downloaded "
                   "first and clipped on your PC. Your choices here are remembered next time.")

        if st.button(f"⬇️ Download {len(chosen)} scene(s)", disabled=not chosen):
            run_downloads([by_id_all[sid] for sid in chosen], out_dir, int(n_par),
                          do_clip, do_stack, keep_full, ss.aoi[0])
    else:
        st.warning("No scenes left after the cloud filter. Raise the cloud limit "
                   "or tick 'Keep scenes with no cloud value'.")

    with st.expander("Raw properties of first scene (to check field names)"):
        f0 = ss.feats[0]
        st.json({"properties": f0.get("properties", {}), "assets": f0.get("assets", {}),
                 "links": f0.get("links", [])})
