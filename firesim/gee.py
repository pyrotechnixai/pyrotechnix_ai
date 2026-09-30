"""Earth Engine access: init, geometry, and raster downloads to NumPy arrays.

Only US-covered public datasets are used (see docs/pyretechnics_weathernext3_inputs.md):
- Topography: USGS/SRTMGL1_003
- Land cover (fuel model basis): USGS/NLCD_RELEASES/2019_REL/NLCD/2019
- Weather + 100 hr dead fuel moisture: IDAHO_EPSCOR/GRIDMET
"""

from datetime import datetime, timedelta, timezone
from math import cos, isfinite, radians

import ee
import numpy as np
import requests
from rasterio.io import MemoryFile

GRIDMET_BANDS = ["vs", "th", "tmmx", "tmmn", "rmin", "rmax", "fm100"]


def initialize_ee(project: str) -> None:
    """Initialize Earth Engine, authenticating interactively on first use."""
    if not project:
        raise ValueError("No Earth Engine project set. Add EE_PROJECT to your .env file.")
    try:
        ee.Initialize(project=project)
    except Exception:
        ee.Authenticate()
        ee.Initialize(project=project)


def compute_scale(bounds, max_pixels: int, min_scale_m: float, gsd_m: float | None = None) -> float:
    """Use explicit GSD, or size the grid to ~max_pixels along its longest side."""
    if gsd_m is not None:
        if not isfinite(gsd_m) or gsd_m <= 0:
            raise ValueError("gsd must be a finite number greater than zero.")
        return float(gsd_m)
    if not isfinite(max_pixels) or max_pixels <= 0:
        raise ValueError("max-pixels must be greater than zero.")
    west, south, east, north = bounds
    lat = (south + north) / 2.0
    width_m = (east - west) * 111320.0 * cos(radians(lat))
    height_m = (north - south) * 110540.0
    return max(max(width_m, height_m) / max_pixels, min_scale_m)


def aoi_geometry(bounds) -> "ee.Geometry":
    west, south, east, north = bounds
    return ee.Geometry.Rectangle([west, south, east, north])


def download_image(image, region, scale) -> np.ndarray:
    """Download an EE image as a GeoTIFF and return a (bands, rows, cols) float32 array.

    All layers are pinned to the same geographic grid (EPSG:4326) so the fuel-model cube and
    the water mask align cell-for-cell — without this, water is written as non-burnable at the
    wrong locations and the engine spreads across lakes.
    """
    url = image.getDownloadURL(
        {"region": region, "scale": scale, "crs": "EPSG:4326", "format": "GEO_TIFF"}
    )
    response = requests.get(url, timeout=300)
    response.raise_for_status()
    with MemoryFile(response.content) as memfile, memfile.open() as dataset:
        return dataset.read().astype("float32")


def fetch_topography(region, scale):
    """Return (slope as rise/run, aspect in degrees CW from North)."""
    dem = ee.Image("USGS/SRTMGL1_003")
    terrain = ee.Terrain.products(dem)
    arr = download_image(terrain.select(["slope", "aspect"]), region, scale)
    slope_deg, aspect = arr[0], arr[1]
    slope = np.tan(np.radians(slope_deg))
    return slope, aspect


def fetch_landcover(region, scale) -> np.ndarray:
    """Return the NLCD 2019 land cover class raster (rows, cols)."""
    nlcd = ee.Image("USGS/NLCD_RELEASES/2019_REL/NLCD/2019").select("landcover")
    return download_image(nlcd, region, scale)[0]


def fetch_water_mask(region, scale, threshold=0.25) -> np.ndarray:
    """Boolean water mask, computed conservatively so any cell containing water is flagged.

    NLCD water/ice is aggregated to the sim resolution as a *fraction* (so coarse downsampling
    cannot drop thin shorelines), then thresholded.
    """
    nlcd = ee.Image("USGS/NLCD_RELEASES/2019_REL/NLCD/2019").select("landcover")
    is_water = nlcd.eq(11).Or(nlcd.eq(12))
    fraction = (
        is_water.reduceResolution(reducer=ee.Reducer.mean(), maxPixels=1024)
        .reproject(crs="EPSG:4326", scale=scale)
    )
    return download_image(fraction.unmask(0).clip(region), region, scale)[0] > threshold


def fetch_gridmet_stack(ignition_date: str, num_days: int, region, scale) -> dict:
    """Return {band: (days, rows, cols)} for the GRIDMET weather bands over the horizon."""
    start = ee.Date(ignition_date)
    end = start.advance(num_days, "day")
    collection = ee.ImageCollection("IDAHO_EPSCOR/GRIDMET").filterDate(start, end)
    return {
        band: download_image(collection.select(band).toBands(), region, scale)
        for band in GRIDMET_BANDS
    }


# --- WeatherNext 3 ---
WEATHERNEXT_GRIDDED = "projects/gcp-public-data-weathernext/assets/weathernext_3_0_0_0p1deg"
WEATHERNEXT_STATIONS = "projects/gcp-public-data-weathernext/assets/weathernext_3_0_0_0p05deg"


def _weathernext_collection():
    return ee.ImageCollection(WEATHERNEXT_GRIDDED)


def weathernext_band_names(region):
    """List bands on the WeatherNext gridded collection (to verify variable/stat names)."""
    return _weathernext_collection().filterBounds(region).first().bandNames().getInfo()


def _parse_iso(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _sim_start_date(ignition_date, start_hour):
    return datetime.fromisoformat(ignition_date).replace(tzinfo=timezone.utc) + timedelta(hours=start_hour)


def select_weathernext_init(ignition_date, start_hour, horizon_hours, region):
    """Latest init run (<= sim start) whose forecasts cover the window, or None."""
    sim_start = _sim_start_date(ignition_date, start_hour)
    start = ee.Date(sim_start.isoformat())
    end = start.advance(horizon_hours, "hour")
    covering = _weathernext_collection().filterBounds(region).filterDate(start, end)
    inits = covering.aggregate_array("start_time").distinct().getInfo()
    candidates = [i for i in inits if _parse_iso(i) <= sim_start]
    if not candidates:
        return None
    six_hourly = [i for i in candidates if _parse_iso(i).hour in (0, 6, 12, 18)]
    return max(six_hourly or candidates, key=_parse_iso)


def fetch_weathernext_stack(init_time, ignition_date, start_hour, horizon_hours, stat, step_hours, region, scale):
    """Return {t2m,u,v,dew: (steps, rows, cols)} for one init run, sampled every step_hours."""
    sim_start = _sim_start_date(ignition_date, start_hour)
    start = ee.Date(sim_start.isoformat())
    end = start.advance(horizon_hours, "hour")
    run = _weathernext_collection().filter(ee.Filter.eq("start_time", init_time))
    valid = run.filterDate(start, end).sort("forecast_hour")
    size = valid.size()
    valid_list = valid.toList(size)
    indices = ee.List.sequence(0, size.subtract(1), step_hours)
    sampled = ee.ImageCollection(indices.map(lambda i: ee.Image(valid_list.get(ee.Number(i).toInt()))))
    bands = {
        "t2m": f"temperature_2m_{stat}",
        "u": f"u_component_of_wind_10m_{stat}",
        "v": f"v_component_of_wind_10m_{stat}",
        "dew": f"dewpoint_temperature_2m_{stat}",
    }
    return {key: _download_bands_chunked(sampled, band, region, scale) for key, band in bands.items()}


def _download_bands_chunked(collection, band, region, scale, chunk=24):
    """Download a time-band image in chunks to stay under Earth Engine's request-size cap."""
    count = collection.size().getInfo()
    image_list = collection.toList(count)
    parts = []
    for offset in range(0, count, chunk):
        subset = ee.ImageCollection(image_list.slice(offset, min(offset + chunk, count)))
        parts.append(download_image(subset.select(band).toBands().clip(region), region, scale))
    return np.concatenate(parts, axis=0)
