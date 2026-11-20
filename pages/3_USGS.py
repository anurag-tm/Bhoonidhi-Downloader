"""
USGS page: Landsat 1-9 and every other dataset of the USGS EarthExplorer
Machine-to-Machine (M2M) API - full product bundles or only the bands you need,
then (optional) unpack / clip / composites / indices on your PC.
"""
import os
import sys
import time
from datetime import date, timedelta

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import geo_common as gc  # noqa: E402
import usgs_api as us  # noqa: E402

st.set_page_config(page_title="USGS Downloader", page_icon="🛰️", layout="wide")
CFG = gc.Settings("usgs")
ss = st.session_state
LAST_SEARCH = os.path.join(gc.APP_DIR, "usgs_last_search.json")


@st.cache_resource
def _api():
    return us.M2M()


API = _api()

if not getattr(API, "auto_tried", False):
    API.auto_tried = True
    u = CFG.get("remember_user")
    if u and not API.ok:
        t = gc.secret_load(f"usgs:{u}")
        if t:
            API.login(u, t)

# ============================================================ sidebar: login
with st.sidebar:
    st.header("USGS login")
    if API.ok:
        st.success(f"Logged in as {API.user}")
        if st.button("Log out", key="usgs_logout"):
            API.logout()
            st.rerun()
        if CFG.get("remember_user") and st.button("Log out and forget me", key="usgs_forget"):
            gc.secret_delete(f"usgs:{CFG.get('remember_user')}")
            CFG.set(remember_user=None)
            API.logout()
            st.rerun()
    else:
        with st.expander("First time? How to get access (free)", expanded=not CFG.get("remember_user")):
            st.markdown(
                "1. Create a USGS account at [ers.cr.usgs.gov](https://ers.cr.usgs.gov/register).\n"
                "2. In your ERS profile, open **Access Request** and request "
                "**Machine-to-Machine (M2M)** access (approval can take a few days).\n"
                "3. Create an **Application Token** in your ERS profile "
                "([how](https://www.usgs.gov/media/files/m2m-application-token-documentation)).\n"
                "4. Enter your username and the token below (not your password).")
        with st.form("usgs_login"):
            u = st.text_input("ERS username", value=CFG.get("remember_user") or "")
            t = st.text_input("Application token", type="password")
            rem = st.checkbox("Remember me on this PC", disabled=not gc.has_keyring(),
                              help="Token is saved in Windows Credential Manager.")
            if st.form_submit_button("Log in"):
                err = API.login(u.strip(), t.strip())
                if err:
                    st.error(err + ("  \nCheck that M2M access is approved for your account."
                                    if "AUTH" in err.upper() else ""))
                else:
                    if rem and gc.secret_save(f"usgs:{u.strip()}", t.strip()):
                        CFG.set(remember_user=u.strip())
                    st.rerun()
    st.caption("Searching and downloading both need the login.")

# ============================================================ page
st.title("🛰️ USGS Downloader")
st.caption("Landsat 1–9 (1972–today) and every other USGS EarthExplorer dataset through the "
           "official M2M API.")

aoi_geom, aoi_bounds = gc.aoi_widget("usgs", CFG)

st.subheader("2. Which data?")
names = list(us.DATASETS) + ["Other USGS dataset… (ASTER, SRTM, declassified, aerial, …)"]
c1, c2 = st.columns([2, 1])
dname = c1.selectbox("Dataset", names, key="usgs_ds",
                     index=names.index(CFG.get("ds_name")) if CFG.get("ds_name") in names else 0)
CFG.set(ds_name=dname)
dataset = us.DATASETS.get(dname)
if dataset is None:
    q = c2.text_input("Search USGS datasets", value=CFG.get("ds_query", "aster"), key="usgs_dsq")
    if st.button("Find datasets", disabled=not API.ok, key="usgs_dsfind"):
        try:
            ss.usgs_ds_list = API.dataset_search(q)
            CFG.set(ds_query=q)
        except us.M2MError as e:
            st.error(str(e))
    lst = ss.get("usgs_ds_list") or []
    if lst:
        pick = st.selectbox("Choose dataset", [f"{d['name']}  ·  {d['alias']}" for d in lst], key="usgs_dspick")
        dataset = pick.rsplit("·", 1)[1].strip()
    elif not API.ok:
        st.info("Log in first to search the USGS dataset list.")

st.markdown("**When?**")
c1, c2, c3 = st.columns(3)
try:
    s0 = date.fromisoformat(CFG.get("start", (date.today() - timedelta(days=365)).isoformat()))
    s1 = date.fromisoformat(CFG.get("end", date.today().isoformat()))
except ValueError:
    s0, s1 = date.today() - timedelta(days=365), date.today()
d0 = c1.date_input("Start date", min(s0, date.today()), min_value=date(1972, 7, 23), max_value=date.today(),
                   format="DD/MM/YYYY", key="usgs_d0")
d1 = c2.date_input("End date", min(s1, date.today()), min_value=date(1972, 7, 23), max_value=date.today(),
                   format="DD/MM/YYYY", key="usgs_d1")
max_cloud = c3.slider("Max cloud cover (%)", 0, 100, int(CFG.get("cloud", 30)), key="usgs_cloud")
CFG.set(start=d0.isoformat(), end=d1.isoformat(), cloud=max_cloud)
if d0 > d1:
    st.error("Start date must be before the end date.")

if st.button("🔍 Search", type="primary", disabled=not (API.ok and aoi_geom and dataset and d0 <= d1)):
    prog = st.progress(0.0, text="Searching USGS...")
    try:
        found = API.scene_search(dataset, aoi_bounds, d0, d1, max_cloud,
                                 progress=lambda n, tot: prog.progress(min(n / max(tot, 1), 1.0),
                                                                       text=f"Found {n} of {tot}..."))
        found = [f for f in found if not f["footprint"] or (gc.cover_pct(f["footprint"], aoi_geom) or 0) > 0]
        ss.usgs_found, ss.usgs_found_ds = found, dataset
        gc.save_json(LAST_SEARCH, {"time": time.strftime("%d/%m/%Y %H:%M"), "found": found, "dataset": dataset})
    except us.M2MError as e:
        st.error(str(e))
        ss.usgs_found = []
    prog.empty()
if "usgs_found" not in ss:
    last = gc.load_json(LAST_SEARCH)
    if last and last.get("found") and last.get("dataset") == dataset:
        ss.usgs_found, ss.usgs_found_ds = last["found"], dataset
        st.caption(f"↩️ Showing your last search from {last.get('time')}. Click Search to refresh.")
if not API.ok:
    st.info("Log in from the sidebar to search.")

found = ss.get("usgs_found") or []
if found and ss.get("usgs_found_ds") == dataset:
    rows = [{"Select": False, "Date": f["date"], "Location": us.wrs(f["display"]),
             "AOI covered %": gc.cover_pct(f["footprint"], aoi_geom) if (f["footprint"] and aoi_geom) else None,
             "Cloud %": round(f["cloud"]) if f["cloud"] is not None else None,
             "Scene": f["display"]} for f in found]
    df = pd.DataFrame(rows).sort_values(["Location", "Date"], ascending=[True, False])
    st.subheader(f"3. Results – {len(df)} scenes")
    c1, c2 = st.columns([2, 1])
    locs = sorted(df["Location"].unique())
    pick = c1.multiselect("Show only these locations (same path/row = same area)", locs, key="usgs_locs")
    if pick:
        df = df[df["Location"].isin(pick)]
    if c2.checkbox("Select all shown", key="usgs_all"):
        df["Select"] = True
    ed = st.data_editor(df, hide_index=True, use_container_width=True,
                        disabled=[c for c in df.columns if c != "Select"], key="usgs_tbl")
    chosen = list(ed[ed["Select"]]["Scene"])
    by_disp = {f["display"]: f for f in found}
    if chosen and st.toggle("Show previews of ticked scenes", key="usgs_prev"):
        gc.quicklook_grid([(f"{by_disp[d]['date']} · {us.wrs(d)}", by_disp[d]["thumb"]) for d in chosen[:12]])

    # ------------------------------------------------ download & process
    st.subheader("4. Download & process")
    c1, c2 = st.columns(2)
    out_dir = c1.text_input("Save to folder", value=CFG.get("out_dir", os.path.join(os.getcwd(), "usgs_downloads")),
                            key="usgs_out")
    n_par = c2.number_input("Downloads at the same time", 1, 6, int(CFG.get("parallel", 4)), 1, key="usgs_par")
    CFG.set(out_dir=out_dir, parallel=int(n_par))

    what = st.radio("What to download", ["Full product (all bands, one archive)",
                                         "Only the bands I choose (smaller, faster)"],
                    key="usgs_what", horizontal=True,
                    index=1 if CFG.get("what") == "bands" else 0)
    CFG.set(what="bands" if what.startswith("Only") else "full")
    band_mode = what.startswith("Only")
    want_bands = []
    if band_mode and chosen:
        key = (dataset, chosen[0])
        if ss.get("usgs_opts_key") != key:
            with st.spinner("Asking USGS which files are available..."):
                try:
                    ss.usgs_opts = API.download_options(dataset, [by_disp[chosen[0]]["entity"]])
                    ss.usgs_opts_key = key
                except us.M2MError as e:
                    st.error(str(e))
                    ss.usgs_opts = {}
        files = us.band_files(next(iter((ss.get("usgs_opts") or {}).values()), []))
        if files:
            sens = us.sensor(chosen[0]) or "OLI"
            need = sorted({b for _, (k, a) in us.RECIPES.get(sens, {}).items() for b in a})
            default = [f for f in files if any(f.upper().endswith(f"_{b}.TIF") or f.upper() == f"{b}.TIF"
                                               or f.upper().endswith(f"SR_{b}.TIF") for b in need)]
            saved = [f for f in CFG.get("bands", []) if f in files]
            want_bands = st.multiselect("Bands / files to download", sorted(files),
                                        default=saved or default, key="usgs_bands",
                                        help="The default is what TCC, FCC, SWIR, NDVI, NDSI and NDWI need.")
            CFG.set(bands=want_bands)
        elif ss.get("usgs_opts") is not None:
            st.warning("This dataset offers no single-band files - use 'Full product'.")

    st.markdown("**After download**")
    opts = None
    sens = us.sensor(chosen[0]) if chosen else None
    recipes = us.RECIPES.get(sens, {})
    if gc.gdal_ok():
        c1, c2, c3 = st.columns(3)
        unpack = c1.checkbox("Unpack / process", value=CFG.get("unpack", True), key="usgs_unpack",
                             help="Extract the .tar archive (full product) and do the steps below.")
        clip = c2.checkbox("Clip to my area", value=CFG.get("clip", True), disabled=not unpack, key="usgs_clip")
        keep = c3.radio("Then the archive", ["Keep", "Delete"], horizontal=True, disabled=not (unpack and not band_mode),
                        index=0 if CFG.get("keep_tar", True) else 1, key="usgs_keep")
        prods = st.multiselect("Make these products", list(recipes),
                               default=[p for p in CFG.get("prods", ["TCC"]) if p in recipes],
                               disabled=not (unpack and recipes), key="usgs_prods",
                               help="TCC true colour · FCC false colour (NIR-Red-Green) · SWIR (snow/ice, geology) · "
                                    "NDVI · NDSI (snow & glaciers) · NDWI (water). Level-2 values are converted "
                                    "to surface reflectance first.")
        if chosen and not recipes:
            st.caption("Products (TCC, NDVI, …) are available for Landsat scenes. Other datasets are unpacked and clipped.")
        stack = st.checkbox("Also save the reflective bands as one stacked file", value=CFG.get("stack", False),
                            disabled=not (unpack and clip), key="usgs_stack")
        CFG.set(unpack=unpack, clip=clip, keep_tar=keep == "Keep", prods=prods, stack=stack)
        if unpack:
            opts = {"clip": clip, "prods": prods, "keep": keep == "Keep", "stack": stack}
    else:
        st.caption("Unpack / clip / products need GDAL (conda install -c conda-forge gdal).")

    label_n = len(chosen) * (len(want_bands) if band_mode else 1)
    go = st.button(f"⬇️ Download {len(chosen)} scene(s)" + (f" · {label_n} files" if band_mode else ""),
                   type="primary", disabled=not chosen or (band_mode and not want_bands))
    if go:
        os.makedirs(out_dir, exist_ok=True)
        with st.spinner("Asking USGS for download options..."):
            try:
                options = API.download_options(dataset, [by_disp[d]["entity"] for d in chosen])
            except us.M2MError as e:
                st.error(str(e))
                options = None
        if options is not None:
            paths = {}
            jobs = []
            stamp = time.strftime("%Y%m%d%H%M%S")
            for d in chosen:
                sc = by_disp[d]
                prods_ = options.get(sc["entity"], [])

                def fn(sink, d=d, sc=sc, prods_=prods_):
                    have = gc.index_get(out_dir, d)
                    if have:
                        paths[d] = have
                        return True, "Already downloaded"
                    if band_mode:
                        files = us.band_files(prods_)
                        folder = os.path.join(out_dir, d)
                        got = []
                        for k, suf in enumerate(want_bands, 1):
                            if suf not in files:
                                continue
                            ent, pid, fname = files[suf]
                            if os.path.exists(os.path.join(folder, fname)):
                                got.append(fname)
                                continue
                            sink.write(f"file {k}/{len(want_bands)}: {fname}")
                            url = API.request_url(ent, pid, f"bd-{stamp}-{d[:30]}-{k}", sink=sink)
                            ok, msg, _ = gc.download_file(url, folder, fname, sink=sink, follow_auth=False)
                            if not ok:
                                return False, f"{fname}: {msg}"
                            got.append(fname)
                        if not got:
                            return False, "None of the chosen files exist for this scene."
                        paths[d] = folder
                        gc.index_set(out_dir, d, folder)
                        return True, f"{len(got)} file(s) in {folder}"
                    prod = us.pick_bundle(prods_)
                    if not prod:
                        return False, "No downloadable product for this scene."
                    sink.write("asking USGS for the download link...")
                    url = API.request_url(sc["entity"], prod["id"], f"bd-{stamp}-{d[:40]}", sink=sink)
                    ok, msg, path = gc.download_file(url, out_dir, d + ".tar", sink=sink, follow_auth=False)
                    if ok:
                        paths[d] = path
                        gc.index_set(out_dir, d, path)
                    return ok, msg
                jobs.append((d, fn))
            st.markdown("**Step 1 - downloading**")
            res1 = gc.run_jobs(jobs, int(n_par), "Downloading")
            if opts and paths:
                st.markdown("**Step 2 - unpack / clip / products**")
                pjobs = []
                for d, path in paths.items():
                    def pfn(sink, d=d, path=path):
                        rep = []
                        folder = path
                        if os.path.isfile(path) and path.lower().endswith(gc.ARCHIVE_EXT):
                            sink.write("unpacking...")
                            folder = gc.unpack(path)
                            rep.append(f"Unpacked -> {folder}")
                        s_ = us.sensor(d)
                        bands = us.landsat_bands(folder) if s_ else {}
                        if not bands:      # non-Landsat: clip every raster found
                            bands = {os.path.splitext(os.path.basename(r))[0][-20:]: r
                                     for r in gc.list_rasters(folder)}
                        if (opts["clip"] or opts["prods"]) and bands:
                            sink.write("clipping / making products...")
                            base = f"{by_disp[d]['date']}_{us.wrs(d).replace('Path ', 'P').replace(' / Row ', 'R')}" if s_ else d
                            od = os.path.join(out_dir, base + ("_clip" if opts["clip"] else "_products"))
                            rep += gc.process_bands(bands, base, od, aoi_geom, opts["clip"],
                                                    [p for p in opts["prods"] if p in us.RECIPES.get(s_, {})],
                                                    us.RECIPES.get(s_, {}), scale=us.landsat_scale(d),
                                                    stack_order=us.STACK.get(s_) if opts["stack"] else None)
                        if not opts["keep"] and os.path.isfile(path) and folder != path:
                            os.remove(path)
                            gc.index_set(out_dir, d, folder)
                            rep.append("Archive deleted")
                        return True, "; ".join(rep) or "Nothing to do"
                    pjobs.append((d, pfn))
                gc.run_jobs(pjobs, 2, "Processing")
            n_ok = sum(ok for ok, _ in res1.values())
            (st.success if n_ok == len(jobs) else st.warning)(
                f"{n_ok} of {len(jobs)} scene(s) downloaded to {out_dir}. "
                + ("" if n_ok == len(jobs) else "Click Download again to resume the others."))
elif "usgs_found" in ss and ss.get("usgs_found_ds") == dataset:
    st.info("No scenes found. Try a longer period, a higher cloud limit or a bigger area.")
