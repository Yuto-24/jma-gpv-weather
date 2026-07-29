from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from msm_wind import (
    Availability,
    Bounds,
    EstimatedQnhQuery,
    ForecastRequirements,
    InterpolatedMsmTopographyProvider,
    MsmClient,
    RunId,
    WeatherVariable,
)
from msm_wind.core import RISH_BASE, RemoteFile, RunSelection
from msm_wind.qnh import estimate_qnh

UTC = timezone.utc
MIYAZAKI_AIRPORT = (31.877, 131.449, 6.0)
PINNED_RUN = datetime(2026, 7, 27, 12, tzinfo=UTC)
PINNED_SURFACE_NAME = (
    "Z__C_RJTD_20260727120000_MSM_GPV_Rjp_"
    "Lsurf_FH00-15_grib2.bin"
)
DEFAULT_MANIFEST = (
    Path(__file__).parents[1]
    / "manifests"
    / "topo-msm-5k-2025-05-20.json"
)


def _provider(tmp_path: Path):
    manifest = Path(os.environ.get("MSM_TOPO_MANIFEST", DEFAULT_MANIFEST))
    archive = os.environ.get("MSM_TOPO_ARCHIVE")
    if archive:
        provider = InterpolatedMsmTopographyProvider.from_distribution_archive(
            Path(archive), manifest, bounds=Bounds()
        )
        expected_chain_verified = True
    else:
        topography = os.environ.get("MSM_TOPO_5K")
        landsea = os.environ.get("MSM_LANDSEA_5K")
        if not topography or not landsea:
            pytest.skip(
                "set MSM_TOPO_ARCHIVE or MSM_TOPO_5K and MSM_LANDSEA_5K"
            )
        provider = InterpolatedMsmTopographyProvider.from_raw(
            Path(topography), Path(landsea), manifest, bounds=Bounds()
        )
        expected_chain_verified = False
    cache_path = provider.save(tmp_path / "interpolated-terrain.npz")
    return (
        InterpolatedMsmTopographyProvider.load(cache_path),
        expected_chain_verified,
    )


@pytest.mark.real_data
def test_official_interpolated_topography_prepares_finite_kyushu_cache(tmp_path):
    restored, expected_chain_verified = _provider(tmp_path)

    for latitude, longitude in (
        (29.7, 128.5),
        (29.7, 134.8),
        (35.2, 128.5),
        (35.2, 134.8),
        MIYAZAKI_AIRPORT[:2],
    ):
        terrain = restored(latitude, longitude)
        land_fraction = restored.land_fraction_at(latitude, longitude)
        assert terrain is not None and np.isfinite(terrain)
        assert land_fraction is not None and 0 <= land_fraction <= 1

    provenance = restored.provenance
    assert provenance["terrain_distribution_archive_sha256"] == (
        "6251a2494d8ac0ce6a26ee7c8a8dabc854010c5e9173d5b963ba4880791d242e"
    )
    assert provenance["terrain_inner_archive_sha256"] == (
        "06f678659f8d01b7fc44fe3736da51d78cd398358eca51a34dedea8c9eec75bb"
    )
    assert provenance["terrain_source_sha256"] == (
        "6ce16ae3781399dad2d618220d81fc41938aa54d693174c33ba347ad976f5250"
    )
    assert provenance["terrain_landsea_source_sha256"] == (
        "322bbb1a4086174f7ac813accc391118a1fcd7d13c3135878f52e716f6870483"
    )
    assert provenance["terrain_distribution_chain_verified"] is (
        expected_chain_verified
    )
    assert provenance["terrain_artifacts_verified"] is True
    assert provenance["terrain_license"] == "CC-BY-4.0"
    assert provenance["terrain_license_source_file_name"].endswith("README.txt")
    assert provenance["terrain_attribution"]
    assert provenance["terrain_model_version"] == "2025-05-20"
    assert provenance["terrain_model_version_basis"]
    assert provenance["terrain_source_artifacts_modified"] is False
    assert provenance["terrain_cache_modified"] is True
    assert provenance["terrain_cache_serialized"] is True
    assert provenance["interpolated_from_model_grid"] is True
    assert provenance["terrain_source_page_url"].startswith("https://")
    assert provenance["terrain_source_archive_url"].startswith("https://")
    assert provenance["terrain_source_grid"]["nx"] == 481
    assert provenance["terrain_source_grid"]["ny"] == 505


@pytest.mark.real_data
def test_pinned_rish_run_produces_qnh_and_topography_sensitivity(tmp_path):
    if os.environ.get("MSM_RUN_REAL_DATA") != "1":
        pytest.skip("set MSM_RUN_REAL_DATA=1 to download the pinned RISH run")
    terrain_provider, expected_chain_verified = _provider(tmp_path)
    remote = RemoteFile(
        PINNED_SURFACE_NAME,
        f"{RISH_BASE}/2026/07/27/{PINNED_SURFACE_NAME}",
        PINNED_RUN,
        "Lsurf",
        0,
        15,
    )
    selection = RunSelection(PINNED_RUN, (remote,))
    requirements = ForecastRequirements(
        (PINNED_RUN,),
        frozenset({WeatherVariable.ESTIMATED_QNH}),
    )
    cache_dir = Path(
        os.environ.get("MSM_REAL_DATA_CACHE", "data/real-test")
    )
    forecast = MsmClient(cache_dir=cache_dir).prepare_run(
        RunId(PINNED_RUN),
        requirements,
        available_runs=(selection,),
        terrain_provider=terrain_provider,
    )
    latitude, longitude, elevation = MIYAZAKI_AIRPORT
    result = forecast.query(
        EstimatedQnhQuery(latitude, longitude, PINNED_RUN, elevation)
    )

    assert result.availability == Availability.AVAILABLE
    assert 850 <= result.values["qnh_hpa"] <= 1100
    assert "INTERPOLATED_MODEL_TERRAIN" in result.warnings
    assert "ESTIMATED_QNH_NOT_OFFICIAL" in result.warnings
    assert result.provenance.trace["terrain_source_type"] == (
        "interpolated_msm_gpv_topography"
    )
    assert result.provenance.trace["terrain_license"] == "CC-BY-4.0"
    assert result.provenance.trace["terrain_distribution_chain_verified"] is (
        expected_chain_verified
    )
    assert result.provenance.trace["terrain_cache_serialized"] is True
    assert result.provenance.trace["terrain_source_manifest_sha256"]
    assert result.provenance.trace["interpolated_from_model_grid"] is True

    surface_values = [
        forecast._surface_scalar(name, latitude, longitude, PINNED_RUN)[0]
        for name in ("sp", "tmp_surface", "rh")
    ]
    model_terrain = float(result.values["model_terrain_height_m"])
    sensitivity = {}
    for offset in (-50, -25, -10, 0, 10, 25, 50):
        estimate = estimate_qnh(
            surface_values[0],
            surface_values[1],
            surface_values[2],
            model_terrain + offset,
            elevation,
        )
        sensitivity[str(offset)] = estimate.qnh_pa / 100
    assert sensitivity["-50"] < sensitivity["0"] < sensitivity["50"]

    metrics = {
        "run_utc": PINNED_RUN.isoformat(),
        "location": {
            "latitude": latitude,
            "longitude": longitude,
            "elevation_m": elevation,
        },
        "distribution_chain_verified": expected_chain_verified,
        "model_terrain_height_m": model_terrain,
        "land_fraction": result.values["terrain_land_fraction"],
        "coastal_mixed_fraction": result.provenance.trace[
            "terrain_coastal_mixed_fraction"
        ],
        "qnh_hpa": result.values["qnh_hpa"],
        "terrain_sensitivity_qnh_hpa": sensitivity,
    }
    print(json.dumps(metrics, indent=2, sort_keys=True))
