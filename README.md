# 🛰️ Bhoonidhi Downloader

A simple app to **search, preview, download and clip ISRO Resourcesat satellite data (LISS-IV, LISS-III, AWiFS)** from the NRSC **Bhoonidhi** API — no coding needed.

> **This is the `copernicus` branch.** Besides Bhoonidhi, it adds a **Copernicus** page for Sentinel-1/2/3/5P, Copernicus DEM and more. See [Copernicus page](#copernicus-page) below. The stable Bhoonidhi-only version is on the `main` branch.

You pick an area and dates, the app finds the scenes, shows you quick-look previews side by side, downloads the ones you choose (4 at a time) and clips them to your area.

![App screenshot](docs/screenshot.png)

> Not affiliated with NRSC or ISRO. You need your own free Bhoonidhi account. Please follow Bhoonidhi's terms of use.

---

## What it can do

- **Area of interest** in three ways: a point with a radius (circle) or size (square) in km, a bounding box, or your own shapefile / GeoJSON / GeoPackage (any projection).
- **Any date range.** Bhoonidhi allows only 366 days per search, so the app splits long ranges into yearly searches and merges the results.
- **Cloud cover filter.** Read from each scene's metadata file, or estimated from its quick-look image when the metadata has none.
- **Results table** showing location (path / row / subscene), how much of your area each scene covers, cloud %, and date.
- **Compare window.** 2–4 scenes of the same location side by side; all windows zoom and pan together. Works *before* downloading, using quick-looks.
- **Fast downloads.** 1–5 scenes at the same time (default 4), with automatic retry and **resume** if the internet drops.
- **After download, your choice:** keep the ZIP only, or unzip, then optionally clip to your area, stack the bands, and make an **FCC** (false colour) and/or **TCC** (true colour). Keep or delete the ZIP afterwards. Processing starts when all downloads are finished.
- **Process old ZIPs too:** point the app at a folder of LISS-IV ZIPs already on your PC and it unzips them and makes FCC/TCC in one click. Already-done files are skipped.
- **Stays logged in.** The token is refreshed every 18 min (also while idle), page reloads keep you logged in, and an optional "Remember me on this PC" setting logs you in when the app starts.
- **Never lose your work.** Your last shapefile, area, dates, filters and search results come back automatically after the PC sleeps, the browser disconnects or the app restarts. Download settings are remembered too.

---

## 1. Before you start

1. **A Bhoonidhi account.** Register at <https://bhoonidhi.nrsc.gov.in>. The app uses the same user ID and password.
2. **Miniconda (free) or Anaconda.** If you don't have either, install Miniconda: <https://docs.conda.io/en/latest/miniconda.html>. The default options are fine.

---

## 2. Install (Windows, the easy way)

1. On this page click the green **`Code`** button → **`Download ZIP`**, then unzip it (for example to `C:\Users\<you>\Bhoonidhi-Downloader`).
2. Open the folder and **double-click `install_windows.bat`**.
   The first install takes 5–15 minutes. Wait until it says *"Install finished"*.
3. **Double-click `run_windows.bat`.** The app opens in your web browser.
   Keep the black window open while you use the app; close it to stop the app.

That's it. From now on you only need **`run_windows.bat`**.

> 💡 Right-click `run_windows.bat` → *Send to* → *Desktop (create shortcut)* to get a desktop icon.

<details>
<summary><b>Install using Anaconda Prompt instead (if the .bat files don't work)</b></summary>

Open **Anaconda Prompt** and run:

```bat
cd C:\Users\<you>\Bhoonidhi-Downloader
conda env create -f environment.yml
conda activate bhoonidhi
streamlit run app.py
```

Next time:

```bat
conda activate bhoonidhi
cd C:\Users\<you>\Bhoonidhi-Downloader
streamlit run app.py
```

If Streamlit asks for an email, just press **Enter**.
</details>

<details>
<summary><b>Linux / macOS</b></summary>

```bash
git clone https://github.com/anurag-tm/Bhoonidhi-Downloader.git
cd Bhoonidhi-Downloader
./install.sh     # one time
./run.sh         # every time
```
</details>

<details>
<summary><b>Using pip instead of conda</b></summary>

```bash
pip install -r requirements.txt
streamlit run app.py
```

GDAL is not installed this way, so **"Unzip and clip"** stays off. Everything else works. To enable clipping, install GDAL (easiest: `conda install -c conda-forge gdal`).
</details>

### Optional: check your account first

```bat
conda activate bhoonidhi
python test_connection.py
```

It logs in, lists a few scenes and logs out. If you see *"Everything works"*, the app will work too.

---

## 3. How to use

| Step | What to do |
|---|---|
| **Login** (left sidebar) | Enter your Bhoonidhi user ID and password. Keep *"Log in again automatically"* ticked. Tick *"Remember me on this PC"* if you want automatic login next time. |
| **1. Area of interest** | *Point + size*: enter `latitude, longitude`, choose **Circle** (radius) or **Square** (side length) in km. Or enter a *Bounding box*, or upload a *Shapefile* (`.zip`, or `.shp` + `.shx` + `.dbf` + `.prj` together). |
| **2. Filters** | Pick **Start date** and **End date**, the collections (LISS-IV by default) and the **max cloud cover %**. Click **🔍 Search**. |
| **3. Results** | Each scene shows its **Location** (path / row / subscene), **AOI covered %** and **Cloud %**. Choose scenes with **100 % AOI covered**, and tick the ones you want. |
| **4. Compare** *(optional)* | Pick a **Location**, then 2–4 dates. Choose *Quick-look* to preview before downloading, and turn on **Open comparison viewer**. Pan or zoom one window and the others follow. |
| **5. Download & process** | Check the **download folder** and **Parallel downloads** (1–5). Under **After download** choose: *Unzip* (off = ZIP only), *Clip to my AOI* (off = full scene), *Stack bands*, *Make FCC*, *Make TCC*, *Keep / Delete* the ZIP, and *Keep the unzipped full scene*. Click **⬇️ Download**. Step 1 downloads everything; step 2 does the processing. |
| **6. Process ZIP files already on your PC** *(optional)* | Enter the folder with your ZIPs, choose the same options, and click **⚙️ Process ZIP files**. No login needed. |
| **Finish** | Click **Logout** in the sidebar, then close the black window. |

### Where your files go

```
liss4_downloads/
├── <scene>.zip                  full scene as downloaded (removed if "Delete" is chosen)
├── <scene>/                     unzipped full scene
├── <scene>_FCC.tif              full-scene composites (when "Clip" is off)
├── <scene>_TCC.tif
└── <scene>_clip/                (when "Clip" is on)
    ├── BAND2_clip.tif           each band clipped to your area
    ├── BAND3_clip.tif
    ├── BAND4_clip.tif
    ├── <scene>_stack_clip.tif   all bands in one file (if "Stack" is ticked)
    ├── <scene>_FCC_clip.tif     clipped false colour
    └── <scene>_TCC_clip.tif     clipped true colour
```

Composites are 8-bit RGB GeoTIFFs (2–98 % stretch, 0 = no data, with overviews), ready for QGIS or ArcGIS.

### FCC and TCC: how they are made

LISS-IV has three bands: **B2 Green, B3 Red, B4 NIR**. There is **no blue band**.

| Composite | Red | Green | Blue |
|---|---|---|---|
| **FCC** (standard false colour) | NIR (B4) | Red (B3) | Green (B2) |
| **TCC** (simulated true colour) | Red (B3) | (3·Green + NIR) / 4 | (3·Green − NIR) / 4 |

Because the blue band is missing, the TCC is a *simulated* natural colour. It uses the band combination long used for SPOT, which has the same three bands ([CRISP, NUS: Interpretation of optical images](https://crisp.nus.edu.sg/~research/tutorial/opt_int.htm)). Vegetation looks green and water blue, but colours are approximate. More advanced methods predict the blue band with a regression model ([ISPRS Archives XLII-5, 2018](https://isprs-archives.copernicus.org/articles/XLII-5/643/2018/)).

---

## 4. Good to know

- **Only "online" scenes can be downloaded through the API.** Offline scenes must be ordered on the Bhoonidhi website.
- **The API gives only full scenes.** The app downloads the full scene, then clips it on your PC.
- **Cloud % estimated from quick-looks counts snow and glaciers as cloud.** In winter or over glaciers, set the cloud limit higher and check scenes in the Compare window.
- **Quick-looks are low-resolution** and placed approximately on the map. Downloaded and clipped images are placed exactly, shown as false colour (NIR-Red-Green).
- **Bhoonidhi's limits:** max 5 downloads at once and about 20 logins per hour. Log out when you're done.
- **If downloading stops** (internet cut, PC sleep), click **Download** again: finished scenes are skipped and half-done ones resume.

---

## 5. Problems?

| Message / problem | What to do |
|---|---|
| `401` Session expired | The app logs in again by itself. If it keeps happening, log out and log in. |
| `403` Max sessions | Too many open sessions. Log out on the Bhoonidhi website and wait a few minutes. |
| `404` / *No scenes found* | Widen the dates or the area, or untick *"Online scenes only"*. |
| `412` Too many downloads | Lower *Parallel downloads* to 3 or less. |
| *FCC/TCC skipped: bands 2, 3 and 4 not all found* | The ZIP is not a LISS-IV multispectral product (e.g. LISS-III has B5 too, which is fine; PAN has one band). |
| `429` Rate limit | Wait ~1 hour (too many logins or searches). |
| *Clipping needs GDAL* | Run `install_windows.bat` again, or `conda install -c conda-forge gdal`. |
| Copernicus: *cannot read Sentinel-2 JPEG2000 bands* | Run `install_windows.bat` again (adds `libgdal-jp2openjpeg`). |
| Copernicus: *needs an API client* | Create one in the Copernicus dashboard (see [Copernicus page](#copernicus-page)). |
| *Remember me* is greyed out | `conda install -c conda-forge keyring`, then restart the app. |
| Streamlit asks for an email | Press **Enter** (leave it blank). |
| Logged out after a reload | Update to the latest version. Login now survives page reloads. |
| Page reset, shapefile gone | Update to the latest version and start the app with `run_windows.bat` (or from the app folder). Your last area and results now come back by themselves. |

Still stuck? Open an issue: <https://github.com/anurag-tm/Bhoonidhi-Downloader/issues>

---

## 6. Update to a new version

Download the new ZIP (or run `git pull`), replace the old files, then run `install_windows.bat` once more. Your settings and downloads are kept.

---

## Privacy

- Your password is kept only in the app's memory while it runs.
- With *"Remember me on this PC"* it is saved in **Windows Credential Manager** (or the macOS Keychain), never in a text file. *"Logout and forget me on this PC"* removes it.
- `bhoonidhi_settings.json` (options), `bhoonidhi_last_aoi.json` (your last uploaded area) and `bhoonidhi_last_search.json` (last results) are saved in the app folder on your PC. They never contain your password and are never uploaded anywhere. Delete them to start fresh.
- The app talks only to Bhoonidhi (`bhoonidhi.nrsc.gov.in`) and loads the basemap for the Compare window.

---

## Copernicus page

Open the app as usual, then click **Copernicus** in the left menu.

### One-time setup (free)

1. **Account.** Register at <https://dataspace.copernicus.eu>. Log in on the Copernicus page sidebar (*1 · Your account*). This is needed for **full product downloads**.
2. **API client**, for *ready-made* data. In the [Copernicus dashboard](https://shapps.dataspace.copernicus.eu/dashboard/) go to **User settings → OAuth clients → + Create**, then copy the **Client ID** and **Client secret** into the sidebar (*2 · API client*). The secret is shown only once.

Tick *Remember me* to save both in Windows Credential Manager.

### Two ways to get data

| | **Ready-made for my area** | **Full original products** |
|---|---|---|
| What you get | Only your area, already processed, as GeoTIFF | The complete scene (SAFE/ZIP, often 1 GB+) |
| Processing | Done on Copernicus servers | On your PC (unzip, clip, composites for Sentinel-2) |
| Needs | API client | Account (or API client) |
| Best for | Quick maps, time series, large date ranges | Your own processing in SNAP, QGIS, Python |

**Ready-made products**

| Data | Products |
|---|---|
| Sentinel-2 L2A / L1C (10 m) | True colour, False colour (NIR-Red-Green), SWIR snow/ice (B12-B8A-B4), NDVI, NDSI (snow & glaciers), NDWI (water & glacial lakes), all bands as reflectance, Scene classification (L2A) |
| Sentinel-1 GRD (10 m) | Backscatter in dB (VV+VH or HH+HV), SAR colour composite. Terrain-flattened γ⁰ (best for mountains) or σ⁰, optional speckle filter, ascending/descending pass |
| Copernicus DEM (30 m) | Elevation in metres |

Choose **one image per date** (search, tick dates, preview) or **one mosaic of the whole period**. For Sentinel-2 the mosaic uses the least-cloudy image first, a quick way to get a cloud-free picture of a glacier.

Large areas are split into several requests automatically and joined into one file. Bigger pixels (20–100 m) use less of your free monthly processing allowance; check what's left on the dashboard.

**Full products:** Sentinel-2 L2A/L1C, Sentinel-1 GRD/SLC/RTC, Sentinel-3 OLCI/SLSTR, Sentinel-5P, Copernicus DEM, the Landsat copies on CDSE, global mosaics, or any other CDSE collection (*Other collection…*). Up to 4 downloads at once, with resume. For Sentinel-2 the app can unzip, clip to your area and make TCC, FCC, SWIR, NDVI, NDSI and NDWI. Reflectance offsets for processing baseline 04.00+ (2022 onward) are handled. Other products are unzipped only; for example, SAR GRD/SLC needs SNAP for calibration and terrain correction.

### Output files

- Colour images: 8-bit RGB GeoTIFF, 0 = no data.
- Indices, bands, SAR dB, DEM: Float32 GeoTIFF, -9999 = no data.
- All files are in the local UTM zone and open directly in QGIS / ArcGIS.

---

## License

[MIT](LICENSE) © 2026 Anurag Sharma, Glaciology Lab, Department of Remote Sensing & GIS, University of Jammu.

Data © NRSC / ISRO, provided through Bhoonidhi.
