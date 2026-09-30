# Fire-Spread Simulation — Implementation Overview

End-to-end description of the wildfire propagation prototype in this repository: the ambition,
the physical model, the data sources, the software architecture, and the current MVP scope.

For the detailed per-input source analysis and gap assessment, see
[pyretechnics_weathernext3_inputs.md](pyretechnics_weathernext3_inputs.md). This document is the
consolidated implementation reference.

---

## 1. Ambition

Let a user explore how a wildfire could evolve from a chosen ignition, driven entirely by
open geospatial data. The user provides four inputs:

1. **Area of Interest** — a rectangle (US).
2. **Ignition point** — a lon/lat where the fire departs.
3. **Ignition date** — when it starts.
4. **Projection period** — how many days forward to simulate.

The system then retrieves the terrain, fuels, and weather needed to initialize the model, runs
the fire-spread engine over the period, and lets the user explore the burned-area evolution on
an interactive map plus summary charts.

---

## 2. The model — `pyretechnics`

Fire behavior is computed by [`pyretechnics`](https://github.com/pyregence/pyretechnics), an
operational-grade surface/crown fire library (Rothermel surface spread + ELMFIRE Eulerian
level-set propagation). It consumes a dictionary of aligned `SpaceTimeCube` layers and returns
per-cell `time_of_arrival`, `fire_type`, `spread_rate`, `fireline_intensity`, and `flame_length`.

**Required inputs** (all wrapped as SpaceTimeCubes):

| Group | Layers |
| --- | --- |
| Topography | `slope` (rise/run), `aspect` (deg CW from N) |
| Fuels / canopy | `fuel_model` (Scott & Burgan 40), `canopy_cover`, `canopy_height`, `canopy_base_height`, `canopy_bulk_density` |
| Weather | `wind_speed_10m` (km/hr), `upwind_direction` (deg CW from N), `temperature` (°C) |
| Dead fuel moisture | `fuel_moisture_dead_1hr` / `_10hr` / `_100hr` (kg/kg) |
| Live fuel moisture | `fuel_moisture_live_herbaceous`, `_live_woody`, `foliar_moisture` (kg/kg) |

**Non-burnable rule:** fuel-model codes **91–99** never spread. Water is forced to `98`, which is
how propagation is constrained to land (see §5).

---

## 3. Data sources (US)

All raster inputs are fetched from the Google Earth Engine catalog (project set via the
`EE_PROJECT` variable in `.env`) and downloaded to NumPy via GeoTIFF (`getDownloadURL`).

| Purpose | Earth Engine source | Status |
| --- | --- | --- |
| Elevation → slope/aspect | `USGS/SRTMGL1_003` + `ee.Terrain` | 🟢 direct |
| Fuel model | `USGS/NLCD_RELEASES/2019_REL/NLCD/2019` land cover → Scott & Burgan crosswalk | 🟡 processed |
| Water/land mask | Same NLCD (classes 11, 12), aggregated as a fraction | 🟡 processed |
| Weather (default) | `IDAHO_EPSCOR/GRIDMET` — daily, CONUS, 1979→present, incl. `fm100` | 🟢 direct |
| Weather (preferred) | **WeatherNext 3** `projects/gcp-public-data-weathernext/assets/weathernext_3_0_0_0p1deg` — hourly forecast | 🟢 direct |
| Dead fuel moisture 1/10 hr | Derived (Simard EMC) from weather | 🟡 processed |
| Live fuel moisture, canopy | Seasonal constants (MVP) | 🔴 assumption |

---

## 4. Weather backends

Two interchangeable providers sit behind a single interface (`firesim/weather.py`, `WeatherStack`):

### GRIDMET (default, `weather_source="gridmet"`)
- Daily, CONUS, historical. Band duration = 1440 min.
- Supplies wind (`vs`, `th`), temperature (`tmmn`/`tmmx`), humidity (`rmin`/`rmax`), and
  **100 hr dead fuel moisture** directly (`fm100`). 1 hr / 10 hr from EMC.

### WeatherNext 3 (preferred, `weather_source="weathernext"`)
- Hourly ensemble **forecast**; enables projecting fires into the future.
- Requires an approved WeatherNext EE data request.
- No dead-fuel-moisture product → **all** dead FM (1/10/100 hr) derived from EMC using
  temperature + RH (RH from 2 m dewpoint via Magnus).
- **Init selection:** the latest run at/before the ignition time whose forecasts cover the
  window (prefers the 6-hourly 00/06/12/18Z cycles with a 15-day horizon).
- **Ensemble statistic** configurable (`weathernext_stat`, default `mean`).
- **Download size control:** forecasts are sampled every `weathernext_step_hours` (default 6 h)
  and downloaded in time-chunks to stay under Earth Engine's request-size cap. Band duration =
  `step_hours × 60` min.
- **Automatic fallback:** if no run covers the date, the pipeline falls back to GRIDMET
  (`allow_gridmet_fallback`), so arbitrary historical dates still work.

---

## 5. Constraining spread to land

Open water must not burn. At the coarse simulation grid, nearest-neighbor downsampling of 30 m
NLCD can drop the water class, so a naive class lookup leaks fire across lakes. Two mitigations:

1. **Conservative detection** (`gee.fetch_water_mask`): water is aggregated to the sim resolution
   as a **fraction** (`reduceResolution` mean) and thresholded (`water_fraction_threshold`,
   default 0.25), so thin shorelines are never lost.
2. **Impervious boundary** (`landmask.buffer_mask`): the water mask is dilated by
   `water_buffer_cells` (default 2) to form a hard no-flow ring the level-set cannot cross.

Water cells are set to non-burnable fuel `98` and blanked in the map overlay (their
`time_of_arrival` is `NaN`).

---

## 6. Derived physics (`firesim/physics.py`)

| Quantity | Method |
| --- | --- |
| Wind speed (km/hr) | GRIDMET `vs × 3.6`, or `√(u²+v²) × 3.6` from WeatherNext components |
| Upwind direction | GRIDMET `th`, or meteorological FROM bearing `(270 − atan2(v,u))` |
| Temperature (°C) | Kelvin − 273.15 |
| Relative humidity | GRIDMET `(rmin+rmax)/2`, or Magnus formula from temperature + dewpoint |
| Dead fuel moisture | Simard (1968) equilibrium moisture content; 100 hr from `fm100` when available |
| Fuel model | NLCD class → Scott & Burgan 40 crosswalk |

---

## 7. Software architecture

Logic lives in the `firesim` package; the notebooks only collect user inputs and display results.

```
firesim/
  __init__.py     run(config) entry point + exports
  config.py       SimulationConfig dataclass (all inputs + assumptions)
  gee.py          Earth Engine init, geometry, downloads, layer/weather fetchers
  physics.py      unit conversions, Simard EMC, NLCD→fuel crosswalk
  weather.py      WeatherStack + GRIDMET / WeatherNext providers + fallback
  landmask.py     conservative water mask + impervious buffer + enforcement
  model.py        assemble SpaceTimeCubes, run pyretechnics, compute stats
  viz.py          folium map (aerial basemap) + matplotlib charts
notebooks/
  fire_spread_explorer.ipynb              GRIDMET driver (thin)
  fire_spread_explorer_weathernext.ipynb  WeatherNext driver (thin)
```

**Flow:** `run(config)` → `initialize_ee` → `build_inputs` (fetch topo, fuels, water, weather →
align → mask → SpaceTimeCubes) → `spread_fire_with_phi_field` → `get_full_matrices` →
`compute_stats`. Visualization is separate (`viz.build_map`, `viz.plot_charts`).

---

## 8. Outputs & visualization

- **Stats:** burned hectares/acres, max flame length, mean spread rate, stop condition, cell size.
- **Interactive map** (`viz.build_map`): Esri **aerial imagery** basemap, AOI rectangle, ignition
  marker, a time-of-arrival overlay colored by arrival day, and toggleable daily perimeters.
- **Charts** (`viz.plot_charts`): cumulative burned area vs time, flame-length distribution,
  spread rate vs arrival time.

---

## 9. Configuration reference (`SimulationConfig`)

| Field | Default | Meaning |
| --- | --- | --- |
| `aoi_bounds` | — | (west, south, east, north) lon/lat |
| `ignition_lonlat` | — | (lon, lat) of fire departure |
| `ignition_date` | — | "YYYY-MM-DD" |
| `projection_days` | — | projection horizon (days) |
| `ee_project` | `$EE_PROJECT` | Earth Engine project (read from `.env`) |
| `weather_source` | `gridmet` | `gridmet` \| `weathernext` |
| `weathernext_stat` | `mean` | ensemble statistic |
| `weathernext_step_hours` | `6` | forecast sampling step (fewer bands = smaller download) |
| `weathernext_init_time` | `None` | override init run |
| `allow_gridmet_fallback` | `True` | fall back to GRIDMET if WeatherNext lacks coverage |
| `constrain_to_land` | `True` | force water/ice non-burnable + masked |
| `water_fraction_threshold` | `0.25` | fraction of a cell that counts as water |
| `water_buffer_cells` | `2` | impervious-boundary dilation width |
| `start_hour` | `12` | hour (UTC) the fire starts |
| `max_pixels` | `160` | longest AOI side in pixels (resolution/cost) |
| `min_scale_m` | `30.0` | floor on cell size (m) |
| `gsd_m` | `None` | explicit scale (m), overriding `max_pixels` and `min_scale_m`; CLI: `--gsd` |
| `live_herbaceous` / `live_woody` / `foliar` | `0.9` / `0.6` / `1.0` | live fuel moisture constants |
| `canopy_cover` / `canopy_height` / `canopy_base_height` / `canopy_bulk_density` | `0.0` | canopy constants (0 = surface fire only) |

---

## 10. MVP scope & limitations

- **Surface fire only** — canopy layers are 0, so no crown fire.
- **Live fuel moisture** and **foliar moisture** are seasonal constants (the remaining 🔴 gap).
- **Dead fuel moisture** is modeled from current weather (no multi-week spin-up of the 100 hr class
  in the WeatherNext path).
- **Resolution** defaults to automatic sizing using `max_pixels`; `--gsd` sets an explicit scale. Weather is far coarser than fuels/topo. The EPSG:4326 grid does not guarantee square ground dimensions.
- **US only** — relies on NLCD/GRIDMET/WeatherNext CONUS coverage.

---

## 11. Extension roadmap

- Enable **crown fire** with LANDFIRE canopy layers (CC/CH/CBH/CBD) ingested from the LANDFIRE
  program (not in the EE core catalog).
- Replace fuel-model crosswalk with **LANDFIRE FBFM40** for fidelity.
- Source **live fuel moisture** from an LFMC product (NFMD, satellite LFMC) instead of constants.
- Add **spotting** (firebrands) and dead-fuel-moisture **spin-up** history.
- Higher spatial resolution and hourly weather chunk streaming for large AOIs.

---

## 12. Running it

1. `.venv` with dependencies from [../requirements.txt](../requirements.txt); kernel
   `Python (pyrotechnics-tests .venv)`. Create a `.env` with `EE_PROJECT=<your-project>`.
2. Open a notebook, edit only the scenario cell, run top to bottom.
3. Complete the Earth Engine auth prompt for your `EE_PROJECT` on first run.
   - GRIDMET notebook for historical/near-real-time dates.
   - WeatherNext notebook for forecast-driven runs (auto-falls back to GRIDMET when uncovered).
