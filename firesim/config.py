"""Configuration for a single fire-spread simulation run."""

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


@dataclass
class SimulationConfig:
    """All user-facing inputs plus MVP assumptions for one simulation."""

    # --- User inputs ---
    aoi_bounds: tuple[float, float, float, float]  # (west, south, east, north) lon/lat
    ignition_lonlat: tuple[float, float]           # (lon, lat) of fire departure
    ignition_date: str                             # "YYYY-MM-DD" (historical, GRIDMET coverage)
    projection_days: int                           # projection horizon in days

    # --- Earth Engine (set EE_PROJECT in .env) ---
    ee_project: str = field(default_factory=lambda: os.environ.get("EE_PROJECT", ""), repr=False)

    # --- Weather backend ---
    weather_source: str = "gridmet"           # "gridmet" | "weathernext"
    weathernext_stat: str = "mean"            # mean | p10 | p25 | p50 | p75 | p90
    weathernext_step_hours: int = 6           # sample WeatherNext every N hours (fewer bands)
    weathernext_init_time: str | None = None  # override init run (else auto-select)
    allow_gridmet_fallback: bool = True       # fall back to GRIDMET if WeatherNext lacks coverage
    constrain_to_land: bool = True            # water/ice -> non-burnable + masked on the map
    water_fraction_threshold: float = 0.25    # flag a cell as water if >= this fraction is water
    water_buffer_cells: int = 2               # dilate the water mask by N cells (impervious boundary)

    # --- Grid / performance ---
    start_hour: int = 12        # hour of day (UTC) the fire starts
    max_pixels: int = 160       # longest AOI side in pixels (controls resolution/cost)
    min_scale_m: float = 30.0   # floor on cell size in meters
    gsd_m: float | None = field(default=None, kw_only=True)  # explicit scale; overrides grid limits

    # --- Data cache (reuse fetched layers across runs; None disables) ---
    cache_dir: str | None = None

    # --- Live fuel moisture constants (kg moisture / kg ovendry) — MVP ---
    live_herbaceous: float = 0.9
    live_woody: float = 0.6
    foliar: float = 1.0

    # --- Canopy constants (surface-fire MVP; 0 disables crown fire) ---
    canopy_cover: float = 0.0
    canopy_height: float = 0.0
    canopy_base_height: float = 0.0
    canopy_bulk_density: float = 0.0
