"""
Shared helpers for the Copernicus and USGS pages:
settings, area-of-interest input, resumable downloads, a live progress table,
and GDAL processing (unpack, clip, colour composites, spectral indices).

The Bhoonidhi page (app.py) does not use this file, so it keeps working unchanged.
"""
import glob
import io
import json
import math
import os
import re
import shutil
import tarfile
import tempfile
import threading
import time
import zipfile

import requests
import streamlit as st

APP_DIR = os.path.dirname(os.path.abspath(__file__))
RASTER_EXT = (".tif", ".tiff", ".jp2", ".img")
ARCHIVE_EXT = (".zip", ".tar", ".tar.gz", ".tgz")


# ============================================================ settings
class Settings:
    """Small JSON settings file per page (e.g. copernicus_settings.json)."""

    def __init__(self, name):
        self.path = os.path.join(APP_DIR, f"{name}_settings.json")
        self.lock = threading.Lock()
        self.data = self._load()

    def _load(self):
        try:
            with open(self.path) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, **kw):
        with self.lock:
            if all(self.data.get(k) == v for k, v in kw.items()):
                return
            self.data.update(kw)
            try:
                tmp = self.path + ".tmp"
                with open(tmp, "w") as fh:
                    json.dump(self.data, fh, indent=1)
                os.replace(tmp, self.path)
            except (OSError, TypeError, ValueError):
                pass


def save_json(path, obj):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(obj, fh)
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        pass


def load_json(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


# ============================================================ password store
KEYRING_SERVICE = "bhoonidhi-downloader"


def _keyring():
    try:
        import keyring
        keyring.get_keyring()
        return keyring
    except Exception:
        return None


def has_keyring():
    return _keyring() is not None


def secret_save(name, value):
    kr = _keyring()
    if kr is None:
        return False
    try:
        kr.set_password(KEYRING_SERVICE, name, value)
        return True
    except Exception:
        return False


def secret_load(name):
    kr = _keyring()
    if kr is None:
        return None
    try:
        return kr.get_password(KEYRING_SERVICE, name)
    except Exception:
        return None


def secret_delete(name):
    kr = _keyring()
    if kr is not None:
        try:
            kr.delete_password(KEYRING_SERVICE, name)
        except Exception:
            pass


# ============================================================ area of interest
def reproject(geom, fn):
    """Apply a pyproj transform function to a shapely geometry (Shapely 2 way)."""
    import shapely
    return shapely.transform(geom, lambda x, y: fn(x, y), interleaved=False)


def point_aoi(lat, lon, size_km, kind="Circle"):
    """Circle (size = radius) or square (size = side), true km on the ground."""
    from pyproj import CRS, Transformer
    from shapely.geometry import Point, Polygon, mapping

    aeqd = CRS.from_proj4(f"+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m")
    to_ll = Transformer.from_crs(aeqd, "EPSG:4326", always_xy=True).transform
    if kind == "Square":
        h = size_km * 1000 / 2
        local = Polygon([(-h, -h), (h, -h), (h, h), (-h, h)]).segmentize(h / 10)
    else:
        local = Point(0, 0).buffer(size_km * 1000, 64)
    geom = reproject(local, to_ll)
    return json.loads(json.dumps(mapping(geom))), geom.bounds


def bbox_geojson(b):
    x0, y0, x1, y1 = b
    return {"type": "Polygon",
            "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}


def read_vector(files):
    """Uploaded shapefile (.zip or parts) / GeoJSON / GPKG -> (WGS84 geojson, bounds, note)."""
    import geopandas as gpd
    from shapely.geometry import mapping

    tmp = tempfile.mkdtemp()
    for f in files:
        data = f.getvalue()
        if f.name.lower().endswith(".zip"):
            zipfile.ZipFile(io.BytesIO(data)).extractall(tmp)
        else:
            with open(os.path.join(tmp, os.path.basename(f.name)), "wb") as fh:
                fh.write(data)
    found = [os.path.join(r, n) for r, _, fs in os.walk(tmp) for n in fs
             if n.lower().endswith((".shp", ".geojson", ".json", ".gpkg"))]
    if not found:
        raise ValueError("No .shp / .geojson / .gpkg found in upload.")
    gdf = gpd.read_file(found[0])
    note = ""
    if gdf.crs is None:
        note = "File has no projection (.prj) - assumed WGS84 (EPSG:4326)."
        gdf = gdf.set_crs(4326)
    else:
        gdf = gdf.to_crs(4326)
    geom = gdf.union_all() if hasattr(gdf, "union_all") else gdf.unary_union
    if geom.geom_type not in ("Polygon", "MultiPolygon"):
        geom = geom.buffer(0.01) if geom.geom_type == "Point" else geom.convex_hull
    tol = 0.0005
    while len(json.dumps(mapping(geom))) > 20000 and tol < 0.1:
        geom = geom.simplify(tol, preserve_topology=True)
        tol *= 2
    return json.loads(json.dumps(mapping(geom))), geom.bounds, note


def aoi_widget(prefix, cfg):
    """Area-of-interest input used on every page. Remembers choices and the last
    uploaded file. Returns (geojson, bounds) or (None, None)."""
    import re as _re
    st.subheader("1. Area of interest")
    modes = ["Point + size", "Bounding box", "Shapefile / GeoJSON"]
    mode = st.radio("Define the area by", modes, horizontal=True, key=f"{prefix}_aoi_mode",
                    index=modes.index(cfg.get("aoi_mode")) if cfg.get("aoi_mode") in modes else 0)
    cfg.set(aoi_mode=mode)
    geom = bounds = None
    if mode == "Point + size":
        c1, c2, c3 = st.columns([3, 1, 1])
        txt = c1.text_input("Latitude, Longitude", value=cfg.get("latlon", "34.1667, 77.5833"),
                            key=f"{prefix}_latlon")
        kind = c2.radio("Shape", ["Circle", "Square"], horizontal=True, key=f"{prefix}_shape",
                        index=1 if cfg.get("shape") == "Square" else 0)
        lbl = "Radius (km)" if kind == "Circle" else "Side length (km)"
        size = c3.number_input(lbl, 0.5, 400.0, float(cfg.get("size", 20.0)), 1.0,
                               key=f"{prefix}_size")
        cfg.set(latlon=txt, shape=kind, size=float(size))
        area = math.pi * size ** 2 if kind == "Circle" else size ** 2
        try:
            lat, lon = [float(v) for v in _re.split(r"[,\s]+", txt.strip()) if v][:2]
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError
            geom, bounds = point_aoi(lat, lon, size, kind)
            st.caption(f"Area: about {area:,.0f} km²")
        except ValueError:
            st.error("Enter latitude first, then longitude, e.g. 34.1667, 77.5833")
    elif mode == "Bounding box":
        bb = cfg.get("bbox") or [77.4, 34.0, 77.8, 34.3]
        c1, c2, c3, c4 = st.columns(4)
        x0 = c1.number_input("Min longitude", value=float(bb[0]), format="%.5f", key=f"{prefix}_x0")
        y0 = c2.number_input("Min latitude", value=float(bb[1]), format="%.5f", key=f"{prefix}_y0")
        x1 = c3.number_input("Max longitude", value=float(bb[2]), format="%.5f", key=f"{prefix}_x1")
        y1 = c4.number_input("Max latitude", value=float(bb[3]), format="%.5f", key=f"{prefix}_y1")
        cfg.set(bbox=[x0, y0, x1, y1])
        if x0 >= x1 or y0 >= y1:
            st.error("Min values must be smaller than max values.")
        else:
            bounds = (x0, y0, x1, y1)
            geom = bbox_geojson(bounds)
    else:
        last_file = os.path.join(APP_DIR, f"{prefix}_last_aoi.json")
        up = st.file_uploader("Upload a zipped shapefile, or .shp + .shx + .dbf + .prj together "
                              "(GeoJSON / GPKG also work)",
                              type=["zip", "shp", "shx", "dbf", "prj", "cpg", "geojson", "json", "gpkg"],
                              accept_multiple_files=True, key=f"{prefix}_upload")
        last = load_json(last_file)
        if up:
            try:
                geom, bounds, note = read_vector(up)
                if note:
                    st.warning(note)
                st.success("Area bounds: " + ", ".join(f"{v:.4f}" for v in bounds))
                save_json(last_file, {"name": ", ".join(f.name for f in up), "geometry": geom,
                                      "bounds": list(bounds)})
            except Exception as e:
                st.error(f"Could not read file: {e}")
        elif last and last.get("geometry"):
            geom, bounds = last["geometry"], tuple(last["bounds"])
            c1, c2 = st.columns([4, 1])
            c1.info(f"Using your last file: **{last.get('name', 'saved area')}**. "
                    "Upload another to replace it.")
            if c2.button("Forget it", key=f"{prefix}_forget"):
                try:
                    os.remove(last_file)
                except OSError:
                    pass
                st.rerun()
    return geom, bounds


def utm_epsg(lon, lat):
    zone = int((lon + 180) // 6) + 1
    return (32600 if lat >= 0 else 32700) + zone


def cover_pct(footprint, aoi_geom):
    """% of the AOI inside a footprint (both GeoJSON, WGS84)."""
    try:
        from shapely.geometry import shape
        a = shape(aoi_geom)
        return round(shape(footprint).intersection(a).area / a.area * 100)
    except Exception:
        return None


# ============================================================ downloads
class Sink:
    """Collects progress from worker threads; the page redraws it."""

    def __init__(self, state, key):
        self.d, self.k = state, key

    def progress(self, v):
        self.d[self.k]["pct"] = float(min(max(v, 0.0), 1.0))

    def write(self, msg):
        self.d[self.k]["msg"] = str(msg)


def download_file(url, out_dir, name_hint, headers_fn=None, sink=None, max_tries=8,
                  follow_auth=True, session=None):
    """Resumable download (HTTP Range) with retries and token refresh.
    headers_fn(force) -> dict of auth headers (force=True after a 401).
    Returns (ok, message, path)."""
    os.makedirs(out_dir, exist_ok=True)
    http = session or requests
    part = os.path.join(out_dir, name_hint + ".part")
    last_err = "Failed after retries."
    force = False
    for attempt in range(1, max_tries + 1):
        hdr = dict(headers_fn(force) if headers_fn else {})
        force = False
        start = os.path.getsize(part) if os.path.exists(part) else 0
        if start:
            hdr["Range"] = f"bytes={start}-"
            if sink:
                sink.write(f"resuming from {start / 1e6:.1f} MB...")
        try:
            target = url
            for _ in range(6):   # follow redirects ourselves so the token is kept
                r = http.get(target, headers=hdr, stream=True, timeout=(30, 300),
                             allow_redirects=not follow_auth)
                if follow_auth and r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                    target = requests.compat.urljoin(target, r.headers["Location"])
                    r.close()
                    continue
                break
            with r:
                if r.status_code == 401 and headers_fn:
                    force = True
                    last_err = "Session expired"
                    if sink:
                        sink.write("session expired, logging in again...")
                    continue
                if r.status_code == 416:
                    os.remove(part)
                    continue
                if r.status_code in (429, 500, 502, 503, 504):
                    wait = int(r.headers.get("Retry-After", 30)) if str(r.headers.get("Retry-After", "")).isdigit() else 30
                    last_err = f"HTTP {r.status_code}"
                    if sink:
                        sink.write(f"server busy (HTTP {r.status_code}), retry {attempt}/{max_tries} in {wait} s")
                    time.sleep(min(wait, 120))
                    continue
                if r.status_code not in (200, 206):
                    return False, f"HTTP {r.status_code}: {r.text[:200]}", None
                if r.status_code == 206:
                    mode, done = "ab", start
                    m = re.search(r"/(\d+)", r.headers.get("Content-Range", ""))
                    total = int(m.group(1)) if m else 0
                else:
                    mode, done = "wb", 0
                    total = int(r.headers.get("Content-Length", 0) or 0)
                cd = r.headers.get("Content-Disposition", "")
                m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', cd)
                name = os.path.basename(m.group(1)) if m else name_hint
                with open(part, mode) as fh:
                    for chunk in r.iter_content(1024 * 1024):
                        if not chunk:
                            continue
                        fh.write(chunk)
                        done += len(chunk)
                        if sink:
                            if total:
                                sink.progress(done / total)
                            sink.write(f"{done / 1e6:.1f} MB" + (f" / {total / 1e6:.1f} MB" if total else ""))
                if total and done < total:
                    raise requests.ConnectionError(f"stopped at {done / 1e6:.1f} MB")
            path = os.path.join(out_dir, name)
            os.replace(part, path)
            if sink:
                sink.progress(1.0)
            return True, f"Saved: {path}", path
        except requests.RequestException as e:
            last_err = f"Connection lost ({e})"
            if sink:
                sink.write(f"connection lost - retrying in 15 s ({attempt}/{max_tries})")
            time.sleep(15)
    return False, last_err + " - partial file kept; run again to resume.", None


INDEX_LOCK = threading.Lock()


def index_get(folder, key):
    """Path recorded for a downloaded product (ZIP, or its folder after the ZIP was deleted)."""
    idx = load_json(os.path.join(folder, "_downloaded.json")) or {}
    p = idx.get(key)
    return p if p and os.path.exists(p) else None


def index_set(folder, key, path):
    with INDEX_LOCK:
        f = os.path.join(folder, "_downloaded.json")
        idx = load_json(f) or {}
        idx[key] = path
        save_json(f, idx)


def safe_name(text, n=40):
    """'NDSI – snow & glaciers' -> 'NDSI_snow_glaciers'."""
    t = re.sub(r"[^0-9A-Za-z]+", "_", text).strip("_")
    return t[:n].rstrip("_")


ICONS = {"waiting": "⏳", "running": "⬇️", "processing": "⚙️", "done": "✅", "failed": "❌"}


def run_jobs(jobs, workers, title):
    """jobs: list of (key, fn) where fn(sink) -> (ok, message). Shows a live table.
    Returns {key: (ok, message)}."""
    from concurrent.futures import ThreadPoolExecutor
    if not jobs:
        return {}
    state = {k: {"status": "waiting", "pct": 0.0, "msg": "waiting..."} for k, _ in jobs}
    results = {}
    overall = st.progress(0.0, text=title)
    rows = {}
    for k, _ in jobs:
        c1, c2 = st.columns([2, 3])
        rows[k] = (c1.empty(), c2.progress(0.0), c2.empty())

    def wrap(k, fn):
        state[k]["status"] = "running"
        try:
            ok, msg = fn(Sink(state, k))
        except Exception as e:
            ok, msg = False, f"Error: {e}"
        state[k].update(status="done" if ok else "failed", msg=msg, pct=1.0 if ok else state[k]["pct"])
        results[k] = (ok, msg)

    def draw():
        for k, (head, bar, line) in rows.items():
            s = state[k]
            head.markdown(f"{ICONS[s['status']]} `{k}`")
            bar.progress(s["pct"])
            line.caption(s["msg"][:300])
        fin = sum(s["status"] in ("done", "failed") for s in state.values())
        overall.progress(fin / len(jobs), text=f"{title}: {fin}/{len(jobs)} finished")

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = [ex.submit(wrap, k, fn) for k, fn in jobs]
        while not all(f.done() for f in futs):
            draw()
            time.sleep(0.7)
    draw()
    return results


# ============================================================ GDAL processing
def gdal_ok():
    try:
        from osgeo import gdal  # noqa: F401
        return True
    except ImportError:
        return False


def jp2_ok():
    """Sentinel-2 bands are JPEG2000; newer conda GDAL needs the libgdal-jp2openjpeg plugin."""
    try:
        from osgeo import gdal
        return gdal.GetDriverByName("JP2OpenJPEG") is not None
    except ImportError:
        return False


def unpack(path):
    """Extract .zip / .tar / .tar.gz next to the file (once). Returns the folder."""
    folder = os.path.dirname(path)
    base = re.sub(r"\.(zip|tar\.gz|tgz|tar)$", "", os.path.basename(path), flags=re.I)
    out = os.path.join(folder, base)
    if os.path.isdir(out) and os.listdir(out):
        return out
    os.makedirs(out, exist_ok=True)
    low = path.lower()
    if low.endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            z.extractall(out)
    elif low.endswith((".tar", ".tar.gz", ".tgz")):
        with tarfile.open(path) as t:
            try:
                t.extractall(out, filter="data")
            except TypeError:          # Python < 3.12
                t.extractall(out)
    else:
        raise ValueError("Not an archive")
    return out


def list_rasters(folder):
    return sorted(os.path.join(r, n) for r, _, fs in os.walk(folder) for n in fs
                  if n.lower().endswith(RASTER_EXT) and "_clip" not in n.lower())


def write_aoi(aoi_geom, folder):
    path = os.path.join(folder, "aoi.geojson")
    with open(path, "w") as fh:
        json.dump({"type": "FeatureCollection",
                   "features": [{"type": "Feature", "properties": {}, "geometry": aoi_geom}]}, fh)
    return path


def clip_raster(src, out, cutline, nodata=0):
    """Clip to the AOI. Returns True, or False if the AOI does not overlap."""
    from osgeo import gdal
    gdal.UseExceptions()
    ds = None
    try:
        ds = gdal.Warp(out, src, cutlineDSName=cutline, cropToCutline=True, dstNodata=nodata,
                       creationOptions=["COMPRESS=DEFLATE", "TILED=YES", "BIGTIFF=IF_SAFER"])
        ds.GetRasterBand(1).ComputeRasterMinMax(False)     # raises when nothing valid
        ds = None
        return True
    except RuntimeError:
        ds = None
        if os.path.exists(out):
            os.remove(out)
        return False


def _band_reader(paths):
    """Put several single-band files on one grid (finest resolution) and return
    (vrt_dataset, {name: band_index})."""
    from osgeo import gdal
    names = list(paths)
    vrt = gdal.BuildVRT("", [paths[n] for n in names], separate=True, resolution="highest")
    return vrt, {n: i + 1 for i, n in enumerate(names)}


def make_rgb(paths, recipe, out_path, scale=None, block_rows=1024):
    """8-bit RGB GeoTIFF. recipe = (r, g, b) band names. 2-98 % stretch, 0 = no data."""
    import numpy as np
    from osgeo import gdal
    gdal.UseExceptions()
    need = {n: paths[n] for n in recipe}
    vrt, idx = _band_reader(need)
    xs, ys = vrt.RasterXSize, vrt.RasterYSize
    f = max(1.0, max(xs, ys) / 2000.0)
    bx, by = max(1, int(xs / f)), max(1, int(ys / f))
    small = {n: vrt.GetRasterBand(i).ReadAsArray(buf_xsize=bx, buf_ysize=by).astype("float32")
             for n, i in idx.items()}
    valid = np.all([small[n] > 0 for n in recipe], axis=0)
    if valid.sum() < 50:
        raise RuntimeError("no valid pixels")
    lims = [(float(np.percentile(small[n][valid], 2)), float(np.percentile(small[n][valid], 98)))
            for n in recipe]
    out = gdal.GetDriverByName("GTiff").Create(
        out_path, xs, ys, 3, gdal.GDT_Byte,
        ["COMPRESS=DEFLATE", "TILED=YES", "PHOTOMETRIC=RGB", "BIGTIFF=IF_SAFER"])
    out.SetGeoTransform(vrt.GetGeoTransform())
    out.SetProjection(vrt.GetProjection())
    for i in range(3):
        out.GetRasterBand(i + 1).SetNoDataValue(0)
    for y in range(0, ys, block_rows):
        rows = min(block_rows, ys - y)
        arr = {n: vrt.GetRasterBand(i).ReadAsArray(0, y, xs, rows).astype("float32")
               for n, i in idx.items()}
        valid = np.all([arr[n] > 0 for n in recipe], axis=0)
        for i, (n, (lo, hi)) in enumerate(zip(recipe, lims)):
            v = np.clip((arr[n] - lo) / max(hi - lo, 1e-6) * 254 + 1, 1, 255)
            v[~valid] = 0
            out.GetRasterBand(i + 1).WriteArray(v.astype("uint8"), 0, y)
    out.BuildOverviews("AVERAGE", [2, 4, 8, 16])
    out = None
    return out_path


def make_index(paths, a, b, out_path, scale=None, block_rows=1024):
    """Normalised difference (a - b) / (a + b) as Float32 GeoTIFF, -9999 = no data.
    scale = {band: (gain, offset)} converts digital numbers to reflectance first."""
    import numpy as np
    from osgeo import gdal
    gdal.UseExceptions()
    scale = scale or {}
    vrt, idx = _band_reader({a: paths[a], b: paths[b]})
    xs, ys = vrt.RasterXSize, vrt.RasterYSize
    out = gdal.GetDriverByName("GTiff").Create(
        out_path, xs, ys, 1, gdal.GDT_Float32,
        ["COMPRESS=DEFLATE", "PREDICTOR=3", "TILED=YES", "BIGTIFF=IF_SAFER"])
    out.SetGeoTransform(vrt.GetGeoTransform())
    out.SetProjection(vrt.GetProjection())
    out.GetRasterBand(1).SetNoDataValue(-9999)
    for y in range(0, ys, block_rows):
        rows = min(block_rows, ys - y)
        ra = vrt.GetRasterBand(idx[a]).ReadAsArray(0, y, xs, rows).astype("float32")
        rb = vrt.GetRasterBand(idx[b]).ReadAsArray(0, y, xs, rows).astype("float32")
        valid = (ra > 0) & (rb > 0)
        ga, oa = scale.get(a, (1.0, 0.0))
        gb, ob = scale.get(b, (1.0, 0.0))
        ra, rb = ra * ga + oa, rb * gb + ob
        with np.errstate(divide="ignore", invalid="ignore"):
            nd = (ra - rb) / (ra + rb)
        nd = np.clip(nd, -1, 1)
        nd[~valid | ~np.isfinite(nd)] = -9999
        out.GetRasterBand(1).WriteArray(nd.astype("float32"), 0, y)
    out.BuildOverviews("AVERAGE", [2, 4, 8, 16])
    out = None
    return out_path


def process_bands(bands, base, out_dir, aoi_geom, clip, products, recipes, scale=None,
                  stack_order=None):
    """bands = {name: path}. Clips (optional), then makes the chosen products.
    recipes = {"TCC": ("rgb", (r, g, b)), "NDVI": ("nd", (a, b)), ...}.
    Returns a list of report lines."""
    from osgeo import gdal
    gdal.UseExceptions()
    report = []
    work = dict(bands)
    if clip:
        os.makedirs(out_dir, exist_ok=True)
        cut = write_aoi(aoi_geom, out_dir)
        clipped, skipped = {}, []
        for n, src in sorted(work.items()):
            out = os.path.join(out_dir, f"{base}_{n}_clip.tif")
            (clipped.__setitem__(n, out) if clip_raster(src, out, cut) else skipped.append(n))
        work = clipped
        report.append(f"Clipped {len(clipped)} band(s)" + (f" (no overlap: {', '.join(skipped)})" if skipped else ""))
        if stack_order:
            names = [n for n in stack_order if n in work]
            if len(names) > 1:
                vrt = os.path.join(out_dir, "_stack.vrt")
                gdal.BuildVRT(vrt, [work[n] for n in names], separate=True, resolution="highest")
                stacked = os.path.join(out_dir, f"{base}_stack_clip.tif")
                gdal.Translate(stacked, vrt, creationOptions=["COMPRESS=DEFLATE", "TILED=YES",
                                                             "BIGTIFF=IF_SAFER"])
                os.remove(vrt)
                report.append(f"Stack: {os.path.basename(stacked)} ({', '.join(names)})")
    else:
        os.makedirs(out_dir, exist_ok=True)
    tag = "_clip" if clip else ""
    for prod in products:
        kind, args = recipes[prod]
        if not all(n in work for n in args):
            report.append(f"{prod} skipped (needs {', '.join(args)})")
            continue
        out = os.path.join(out_dir, f"{base}_{prod}{tag}.tif")
        try:
            if kind == "rgb":
                make_rgb(work, args, out)
            else:
                make_index(work, args[0], args[1], out, scale=scale)
            report.append(f"{prod}: {os.path.basename(out)}")
        except Exception as e:
            report.append(f"{prod} failed: {e}")
    return report


def mosaic_tiles(tiles, out_path, cutline=None, nodata=None):
    """Join GeoTIFF tiles into one file, optionally cut to the AOI."""
    from osgeo import gdal
    gdal.UseExceptions()
    vrt = gdal.BuildVRT("", tiles)
    opts = dict(creationOptions=["COMPRESS=DEFLATE", "TILED=YES", "BIGTIFF=IF_SAFER"])
    if nodata is not None:
        opts["dstNodata"] = nodata
    if cutline:
        opts.update(cutlineDSName=cutline, cropToCutline=True)
    gdal.Warp(out_path, vrt, **opts)
    return out_path


def human_size(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return ""
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {u}" if u in ("B", "KB") else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"


def quicklook_grid(items, cols=4):
    """items: list of (caption, url_or_bytes). Simple preview gallery."""
    if not items:
        return
    for i in range(0, len(items), cols):
        cs = st.columns(cols)
        for c, (cap, src) in zip(cs, items[i:i + cols]):
            if src:
                try:
                    c.image(src, caption=cap, use_container_width=True)
                    continue
                except Exception:
                    pass
            c.caption(f"{cap}\n\n(no preview)")
