"""On-disk cache for fetched Earth Engine layers, split by date dependency.

Two groups are cached separately so re-runs avoid redundant downloads:
- static: slope, aspect, landcover, water — depend only on the AOI grid (date-independent).
- weather: the WeatherStack cubes — depend on the date/backend as well.

Each group lives under `<cache_dir>/<group>/<key>/` as `arrays.npz` + `manifest.json`, keyed by a
hash of the parameters that determine its contents. Changing only the ignition point reuses both
groups; changing only the date reuses static and refetches weather.
"""

import hashlib
import json
import pathlib

import numpy as np

from .weather import WeatherStack


def _key(params: dict) -> str:
    """Stable short hash of the parameters that determine a cached group's contents."""
    payload = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(payload.encode()).hexdigest()[:16]


def _round_bounds(bounds) -> list[float]:
    return [round(float(b), 6) for b in bounds]


def static_params(config) -> dict:
    """Parameters that fully determine the static (date-independent) layers."""
    params = {
        "aoi_bounds": _round_bounds(config.aoi_bounds),
        "max_pixels": config.max_pixels,
        "min_scale_m": config.min_scale_m,
        "water_fraction_threshold": config.water_fraction_threshold,
    }
    if config.gsd_m is not None:
        # Explicit GSD overrides these limits. Preserve legacy keys otherwise.
        del params["max_pixels"]
        del params["min_scale_m"]
        params["gsd_m"] = float(config.gsd_m)
    return params


def weather_params(config) -> dict:
    """Parameters that fully determine the weather layers (static grid + date/backend)."""
    params = static_params(config)
    params.update(
        {
            "ignition_date": config.ignition_date,
            "projection_days": config.projection_days,
            "weather_source": config.weather_source,
            "start_hour": config.start_hour,
            "allow_gridmet_fallback": config.allow_gridmet_fallback,
        }
    )
    if config.weather_source == "weathernext":
        params.update(
            {
                "weathernext_stat": config.weathernext_stat,
                "weathernext_step_hours": config.weathernext_step_hours,
                "weathernext_init_time": config.weathernext_init_time,
            }
        )
    return params


class DataStore:
    """Read/write cached static and weather layers under a cache directory."""

    def __init__(self, cache_dir):
        self.root = pathlib.Path(cache_dir).expanduser()

    def _group_dir(self, group: str, key: str) -> pathlib.Path:
        return self.root / group / key

    # --- static layers ---
    def load_static(self, config) -> dict | None:
        """Return {slope, aspect, landcover, water} from cache, or None on a miss."""
        path = self._group_dir("static", _key(static_params(config))) / "arrays.npz"
        if not path.exists():
            return None
        with np.load(path) as data:
            return {name: data[name] for name in data.files}

    def save_static(self, config, static: dict) -> None:
        directory = self._group_dir("static", _key(static_params(config)))
        directory.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(directory / "arrays.npz", **static)
        _write_manifest(directory, static_params(config))

    # --- weather layers ---
    def load_weather(self, config) -> WeatherStack | None:
        """Return a WeatherStack rebuilt from cache, or None on a miss."""
        directory = self._group_dir("weather", _key(weather_params(config)))
        arrays_path = directory / "arrays.npz"
        manifest_path = directory / "manifest.json"
        if not (arrays_path.exists() and manifest_path.exists()):
            return None
        manifest = json.loads(manifest_path.read_text())
        with np.load(arrays_path) as data:
            cubes = {name: data[name] for name in data.files}
        return WeatherStack(
            cubes=cubes,
            band_duration_min=manifest["band_duration_min"],
            start_minutes=manifest["start_minutes"],
            source=manifest["source"],
        )

    def save_weather(self, config, weather_stack: WeatherStack) -> None:
        directory = self._group_dir("weather", _key(weather_params(config)))
        directory.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(directory / "arrays.npz", **weather_stack.cubes)
        manifest = weather_params(config)
        # Record the actual backend/timing used (WeatherNext may fall back to GRIDMET).
        manifest.update(
            {
                "band_duration_min": weather_stack.band_duration_min,
                "start_minutes": weather_stack.start_minutes,
                "source": weather_stack.source,
            }
        )
        _write_manifest(directory, manifest)


def _write_manifest(directory: pathlib.Path, manifest: dict) -> None:
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
