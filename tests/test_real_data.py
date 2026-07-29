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
    GridTerrainProvider,
    MsmClient,
    RunId,
    WeatherVariable,
)
from msm_wind.core import RISH_BASE, RemoteFile, RunSelection
from msm_wind.cache import sha256_file
from msm_wind.terrain import TerrainSourceManifest
from msm_wind.weather_cli import main as weather_main

UTC = timezone.utc
MIYAZAKI_AIRPORT = (31.877, 131.449, 6.0)
PINNED_RUN = datetime(2026, 7, 27, 12, tzinfo=UTC)
PINNED_SURFACE_NAME = (
    "Z__C_RJTD_20260727120000_MSM_GPV_Rjp_"
    "Lsurf_FH00-15_grib2.bin"
)


def _pzs_paths():
    grib = os.environ.get("MSM_PZS_GRIB")
    manifest = os.environ.get("MSM_PZS_MANIFEST")
    if not grib or not manifest:
        pytest.skip(
            "set MSM_PZS_GRIB and MSM_PZS_MANIFEST to an official Pzs asset"
        )
    return Path(grib), Path(manifest)


@pytest.mark.real_data
def test_official_pzs_prepares_finite_kyushu_cache(tmp_path):
    grib_path, manifest_path = _pzs_paths()
    output = tmp_path / "terrain.npz"

    exit_code = weather_main(
        [
            "prepare-terrain",
            "--input-grib",
            str(grib_path),
            "--source-manifest",
            str(manifest_path),
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    provider = GridTerrainProvider.load(output)
    source_manifest = TerrainSourceManifest.load(manifest_path)
    assert np.isfinite(provider.values_m).all()
    for latitude, longitude in (
        (29.7, 128.5),
        (29.7, 134.8),
        (35.2, 128.5),
        (35.2, 134.8),
        MIYAZAKI_AIRPORT[:2],
    ):
        terrain = provider(latitude, longitude)
        assert terrain is not None and np.isfinite(terrain)
    assert provider.source_sha256 == source_manifest.sha256
    assert provider.source_manifest_sha256 == sha256_file(manifest_path)
    assert provider.model_terrain_version == "2025-05-20"
    sidecar = json.loads(
        output.with_suffix(".npz.json").read_text(encoding="utf-8")
    )
    assert sidecar["schema_version"] == 2
    assert sidecar["source_sha256"] == source_manifest.sha256


@pytest.mark.real_data
def test_pinned_rish_run_produces_qnh_with_pzs_provenance():
    if os.environ.get("MSM_RUN_REAL_DATA") != "1":
        pytest.skip("set MSM_RUN_REAL_DATA=1 to download the pinned RISH run")
    grib_path, manifest_path = _pzs_paths()
    terrain = GridTerrainProvider.from_grib(
        grib_path,
        Bounds(),
        source_manifest=manifest_path,
    )
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
        terrain_provider=terrain,
    )
    latitude, longitude, elevation = MIYAZAKI_AIRPORT

    result = forecast.query(
        EstimatedQnhQuery(
            latitude,
            longitude,
            PINNED_RUN,
            elevation,
        )
    )

    assert result.availability == Availability.AVAILABLE
    assert 850 <= result.values["qnh_hpa"] <= 1100
    assert np.isfinite(result.values["model_terrain_height_m"])
    assert {
        "ESTIMATED_QNH_NOT_OFFICIAL",
        "NOT_FOR_OPERATIONAL_USE",
    }.issubset(result.warnings)
    assert (
        result.provenance.trace["terrain_source_sha256"]
        == terrain.source_sha256
    )
    assert (
        result.provenance.trace["terrain_model_version"]
        == "2025-05-20"
    )
    assert result.provenance.trace["terrain_source_kind"] == "official"
