# pyroSim — Wildfire Spread CLI

`pyroSim` simulates how a wildfire could spread from a chosen ignition point using open
geospatial data ([pyretechnics](https://github.com/pyregence/pyretechnics) + Google Earth
Engine). It saves a single **EPSG:4326 GeoTIFF** where each pixel is the number of **hours
before that cell burns** (`0` at the ignition cell, `-999` where it never burns).

## Prerequisites

- Python 3.10+
- A Google **Earth Engine** account with an approved project (and a WeatherNext data request if
  you want the `weathernext` forecast backend).

## 1. Install requirements

The project ships an editable install that also registers the `pyroSim` command. Pick one:

### Option A — uv (recommended)

```bash
uv venv                       # create .venv (skip if it already exists)
uv pip install -e .           # install deps from requirements.txt + the pyroSim command
```

### Option B — pip

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

Either option reads dependencies from [requirements.txt](requirements.txt) and installs the
`pyroSim` console script into the environment.

## 2. Configure Earth Engine

Create a `.env` file in the repo root with your Earth Engine project id:

```
EE_PROJECT=your-earth-engine-project
```

On the first run, Earth Engine will prompt you to authenticate in the browser.

## 3. Run the CLI

```bash
pyroSim run \
  --aoi-bounds -120.55 39.00 -120.30 39.20 \
  --ignition-lonlat -120.45 39.10 \
  --ignition-date 2026-09-10 \
  --projection-days 5 \
  --weather-source weathernext \
  --output-name tahoe_fire.tif \
  --intensity \
  --severity
```

If you used the pip venv, activate it first (`source .venv/bin/activate`). With uv you can also
run it via `uv run pyroSim run ...` or `.venv/bin/pyroSim run ...`. The equivalent module form is
`python -m firesim run ...`.

### Arguments

| Argument | Required | Description |
| --- | --- | --- |
| `--aoi-bounds WEST SOUTH EAST NORTH` | yes | Area of interest as lon/lat (`west south east north`). |
| `--ignition-lonlat LON LAT` | yes | Ignition point; must fall inside the AOI. |
| `--ignition-date YYYY-MM-DD` | yes | Date the fire departs. |
| `--projection-days N` | yes | Projection horizon in days. |
| `--weather-source {gridmet,weathernext}` | no | Weather backend (default: `gridmet`). |
| `--gsd METERS` | no | Explicit grid scale in meters, e.g. `--gsd 30`. Mutually exclusive with `--max-pixels`. |
| `--max-pixels N` | no | Approximate cell count along the longest AOI side; derives the scale with a 30 m minimum. Default: `160` when `--gsd` is omitted. |
| `--output-name NAME`, `-o NAME` | yes | Output GeoTIFF filename (a `.tif` extension is added if missing). |
| `--intensity` | no | Also save a fireline-intensity GeoTIFF as `<name>_intensity.tif`. |
| `--severity` | no | Also save a flame-length severity-class GeoTIFF as `<name>_severity.tif`. |

Run `pyroSim run --help` for the full reference.

### Choosing the grid resolution

Both `run` and `fetch` accept `--gsd` or `--max-pixels`. Use `--gsd 30` to request a
30-meter grid scale regardless of the AOI size:

```bash
pyroSim run \
  --aoi-bounds -120.55 39.00 -120.30 39.20 \
  --ignition-lonlat -120.45 39.10 \
  --ignition-date 2026-09-10 --projection-days 5 \
  --weather-source weathernext --gsd 30 \
  --cache-dir ./tahoe_cache -o tahoe_30m.tif
```

Alternatively, `--max-pixels 800` derives the scale from the AOI dimensions, keeping it
at least 30 m. With neither option, the existing 160-cell default applies (about 138 m
for this sample area). Explicit GSD bypasses both this automatic sizing and its 30 m floor;
requesting finer sampling does not add detail to the source data.

Use the same grid option for `fetch` and subsequent `run` commands. Changing it uses a
separate cache entry and requires fetching inputs again, including Earth Engine setup
and authentication on a cache miss. The bundled Tahoe cache remains usable with the defaults.

GSD is passed as the Earth Engine scale in meters and the simulation's cell size.
Downloads and exports still use EPSG:4326 (angular coordinates); this option does not
introduce a projected grid with constant square ground dimensions.

## Outputs

All products are single-band GeoTIFFs (`EPSG:4326`) sharing the same AOI grid.

### Hours before burn — `<name>.tif` (always)

- **Pixel value** = hours between ignition and when the cell burns.
- **`0`** = the ignition cell.
- A cell that burns two weather cycles later = `2 × weather step` hours (e.g. 12 h for a
  6‑hourly WeatherNext step, 48 h for daily GRIDMET).
- **`-999`** = cells that never burn (including masked water); this is the nodata value.

### Fireline intensity — `<name>_intensity.tif` (with `--intensity`)

- **Pixel value** = Byram's fireline intensity in **kW/m** for each burned cell.
- **`-999`** = cells that never burn (nodata).

### Severity class — `<name>_severity.tif` (with `--severity`)

Modeled fire-behavior severity from flame-length classes (Fire Characteristics Chart), **not**
satellite burn severity (dNBR). Since the MVP is surface-fire only, values reflect surface
intensity.

| Value | Class | Flame length |
| --- | --- | --- |
| `0` | Unburned (nodata) | — |
| `1` | Low | < 1.2 m |
| `2` | Moderate | 1.2–2.4 m |
| `3` | High | 2.4–3.4 m |
| `4` | Very high | > 3.4 m |

## Reusing fetched data (cache)

Earth Engine downloads are the slow part of a run. Pass `--cache-dir DIR` to store the fetched
layers on disk and reuse them. The cache is split into two groups:

- **static** — slope, aspect, land cover, water (depend only on the AOI grid).
- **weather** — the weather cubes (depend on the AOI grid *and* the date/backend).

So changing only the **ignition point** reuses everything (no download); changing only the
**date** reuses the static layers and refetches just the weather.

### Pre-fetch, then run several ignition points

```bash
# Download once for an AOI/date into ./tahoe_cache
pyroSim fetch \
  --aoi-bounds -120.55 39.00 -120.30 39.20 \
  --ignition-date 2026-09-10 \
  --projection-days 5 \
  --weather-source weathernext \
  --cache-dir ./tahoe_cache

# Run different ignition points with no re-download
pyroSim run --aoi-bounds -120.55 39.00 -120.30 39.20 --ignition-lonlat -120.45 39.10 \
  --ignition-date 2026-09-10 --projection-days 5 --weather-source weathernext \
  --cache-dir ./tahoe_cache -o point_a.tif

pyroSim run --aoi-bounds -120.55 39.00 -120.30 39.20 --ignition-lonlat -120.40 39.05 \
  --ignition-date 2026-09-10 --projection-days 5 --weather-source weathernext \
  --cache-dir ./tahoe_cache -o point_b.tif
```

`fetch` accepts `--static-only` to download just the date-independent layers. `run` also
populates the cache on a miss, so the first `run` alone is enough to speed up later ones — the
explicit `fetch` step is optional. The AOI/date/grid parameters must match for a cache hit;
delete the cache directory to invalidate it.

Earth Engine is initialized **lazily**, only when a layer is missing from the cache. A `run`
whose AOI/date is fully cached needs no EE authentication or network call at all, so batches of
runs at different ignition points stay offline and fast.
