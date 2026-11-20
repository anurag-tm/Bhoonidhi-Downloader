"""
Copernicus page: Sentinel-1/2/3/5P, Copernicus DEM and more from the
Copernicus Data Space Ecosystem (CDSE).

Two ways to get data:
  A) Ready-made for your area - processed on Copernicus servers (TCC, FCC, NDVI, NDSI, SAR dB, DEM)
  B) Full original products   - whole scenes, then unzip / clip / composites on your PC
"""
import os
import sys
import time
from datetime import date, timedelta

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import copernicus_api as cop  # noqa: E402
import geo_common as gc  # noqa: E402

st.set_page_config(page_title="Copernicus Downloader", page_icon="🛰️", layout="wide")
CFG = gc.Settings("copernicus")
ss = st.session_state
LAST_SEARCH = os.path.join(gc.APP_DIR, "copernicus_last_search.json")


@st.cache_resource
def _auth():
    return cop.CDSEAuth()


AUTH = _auth()


def _auto_login():
    if getattr(AUTH, "auto_tried", False):
        return
    AUTH.auto_tried = True
    u = CFG.get("remember_user")
    if u and not AUTH.user:
        pwd = gc.secret_load(f"cdse:{u}")
        if pwd:
            AUTH.login(u, pwd)
    cid = CFG.get("remember_client")
    if cid and not AUTH.client_id:
        sec = gc.secret_load(f"cdse-client:{cid}")
        if sec:
            AUTH.set_client(cid, sec)


_auto_login()

# ============================================================ sidebar: logins
with st.sidebar:
    st.header("Copernicus login")
    st.caption("Free account: [dataspace.copernicus.eu](https://dataspace.copernicus.eu) → Register.")

    st.markdown("**1 · Your account** — for full product downloads")
    if AUTH.user and AUTH.user_token():
        st.success(f"Logged in as {AUTH.user}")
        if st.button("Log out", key="cop_logout"):
            AUTH.logout()
            st.rerun()
        if CFG.get("remember_user") and st.button("Log out and forget me", key="cop_forget"):
            gc.secret_delete(f"cdse:{CFG.get('remember_user')}")
            CFG.set(remember_user=None)
            AUTH.logout()
            st.rerun()
    else:
        with st.form("cop_login"):
            u = st.text_input("E-mail / username", value=CFG.get("remember_user") or "")
            p = st.text_input("Password", type="password")
            totp = st.text_input("2FA code (only if you turned on 2-factor login)", value="")
            rem = st.checkbox("Remember me on this PC", disabled=not gc.has_keyring(),
                              help="Password is saved in Windows Credential Manager.")
            if st.form_submit_button("Log in"):
                err = AUTH.login(u.strip(), p, totp.strip() or None)
                if err:
                    st.error(err)
                else:
                    if rem and gc.secret_save(f"cdse:{u.strip()}", p):
                        CFG.set(remember_user=u.strip())
                    st.rerun()

    st.markdown("**2 · API client** — for *ready-made* data (processed on Copernicus servers)")
    if AUTH.client_id and AUTH.client_token():
        st.success(f"API client active ({AUTH.client_id[:10]}…)")
        if st.button("Remove API client", key="cop_rmclient"):
            if CFG.get("remember_client"):
                gc.secret_delete(f"cdse-client:{CFG.get('remember_client')}")
                CFG.set(remember_client=None)
            AUTH.clear_client()
            st.rerun()
    else:
        with st.expander("How do I get an API client? (2 minutes, free)"):
            st.markdown(
                "1. Open the [Copernicus dashboard](https://shapps.dataspace.copernicus.eu/dashboard/) "
                "and log in with your account.\n"
                "2. Go to **User settings → OAuth clients → + Create**.\n"
                "3. Give it a name, set an expiry date, click **Create**.\n"
                "4. Copy the **Client ID** and **Client secret** here. "
                "(The secret is shown only once.)\n\n"
                "Free accounts get a monthly processing allowance; you can see what is left "
                "on the dashboard.")
        with st.form("cop_client"):
            cid = st.text_input("Client ID", value=CFG.get("remember_client") or "")
            sec = st.text_input("Client secret", type="password")
            rem_c = st.checkbox("Remember on this PC", disabled=not gc.has_keyring(), key="cop_remc")
            if st.form_submit_button("Save API client"):
                err = AUTH.set_client(cid.strip(), sec.strip())
                if err:
                    st.error(err)
                else:
                    if rem_c and gc.secret_save(f"cdse-client:{cid.strip()}", sec.strip()):
                        CFG.set(remember_client=cid.strip())
                    st.rerun()
    st.caption("You need **1** for full downloads and **2** for ready-made data. "
               "With only an API client, full downloads also work.")

# ============================================================ page
st.title("🛰️ Copernicus Downloader")
st.caption("Sentinel-1, Sentinel-2, Sentinel-3, Sentinel-5P, Copernicus DEM and more from the "
           "Copernicus Data Space Ecosystem.")

aoi_geom, aoi_bounds = gc.aoi_widget("cop", CFG)

st.subheader("2. How do you want the data?")
modes = ["Ready-made for my area (processed on Copernicus servers)",
         "Full original products (whole scenes)"]
mode = st.radio("Choose one", modes, key="cop_mode",
                index=modes.index(CFG.get("mode")) if CFG.get("mode") in modes else 0,
                captions=["Fast and small: you get only your area, already as true colour, false "
                          "colour, NDVI, NDSI, SAR backscatter or DEM. Needs the API client.",
                          "The complete scene as published (SAFE/ZIP, often 1 GB+), for your own "
                          "processing in SNAP, QGIS etc. Needs the account (or API client)."])
CFG.set(mode=mode)
server_mode = mode == modes[0]


def _dates(prefix, d0, d1):
    c1, c2 = st.columns(2)
    try:
        s0 = date.fromisoformat(CFG.get(f"{prefix}_start", d0.isoformat()))
        s1 = date.fromisoformat(CFG.get(f"{prefix}_end", d1.isoformat()))
    except ValueError:
        s0, s1 = d0, d1
    s0 = min(max(s0, date(2014, 4, 1)), date.today())
    s1 = min(max(s1, date(2014, 4, 1)), date.today())
    a = c1.date_input("Start date", s0, min_value=date(2014, 4, 1), max_value=date.today(),
                      format="DD/MM/YYYY", key=f"{prefix}_d0")
    b = c2.date_input("End date", s1, min_value=date(2014, 4, 1), max_value=date.today(),
                      format="DD/MM/YYYY", key=f"{prefix}_d1")
    CFG.set(**{f"{prefix}_start": a.isoformat(), f"{prefix}_end": b.isoformat()})
    if a > b:
        st.error("Start date must be before the end date.")
        return None, None
    return a, b


default_end = date.today()
default_start = default_end - timedelta(days=90)

# ============================================================ A) server-side
if server_mode:
    st.subheader("3. What do you need?")
    keys = list(cop.SERVER_DATA)
    c1, c2 = st.columns([1, 2])
    dkey = c1.selectbox("Data", keys, key="cop_srv_data",
                        index=keys.index(CFG.get("srv_data")) if CFG.get("srv_data") in keys else 0)
    spec = cop.SERVER_DATA[dkey]
    prods = list(spec["products"])
    saved = [p for p in CFG.get(f"srv_prod_{dkey}", [prods[0]]) if p in prods] or [prods[0]]
    chosen_prods = c2.multiselect("Products to make (one file each)", prods, default=saved,
                                  key=f"cop_srv_prod_{dkey}")
    CFG.set(srv_data=dkey, **{f"srv_prod_{dkey}": chosen_prods})

    c1, c2, c3 = st.columns(3)
    res = c1.selectbox("Pixel size (m)", sorted({spec["res"], 10, 20, 30, 60, 100} - ({10, 20} if spec["res"] == 30 else set())),
                       index=0, key=f"cop_res_{dkey}",
                       help="Bigger pixels = smaller files and less processing allowance used.")
    max_cloud = gain = None
    s1 = None
    if spec["cloud"]:
        max_cloud = c2.slider("Max cloud cover (%)", 0, 100, int(CFG.get("srv_cloud", 30)), key="cop_srv_cloud")
        gain = c3.slider("Brightness of colour images", 1.0, 4.0, float(CFG.get("srv_gain", 2.5)), 0.1,
                         key="cop_gain", help="Lower it if snow/glaciers look burnt-out white.")
        CFG.set(srv_cloud=max_cloud, srv_gain=gain)
    if spec["type"] == "sentinel-1-grd":
        orbit = c2.selectbox("Satellite pass", ["Any", "Ascending", "Descending"], key="cop_orbit")
        pol = c3.selectbox("Polarisation", ["VV + VH (most of the world)", "HH + HV (polar areas)"],
                           key="cop_pol")
        cal = c1.radio("Calibration", ["Terrain-flattened γ⁰ (best for mountains)", "σ⁰ (ellipsoid)"],
                       key="cop_cal")
        speckle = c2.checkbox("Reduce speckle (Lee 5×5)", key="cop_speckle")
        s1 = {"orbit": None if orbit == "Any" else orbit.upper(),
              "polarization": "DH" if pol.startswith("HH") else "DV",
              "backCoeff": "GAMMA0_TERRAIN" if cal.startswith("Terrain") else "SIGMA0_ELLIPSOID",
              "speckle": speckle}

    if spec["type"] == "dem":
        when = "dem"
        d0, d1 = date(2010, 1, 1), date.today()
    else:
        st.markdown("**When?**")
        d0, d1 = _dates("srv", default_start, default_end)
        whens = ["One image per date (I pick the dates)",
                 "One cloud-free mosaic of the whole period" if spec["cloud"] else
                 "One mosaic of the whole period (most recent on top)"]
        when = st.radio("Images", whens, horizontal=True, key="cop_when")

    if aoi_geom is not None:
        from pyproj import Transformer
        from shapely.geometry import shape
        g = shape(aoi_geom)
        ep = gc.utm_epsg(g.centroid.x, g.centroid.y)
        b = gc.reproject(g, Transformer.from_crs(4326, ep, always_xy=True).transform).bounds
        w, h = (b[2] - b[0]) / res, (b[3] - b[1]) / res
        n_t = len(cop.plan_tiles(b, res))
        st.caption(f"Output size ≈ {w:,.0f} × {h:,.0f} pixels ({n_t} request{'s' if n_t > 1 else ''} "
                   "per product)." + (" Large area: think about bigger pixels." if n_t > 9 else ""))

    out_dir = st.text_input("Save to folder", value=CFG.get("out_dir", os.path.join(os.getcwd(), "copernicus_downloads")),
                            key="cop_srv_out")
    CFG.set(out_dir=out_dir)
    ok_inputs = aoi_geom is not None and chosen_prods and (when == "dem" or d0)
    jobs_dates = []

    if when.startswith("One image per date"):
        if st.button("🔍 Find available dates", disabled=not ok_inputs, type="primary"):
            coll, name = spec["search"]
            with st.spinner("Searching the Copernicus catalogue..."):
                try:
                    ss.cop_srv_found = cop.search(coll, name, aoi_geom, d0, d1,
                                                  max_cloud if spec["cloud"] else None)
                    ss.cop_srv_key = dkey
                except Exception as e:
                    st.error(str(e))
                    ss.cop_srv_found = []
        found = ss.get("cop_srv_found") if ss.get("cop_srv_key") == dkey else None
        if found is not None:
            from shapely.geometry import shape
            from shapely.ops import unary_union
            groups = {}
            for p in found:
                if s1 and s1["orbit"] and p["orbit_dir"] and p["orbit_dir"].upper() != s1["orbit"]:
                    continue
                groups.setdefault(p["date"], []).append(p)
            rows = []
            a_shp = shape(aoi_geom)
            for d, ps in sorted(groups.items(), reverse=True):
                try:
                    cov = round(unary_union([shape(p["footprint"]) for p in ps if p["footprint"]])
                                .intersection(a_shp).area / a_shp.area * 100)
                except Exception:
                    cov = None
                clouds = [p["cloud"] for p in ps if p["cloud"] is not None]
                rows.append({"Select": False, "Date": d, "AOI covered %": cov,
                             "Cloud % (scene)": round(sum(clouds) / len(clouds)) if clouds else None,
                             "Pass": ", ".join(sorted({(p["orbit_dir"] or "")[:3].title() for p in ps if p["orbit_dir"]})),
                             "Scenes": len(ps)})
            if not rows:
                st.warning("No images found. Try a longer period or higher cloud limit.")
            else:
                st.markdown(f"**{len(rows)} dates found** — tick the ones you want")
                df = pd.DataFrame(rows)
                if st.checkbox("Select all dates", key="cop_srv_all"):
                    df["Select"] = True
                ed = st.data_editor(df, hide_index=True, use_container_width=True,
                                    disabled=[c for c in df.columns if c != "Select"], key="cop_srv_tbl")
                jobs_dates = list(ed[ed["Select"]]["Date"])
                if jobs_dates and st.toggle("Show previews of the ticked dates", key="cop_srv_prev"):
                    items = []
                    with st.spinner("Loading previews..."):
                        for d in jobs_dates[:8]:
                            p = groups[d][0]
                            items.append((f"{d} · {cop.location_of(p)}", cop.quicklook(p["id"])))
                    gc.quicklook_grid(items)
                    st.caption("Previews show the whole satellite scene, not only your area.")
        n_jobs = len(jobs_dates) * len(chosen_prods)
        go = st.button(f"⚙️ Make & download {n_jobs} file(s)", disabled=not (ok_inputs and jobs_dates))
    else:
        n_jobs = len(chosen_prods)
        go = st.button(f"⚙️ Make & download {n_jobs} file(s)", disabled=not ok_inputs, type="primary")

    if go:
        if not (AUTH.client_id and AUTH.client_token()):
            st.error("Add your API client in the sidebar first (it is free - see 'How do I get an API client?').")
        else:
            os.makedirs(out_dir, exist_ok=True)
            short = gc.safe_name(dkey.split("–")[0])
            jobs = []
            periods = ([(d, f"{d}T00:00:00Z", f"{d}T23:59:59Z") for d in jobs_dates]
                       if jobs_dates else
                       [(f"{d0}_to_{d1}" if d0 and when != "dem" else "DEM",
                         f"{d0}T00:00:00Z", f"{d1}T23:59:59Z")])
            for tag, t0, t1 in periods:
                for prod in chosen_prods:
                    name = f"{short}_{tag}_{gc.safe_name(prod.split('(')[0].split('–')[0])}.tif"
                    out = os.path.join(out_dir, name)

                    def fn(sink, t0=t0, t1=t1, prod=prod, out=out):
                        return cop.process_area(AUTH, aoi_geom, dkey, prod, t0, t1, out, res=res,
                                                max_cloud=max_cloud, gain=gain or 2.5, s1=s1,
                                                mosaic="leastCC", sink=sink)
                    jobs.append((name, fn))
            res_ = gc.run_jobs(jobs, workers=2, title="Processing on Copernicus servers")
            n_ok = sum(ok for ok, _ in res_.values())
            (st.success if n_ok == len(jobs) else st.warning)(
                f"{n_ok} of {len(jobs)} file(s) saved in {out_dir}")
            st.caption("Colour images are 8-bit GeoTIFFs (0 = no data). Index, band and DEM files are "
                       "Float32 GeoTIFFs (-9999 = no data). All are in UTM, ready for QGIS/ArcGIS.")

# ============================================================ B) full products
else:
    st.subheader("3. Which products?")
    names = list(cop.FULL_PRODUCTS) + ["Other collection…"]
    c1, c2 = st.columns([2, 1])
    pname = c1.selectbox("Product type", names, key="cop_full_type",
                         index=names.index(CFG.get("full_type")) if CFG.get("full_type") in names else 0)
    CFG.set(full_type=pname)
    if pname == "Other collection…":
        coll = c2.selectbox("Collection", cop.ALL_COLLECTIONS, key="cop_full_coll")
        name_has = st.text_input("Product name contains (optional, e.g. 'IW_GRDH' or 'MSIL2A')",
                                 key="cop_full_name")
        has_cloud = coll in ("SENTINEL-2", "LANDSAT-8", "LANDSAT-9", "LANDSAT-7", "LANDSAT-5")
    else:
        coll, name_has, has_cloud = cop.FULL_PRODUCTS[pname]
    d0, d1 = _dates("full", default_start, default_end)
    c1, c2 = st.columns(2)
    max_cloud = c1.slider("Max cloud cover (%)", 0, 100, int(CFG.get("full_cloud", 30)),
                          key="cop_full_cloud") if has_cloud else None
    if max_cloud is not None:
        CFG.set(full_cloud=max_cloud)
    online_only = c2.checkbox("Only products ready to download now (online)", value=True, key="cop_online")

    if st.button("🔍 Search", type="primary", disabled=not (aoi_geom and d0)):
        prog = st.progress(0.0, text="Searching...")
        try:
            found = cop.search(coll, name_has, aoi_geom, d0, d1, max_cloud, online_only,
                               progress=lambda i, n, a, b: prog.progress(
                                   (i - 1) / n, text=f"Searching {a:%d/%m/%Y} - {b:%d/%m/%Y} ({i}/{n})"))
            ss.cop_full_found = found
            gc.save_json(LAST_SEARCH, {"time": time.strftime("%d/%m/%Y %H:%M"), "found": found,
                                       "type": pname})
        except Exception as e:
            st.error(str(e))
            ss.cop_full_found = []
        prog.empty()
    if "cop_full_found" not in ss:
        last = gc.load_json(LAST_SEARCH)
        if last and last.get("found") and last.get("type") == pname:
            ss.cop_full_found = last["found"]
            st.caption(f"↩️ Showing your last search from {last.get('time')}. Click Search to refresh.")
    found = ss.get("cop_full_found") or []

    if found:
        rows = [{"Select": False, "Date": p["date"], "Location": cop.location_of(p),
                 "AOI covered %": gc.cover_pct(p["footprint"], aoi_geom) if (p["footprint"] and aoi_geom) else None,
                 "Cloud %": round(p["cloud"]) if p["cloud"] is not None else None,
                 "Size": gc.human_size(p["size"]), "Online": "yes" if p["online"] else "no",
                 "Name": p["name"]} for p in found]
        df = pd.DataFrame(rows)
        locs = sorted(df["Location"].unique())
        st.markdown(f"**{len(df)} products found**")
        c1, c2 = st.columns([2, 1])
        pick = c1.multiselect("Show only these locations (same tile / orbit = same area)", locs,
                              key="cop_full_locs")
        if pick:
            df = df[df["Location"].isin(pick)]
        if c2.checkbox("Select all shown", key="cop_full_all"):
            df["Select"] = True
        ed = st.data_editor(df, hide_index=True, use_container_width=True,
                            disabled=[c for c in df.columns if c != "Select"], key="cop_full_tbl")
        chosen = list(ed[ed["Select"]]["Name"])
        by_name = {p["name"]: p for p in found}
        if chosen and st.toggle("Show previews of ticked products", key="cop_full_prev"):
            with st.spinner("Loading previews..."):
                gc.quicklook_grid([(f"{by_name[n]['date']} · {cop.location_of(by_name[n])}",
                                    cop.quicklook(by_name[n]["id"])) for n in chosen[:8]])

        st.subheader("4. Download & process")
        total = sum(float(by_name[n]["size"] or 0) for n in chosen)
        if chosen:
            st.caption(f"{len(chosen)} product(s), about {gc.human_size(total)} in total.")
        c1, c2 = st.columns(2)
        out_dir = c1.text_input("Save to folder", value=CFG.get("out_dir", os.path.join(os.getcwd(), "copernicus_downloads")),
                                key="cop_full_out")
        n_par = c2.number_input("Downloads at the same time", 1, 4, int(CFG.get("parallel", 2)), 1,
                                key="cop_par", help="Copernicus allows at most 4 per user.")
        CFG.set(out_dir=out_dir, parallel=int(n_par))

        is_s2 = coll == "SENTINEL-2"
        st.markdown("**After download**")
        if is_s2 and gc.gdal_ok() and not gc.jp2_ok():
            st.warning("Your GDAL cannot read Sentinel-2 JPEG2000 bands yet. Run install_windows.bat "
                       "again (or: conda install -c conda-forge libgdal-jp2openjpeg), then restart.")
        opts = None
        if gc.gdal_ok():
            c1, c2, c3 = st.columns(3)
            unzip = c1.checkbox("Unzip", value=CFG.get("unzip", True), key="cop_unzip")
            clip = c2.checkbox("Clip to my area", value=CFG.get("clip", True) and is_s2,
                               disabled=not (unzip and is_s2), key="cop_clip",
                               help="Available for Sentinel-2. Other products are unzipped only.")
            zip_keep = c3.radio("Then the ZIP file", ["Keep", "Delete"], horizontal=True,
                                index=0 if CFG.get("keep_zip", True) else 1, key="cop_zipkeep",
                                disabled=not unzip)
            prods = []
            if is_s2:
                prods = st.multiselect("Make these from Sentinel-2 bands", list(cop.S2_RECIPES),
                                       default=[p for p in CFG.get("s2_prods", ["TCC"]) if p in cop.S2_RECIPES],
                                       disabled=not unzip, key="cop_s2prods",
                                       help="TCC true colour · FCC NIR-Red-Green · SWIR B12-B8A-B4 (snow/ice) · "
                                            "NDVI · NDSI (snow, B3/B11) · NDWI (water, B3/B8)")
                stack = st.checkbox("Also save main bands (B2, B3, B4, B8, B11, B12) as one stacked file",
                                    value=CFG.get("stack", False), disabled=not (unzip and clip), key="cop_stack")
            else:
                stack = False
            CFG.set(unzip=unzip, clip=bool(clip), keep_zip=zip_keep == "Keep", s2_prods=prods, stack=stack)
            if unzip:
                opts = {"clip": bool(clip and is_s2), "prods": prods if is_s2 else [],
                        "keep_zip": zip_keep == "Keep", "stack": stack}
        else:
            st.caption("Unzip / clip / composites need GDAL (conda install -c conda-forge gdal).")

        if st.button(f"⬇️ Download {len(chosen)} product(s)", disabled=not chosen, type="primary"):
            if not AUTH.download_headers():
                st.error("Log in with your Copernicus account (or add an API client) in the sidebar first.")
            else:
                jobs, paths = [], {}
                for n in chosen:
                    p = by_name[n]

                    def fn(sink, p=p):
                        stem = p["name"].replace(".SAFE", "").replace(".SEN3", "")
                        have = gc.index_get(out_dir, p["name"])
                        if have:                # ZIP, or its folder if the ZIP was deleted
                            paths[p["name"]] = have
                            return True, "Already downloaded"
                        ext = ".nc" if p["name"].endswith(".nc") else ".zip"
                        ok, msg, path = gc.download_file(cop.DOWNLOAD_URL.format(id=p["id"]), out_dir,
                                                         stem + ext,
                                                         headers_fn=lambda force: AUTH.download_headers(force),
                                                         sink=sink)
                        if ok:
                            paths[p["name"]] = path
                            gc.index_set(out_dir, p["name"], path)
                        return ok, msg
                    jobs.append((n[:60], fn))
                st.markdown("**Step 1 - downloading**")
                res1 = gc.run_jobs(jobs, int(n_par), "Downloading")
                if opts and paths:
                    st.markdown("**Step 2 - unzip / clip / products**")
                    pjobs = []
                    for n, path in paths.items():
                        def pfn(sink, n=n, path=path):
                            sink.write("unzipping...")
                            folder = path if os.path.isdir(path) else gc.unpack(path)
                            rep = [f"Unzipped -> {folder}"]
                            if coll == "SENTINEL-2" and (opts["clip"] or opts["prods"] or opts["stack"]):
                                bands = cop.s2_bands(folder)
                                if not bands:
                                    rep.append("No Sentinel-2 band files found")
                                else:
                                    base = by_name[n]["date"] + "_" + (by_name[n]["tile"] or "S2")
                                    od = os.path.join(out_dir, base + ("_clip" if opts["clip"] else "_products"))
                                    sink.write("clipping / making products...")
                                    rep += gc.process_bands(bands, base, od, aoi_geom, opts["clip"],
                                                            opts["prods"], cop.S2_RECIPES,
                                                            scale=cop.s2_scale(n),
                                                            stack_order=cop.S2_STACK if opts["stack"] else None)
                            if not opts["keep_zip"] and os.path.isfile(path):
                                os.remove(path)
                                gc.index_set(out_dir, n, folder)   # still counts as downloaded
                                rep.append("ZIP deleted")
                            return True, "; ".join(rep)
                        pjobs.append((n[:60], pfn))
                    gc.run_jobs(pjobs, 2, "Processing")
                n_ok = sum(ok for ok, _ in res1.values())
                (st.success if n_ok == len(jobs) else st.warning)(
                    f"{n_ok} of {len(jobs)} product(s) downloaded to {out_dir}. "
                    + ("" if n_ok == len(jobs) else "Click Download again to resume the others."))
    elif "cop_full_found" in ss:
        st.info("Nothing found. Try a longer period, a higher cloud limit, or untick 'online'.")
