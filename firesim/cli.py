"""Command-line interface for firesim.

Usage::

    pyroSim run \
        --aoi-bounds -120.55 39.00 -120.30 39.20 \
        --ignition-lonlat -120.45 39.10 \
        --ignition-date 2026-09-10 \
        --projection-days 5 \
        --weather-source weathernext \
        --output-name tahoe_fire.tif

Saves a single EPSG:4326 GeoTIFF where each pixel is the number of hours before that cell
burns (0 at the ignition cell, -999 where it never burns). Optionally also saves sibling
intensity (kW/m) and flame-length severity-class GeoTIFFs.

Pass `--cache-dir DIR` to reuse fetched Earth Engine data across runs. `pyroSim fetch` can
pre-populate that cache; a later `run` with the same AOI/date only needs a new ignition point.
"""

import argparse
import math
import pathlib
import sys

from . import gee, model, raster, weather
from .cache import DataStore
from .config import SimulationConfig
from .gee import initialize_ee
from .model import run_simulation


def _positive_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    return number


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be an integer greater than zero")
    return number


def _add_data_args(sub) -> None:
    """AOI/date/weather arguments shared by `run` and `fetch`."""
    sub.add_argument(
        "--aoi-bounds",
        type=float,
        nargs=4,
        required=True,
        metavar=("WEST", "SOUTH", "EAST", "NORTH"),
        help="Area of interest as lon/lat: west south east north.",
    )
    sub.add_argument(
        "--ignition-date",
        required=True,
        metavar="YYYY-MM-DD",
        help="Date the fire departs.",
    )
    sub.add_argument(
        "--projection-days",
        type=int,
        required=True,
        help="Projection horizon in days.",
    )
    sub.add_argument(
        "--weather-source",
        default="gridmet",
        choices=["gridmet", "weathernext"],
        help="Weather backend (default: gridmet).",
    )
    sub.add_argument(
        "--cache-dir",
        help="Directory for cached fetched layers (reused across runs on the same AOI/date).",
    )
    grid = sub.add_mutually_exclusive_group()
    grid.add_argument(
        "--gsd",
        dest="gsd_m",
        type=_positive_float,
        metavar="METERS",
        help="Explicit grid scale in meters (e.g. 30); overrides automatic grid sizing.",
    )
    grid.add_argument(
        "--max-pixels",
        type=_positive_int,
        default=SimulationConfig.max_pixels,
        metavar="N",
        help="Size the longest AOI side to approximately N cells, with a 30 m minimum cell size "
        "(default: %(default)s when --gsd is omitted).",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pyroSim",
        description="Simulate wildfire spread and export an hours-before-burn GeoTIFF (EPSG:4326).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run one simulation and save a GeoTIFF.")
    _add_data_args(run_parser)
    run_parser.add_argument(
        "--ignition-lonlat",
        type=float,
        nargs=2,
        required=True,
        metavar=("LON", "LAT"),
        help="Ignition point as lon lat (must fall inside the AOI).",
    )
    run_parser.add_argument(
        "--output-name",
        "-o",
        required=True,
        help="Output GeoTIFF filename (a .tif extension is added if missing).",
    )
    run_parser.add_argument(
        "--intensity",
        action="store_true",
        help="Also save a fireline-intensity (kW/m) GeoTIFF as <name>_intensity.tif.",
    )
    run_parser.add_argument(
        "--severity",
        action="store_true",
        help="Also save a flame-length severity-class GeoTIFF as <name>_severity.tif.",
    )
    run_parser.set_defaults(func=_cmd_run)

    fetch_parser = subparsers.add_parser(
        "fetch",
        help="Fetch and cache the data for an AOI/date so later runs skip the download.",
    )
    _add_data_args(fetch_parser)
    fetch_parser.add_argument(
        "--static-only",
        action="store_true",
        help="Fetch only the date-independent layers (DEM/fuel/water), not the weather.",
    )
    fetch_parser.set_defaults(func=_cmd_fetch)
    return parser


def _sibling_path(output_path: pathlib.Path, suffix: str) -> pathlib.Path:
    """Build a sibling GeoTIFF path like `<stem>_<suffix>.tif` next to the hours output."""
    return output_path.with_name(f"{output_path.stem}_{suffix}.tif")


def _resolve_output_path(output_name: str) -> pathlib.Path:
    path = pathlib.Path(output_name).expanduser()
    if path.suffix.lower() not in (".tif", ".tiff"):
        path = path.with_suffix(".tif")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _validate_aoi(aoi_bounds, projection_days) -> None:
    west, south, east, north = aoi_bounds
    if west >= east or south >= north:
        raise ValueError("aoi-bounds must be ordered as west south east north with west<east, south<north.")
    if projection_days < 1:
        raise ValueError("projection-days must be >= 1.")


def _validate_scenario(aoi_bounds, ignition_lonlat, projection_days) -> None:
    _validate_aoi(aoi_bounds, projection_days)
    west, south, east, north = aoi_bounds
    lon, lat = ignition_lonlat
    if not (west <= lon <= east and south <= lat <= north):
        raise ValueError("ignition-lonlat must fall inside the AOI bounds.")


def _cmd_run(args: argparse.Namespace) -> int:
    aoi_bounds = tuple(args.aoi_bounds)
    ignition_lonlat = tuple(args.ignition_lonlat)
    _validate_scenario(aoi_bounds, ignition_lonlat, args.projection_days)
    output_path = _resolve_output_path(args.output_name)

    config = SimulationConfig(
        aoi_bounds=aoi_bounds,
        ignition_lonlat=ignition_lonlat,
        ignition_date=args.ignition_date,
        projection_days=args.projection_days,
        weather_source=args.weather_source,
        cache_dir=args.cache_dir,
        gsd_m=args.gsd_m,
        max_pixels=args.max_pixels,
    )

    store = DataStore(config.cache_dir) if config.cache_dir else None
    if store:
        print(f"Using cache: {config.cache_dir}")
    # Earth Engine is initialized lazily, only if a layer is missing from the cache.
    print("Running simulation...")
    results = run_simulation(config, store)

    saved = raster.write_geotiff(results, config, output_path)
    print(f"Saved hours GeoTIFF: {saved}")
    if args.intensity:
        intensity_path = raster.write_intensity_geotiff(results, config, _sibling_path(output_path, "intensity"))
        print(f"Saved intensity GeoTIFF: {intensity_path}")
    if args.severity:
        severity_path = raster.write_severity_geotiff(results, config, _sibling_path(output_path, "severity"))
        print(f"Saved severity GeoTIFF: {severity_path}")

    stats = results["stats"]
    print(
        f"Burned cells: {stats['burned_cells']} "
        f"({stats['burned_hectares']:.1f} ha) | "
        f"max fireline intensity: {stats['max_fireline_intensity_kw_m']:.0f} kW/m | "
        f"cell size: {stats['cell_size_m']:.0f} m | "
        f"weather: {results['meta']['weather_source']} | "
        f"stop: {stats['stop_condition']}"
    )
    return 0


def _cmd_fetch(args: argparse.Namespace) -> int:
    aoi_bounds = tuple(args.aoi_bounds)
    _validate_aoi(aoi_bounds, args.projection_days)
    if not args.cache_dir:
        raise ValueError("fetch requires --cache-dir.")

    # AOI center is a valid in-bounds placeholder; the ignition point does not affect fetching.
    west, south, east, north = aoi_bounds
    config = SimulationConfig(
        aoi_bounds=aoi_bounds,
        ignition_lonlat=((west + east) / 2.0, (south + north) / 2.0),
        ignition_date=args.ignition_date,
        projection_days=args.projection_days,
        weather_source=args.weather_source,
        cache_dir=args.cache_dir,
        gsd_m=args.gsd_m,
        max_pixels=args.max_pixels,
    )

    print(f"Initializing Earth Engine (project: {config.ee_project or 'unset'})...")
    initialize_ee(config.ee_project)

    store = DataStore(config.cache_dir)
    region = gee.aoi_geometry(config.aoi_bounds)
    scale = gee.compute_scale(config.aoi_bounds, config.max_pixels, config.min_scale_m, config.gsd_m)

    print("Fetching static layers (DEM, fuel, water)...")
    static = model.fetch_static(config, region, scale)
    store.save_static(config, static)

    if args.static_only:
        print(f"Cached static layers to {config.cache_dir}")
        return 0

    print("Fetching weather layers...")
    weather_stack = weather.get_weather(config, region, scale)
    store.save_weather(config, weather_stack)
    print(f"Cached static + weather ({weather_stack.source}) layers to {config.cache_dir}")
    return 0


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    sys.exit(main())
