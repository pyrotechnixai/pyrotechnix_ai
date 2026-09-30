"""Grid selection, CLI plumbing, and compatibility with the bundled sample cache."""

import contextlib
import io
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from firesim import cli, gee, model
from firesim.cache import DataStore, _key, static_params, weather_params
from firesim.config import SimulationConfig


CACHE = Path(__file__).resolve().parents[1] / "tahoe_cache"


def sample_config(**kwargs):
    return SimulationConfig(
        aoi_bounds=(-120.55, 39.0, -120.3, 39.2),
        ignition_lonlat=(-120.45, 39.1),
        ignition_date="2026-09-10",
        projection_days=5,
        weather_source="weathernext",
        **kwargs,
    )


def arguments(command):
    args = [
        command, "--aoi-bounds", "-120.55", "39.0", "-120.3", "39.2",
        "--ignition-date", "2026-09-10", "--projection-days", "5",
        "--weather-source", "weathernext", "--cache-dir", str(CACHE),
    ]
    if command == "run":
        args += ["--ignition-lonlat", "-120.45", "39.1", "-o", "unused.tif"]
    return args


class GridOptionsTests(unittest.TestCase):
    def test_scale_selection(self):
        bounds = sample_config().aoi_bounds
        self.assertAlmostEqual(gee.compute_scale(bounds, 160, 30), 138.175)
        self.assertEqual(gee.compute_scale(bounds, 800, 30), 30)
        for gsd in (15, 30, 60.5):
            self.assertEqual(gee.compute_scale(bounds, 160, 30, gsd), gsd)
        for bad in (0, -1, float("nan"), float("inf")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                gee.compute_scale(bounds, 160, 30, bad)

    def test_cli_rejects_conflicting_and_invalid_options(self):
        parser = cli._build_parser()
        invalid = [
            ["--gsd", "30", "--max-pixels", "800"],
            *[["--gsd", value] for value in ("0", "-1", "nan", "inf", "bad")],
            *[["--max-pixels", value] for value in ("0", "-1", "1.5", "nan")],
        ]
        for command in ("fetch", "run"):
            for flags in invalid:
                with self.subTest(command=command, flags=flags):
                    with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                        parser.parse_args(arguments(command) + flags)
                    self.assertEqual(error.exception.code, 2)

    def test_both_commands_pass_grid_options(self):
        class StopBeforeWork(Exception):
            pass

        for command in ("fetch", "run"):
            for flags, gsd, pixels in (([], None, 160), (["--gsd", "30"], 30, 160),
                                       (["--max-pixels", "800"], None, 800)):
                with self.subTest(command=command, flags=flags):
                    target = "firesim.cli.run_simulation" if command == "run" else "firesim.cli.model.fetch_static"
                    with patch(target, side_effect=StopBeforeWork) as work, \
                         patch("firesim.cli.initialize_ee"), \
                         patch("firesim.cli.gee.aoi_geometry"), \
                         patch("firesim.cli._resolve_output_path", return_value=Path("unused.tif")), \
                         contextlib.redirect_stdout(io.StringIO()):
                        with self.assertRaises(StopBeforeWork):
                            cli.main(arguments(command) + flags)
                    config = work.call_args.args[0]
                    self.assertEqual(config.gsd_m, gsd)
                    self.assertEqual(config.max_pixels, pixels)
                    if command == "fetch":
                        self.assertAlmostEqual(work.call_args.args[2], 138.175 if not flags else 30)

    def test_legacy_cache_and_resolution_isolation(self):
        config = sample_config()
        self.assertEqual(_key(static_params(config)), "0c98ea1e6c520767")
        self.assertEqual(_key(weather_params(config)), "ce676bdcce4421ec")
        store = DataStore(CACHE)
        static, weather = store.load_static(config), store.load_weather(config)
        self.assertIsNotNone(static)
        self.assertIsNotNone(weather)
        for changed in (replace(config, gsd_m=30), replace(config, max_pixels=800)):
            self.assertIsNone(store.load_static(changed))
            self.assertIsNone(store.load_weather(changed))
        explicit = replace(config, gsd_m=30)
        with tempfile.TemporaryDirectory() as directory:
            store = DataStore(directory)
            # Fixture arrays only: verify cache routing independently of fetching.
            store.save_static(explicit, static)
            store.save_weather(explicit, weather)
            equivalent = replace(explicit, gsd_m=30.0, max_pixels=800, min_scale_m=60)
            self.assertIsNotNone(store.load_static(equivalent))
            self.assertIsNotNone(store.load_weather(equivalent))
            self.assertIsNone(store.load_static(replace(explicit, gsd_m=60)))
            self.assertIsNone(store.load_weather(replace(explicit, gsd_m=60)))

    def test_run_fetch_and_model_metadata_use_explicit_gsd(self):
        config = sample_config(gsd_m=30)
        store = DataStore(CACHE)
        static = store.load_static(sample_config())
        weather = store.load_weather(sample_config())
        with patch("firesim.model.gee.initialize_ee"), \
             patch("firesim.model.gee.aoi_geometry"), \
             patch("firesim.model.fetch_static", return_value=static) as fetch, \
             patch("firesim.model.weather.get_weather", return_value=weather) as get_weather:
            _, meta = model.build_inputs(config)
        self.assertEqual(fetch.call_args.args[2], 30)
        self.assertEqual(get_weather.call_args.args[2], 30)
        self.assertEqual(meta["scale"], 30)


if __name__ == "__main__":
    unittest.main()
