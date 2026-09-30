"""Assemble pyretechnics inputs, run the spread engine, and summarize results."""

import numpy as np
import pyretechnics.eulerian_level_set as els
from pyretechnics.space_time_cube import SpaceTimeCube

from . import gee, landmask, physics, weather

DAY_MINUTES = 1440.0


def lonlat_to_rc(lon, lat, bounds, rows, cols):
    """Convert a lon/lat point to a (row, col) cell index in the north-up AOI grid."""
    west, south, east, north = bounds
    col = int((lon - west) / (east - west) * cols)
    row = int((north - lat) / (north - south) * rows)
    return (min(max(row, 0), rows - 1), min(max(col, 0), cols - 1))


def _crop(arr, rows, cols):
    return arr[..., :rows, :cols]


def build_inputs(config, store=None):
    """Fetch (or load cached) every layer and wrap them as SpaceTimeCubes.

    Pass a `cache.DataStore` to reuse previously fetched static/weather layers; on a miss the
    layers are fetched and persisted so later runs (e.g. a different ignition point) skip the
    download.
    """
    static, weather_stack = fetch_layers(config, store)
    return assemble_inputs(config, static, weather_stack)


def fetch_layers(config, store=None):
    """Return (static dict, WeatherStack), loading from and populating `store` when given.

    Earth Engine is initialized lazily: a fully cached AOI/date needs neither EE auth nor a
    network call, so repeated runs at new ignition points stay offline and fast.
    """
    scale = gee.compute_scale(config.aoi_bounds, config.max_pixels, config.min_scale_m, config.gsd_m)
    region_state = {"region": None, "ready": False}  # EE initialized + region built on first fetch

    def ensure_region():
        if not region_state["ready"]:
            gee.initialize_ee(config.ee_project)
            region_state["region"] = gee.aoi_geometry(config.aoi_bounds)
            region_state["ready"] = True
        return region_state["region"]

    static = store.load_static(config) if store else None
    if static is None:
        static = fetch_static(config, ensure_region(), scale)
        if store:
            store.save_static(config, static)

    weather_stack = store.load_weather(config) if store else None
    if weather_stack is None:
        weather_stack = weather.get_weather(config, ensure_region(), scale)
        if store:
            store.save_weather(config, weather_stack)

    return static, weather_stack


def fetch_static(config, region, scale):
    """Fetch the date-independent layers: slope, aspect, landcover, water."""
    slope, aspect = gee.fetch_topography(region, scale)
    landcover = gee.fetch_landcover(region, scale)
    water = gee.fetch_water_mask(region, scale, config.water_fraction_threshold)
    return {"slope": slope, "aspect": aspect, "landcover": landcover, "water": water}


def assemble_inputs(config, static, weather_stack):
    """Align, mask, and wrap fetched layers into pyretechnics SpaceTimeCubes + metadata."""
    scale = gee.compute_scale(config.aoi_bounds, config.max_pixels, config.min_scale_m, config.gsd_m)
    slope, aspect = static["slope"], static["aspect"]
    fuel_model = physics.nlcd_to_fuel_model(static["landcover"])
    water = static["water"]

    # Align all layers to a common grid (downloads can differ by a pixel).
    sample = next(iter(weather_stack.cubes.values()))
    rows = min(slope.shape[-2], fuel_model.shape[-2], sample.shape[-2])
    cols = min(slope.shape[-1], fuel_model.shape[-1], sample.shape[-1])
    bands = sample.shape[0]
    cube_shape = (bands, rows, cols)

    fuel_model = _crop(fuel_model, rows, cols)
    non_land = water[:rows, :cols]
    if config.constrain_to_land:
        non_land = landmask.buffer_mask(non_land, config.water_buffer_cells)
        fuel_model = landmask.enforce_land_constraint(fuel_model, non_land)

    def const(value):
        return np.full((rows, cols), value, dtype="float32")

    arrays = {
        "slope": _crop(slope, rows, cols),
        "aspect": _crop(aspect, rows, cols),
        "fuel_model": fuel_model,
        "canopy_cover": const(config.canopy_cover),
        "canopy_height": const(config.canopy_height),
        "canopy_base_height": const(config.canopy_base_height),
        "canopy_bulk_density": const(config.canopy_bulk_density),
        "fuel_moisture_live_herbaceous": const(config.live_herbaceous),
        "fuel_moisture_live_woody": const(config.live_woody),
        "foliar_moisture": const(config.foliar),
    }
    for name, cube in weather_stack.cubes.items():
        arrays[name] = _crop(cube, rows, cols)

    space_time_cubes = {
        name: SpaceTimeCube(cube_shape, np.ascontiguousarray(arr, dtype="float32"))
        for name, arr in arrays.items()
    }
    meta = {
        "scale": scale,
        "cube_shape": cube_shape,
        "rows": rows,
        "cols": cols,
        "bands": bands,
        "band_duration_min": weather_stack.band_duration_min,
        "start_minutes": weather_stack.start_minutes,
        "weather_source": weather_stack.source,
        "non_land_mask": non_land if config.constrain_to_land else None,
    }
    return space_time_cubes, meta


def run_simulation(config, store=None):
    """Build inputs, spread the fire, and return matrices + metadata + stats."""
    space_time_cubes, meta = build_inputs(config, store)
    ignition_rc = lonlat_to_rc(*config.ignition_lonlat, config.aoi_bounds, meta["rows"], meta["cols"])

    spread_state = els.SpreadState(meta["cube_shape"]).ignite_cell(ignition_rc)
    cube_resolution = (meta["band_duration_min"], meta["scale"], meta["scale"])
    result = els.spread_fire_with_phi_field(
        space_time_cubes,
        spread_state,
        cube_resolution,
        start_time=meta["start_minutes"],
        max_duration=config.projection_days * DAY_MINUTES,
    )
    matrices = result["spread_state"].get_full_matrices()
    if meta["non_land_mask"] is not None:
        matrices["time_of_arrival"][meta["non_land_mask"]] = np.nan
    stats = compute_stats(matrices, meta["scale"], result)
    return {"matrices": matrices, "meta": meta, "stats": stats, "ignition_rc": ignition_rc}


def compute_stats(matrices, scale, result):
    """Summarize a spread result into a small dictionary of headline numbers."""
    burned = matrices["fire_type"] > 0
    n_burned = int(np.count_nonzero(burned))
    cell_ha = (scale * scale) / 1e4
    flame = matrices["flame_length"][burned]
    spread = matrices["spread_rate"][burned]
    intensity = matrices["fireline_intensity"][burned]
    return {
        "burned_cells": n_burned,
        "burned_hectares": n_burned * cell_ha,
        "burned_acres": n_burned * cell_ha * 2.47105,
        "max_flame_length_m": float(np.nanmax(flame)) if n_burned else 0.0,
        "mean_spread_rate_m_min": float(np.nanmean(spread)) if n_burned else 0.0,
        "max_fireline_intensity_kw_m": float(np.nanmax(intensity)) if n_burned else 0.0,
        "mean_fireline_intensity_kw_m": float(np.nanmean(intensity)) if n_burned else 0.0,
        "severity_class_cells": _severity_class_counts(flame),
        "stop_condition": result["stop_condition"],
        "cell_size_m": scale,
    }


def _severity_class_counts(flame_length):
    """Count burned cells per flame-length severity class (1-4)."""
    from .raster import SEVERITY_BREAKS_M

    classes = np.digitize(flame_length, SEVERITY_BREAKS_M) + 1
    return {cls: int(np.count_nonzero(classes == cls)) for cls in (1, 2, 3, 4)}
