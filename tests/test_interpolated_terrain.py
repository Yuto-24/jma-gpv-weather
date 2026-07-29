from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import msm_wind.interpolated_terrain as terrain_module
from msm_wind import (
    Availability,
    Bounds,
    EstimatedQnhQuery,
    InterpolatedMsmTopographyProvider,
)
from msm_wind.core import RemoteFile, RunSelection
from msm_wind.dataset import PreparedForecast
from msm_wind.errors import TerrainValidationError
from msm_wind.interpolated_terrain import (
    FILE_SIZE_BYTES,
    GRID_NX,
    GRID_NY,
    InterpolatedTerrainSourceManifest,
)
from msm_wind.terrain import GridTerrainProvider, validate_pzs_message
from msm_wind.weather_cli import build_parser

UTC = timezone.utc
MANIFEST_PATH = (
    Path(__file__).parents[1]
    / "manifests"
    / "topo-msm-5k-2025-05-20.json"
)


def _source_arrays():
    latitudes = 47.6 - np.arange(GRID_NY, dtype=float) * 0.05
    longitudes = 120.0 + np.arange(GRID_NX, dtype=float) * 0.0625
    latitude_grid, longitude_grid = np.meshgrid(
        latitudes, longitudes, indexing="ij"
    )
    topography = latitude_grid * 10 + longitude_grid
    land_fraction = (longitude_grid >= 131.45).astype(float)
    return topography, land_fraction


def _source_bytes(byte_order: str = ">") -> tuple[bytes, bytes]:
    topography, land_fraction = _source_arrays()
    return (
        np.asarray(topography, dtype=f"{byte_order}f4").tobytes(),
        np.asarray(land_fraction, dtype=f"{byte_order}f4").tobytes(),
    )


def _patch_artifact_hashes(monkeypatch, topography: bytes, landsea: bytes):
    monkeypatch.setattr(
        terrain_module, "TOPO_SHA256", hashlib.sha256(topography).hexdigest()
    )
    monkeypatch.setattr(
        terrain_module, "LANDSEA_SHA256", hashlib.sha256(landsea).hexdigest()
    )


def _manifest() -> dict:
    return {
        "schema_version": 1,
        "artifact_type": terrain_module.ARTIFACT_TYPE,
        "terrain_source_type": terrain_module.TERRAIN_SOURCE_TYPE,
        "model_terrain_version": terrain_module.MODEL_TERRAIN_VERSION,
        "model_terrain_version_basis": terrain_module.MODEL_TERRAIN_VERSION_BASIS,
        "technical_reference": terrain_module.TECHNICAL_REFERENCE,
        "distribution": {
            "provider": terrain_module.DISTRIBUTION_PROVIDER,
            "page_url": terrain_module.DISTRIBUTION_PAGE_URL,
            "archive_url": terrain_module.DISTRIBUTION_ARCHIVE_URL,
            "archive_file_name": terrain_module.DISTRIBUTION_ARCHIVE_FILE_NAME,
            "archive_sha256": terrain_module.DISTRIBUTION_ARCHIVE_SHA256,
            "inner_archive_file_name": terrain_module.INNER_ARCHIVE_FILE_NAME,
            "inner_archive_sha256": terrain_module.INNER_ARCHIVE_SHA256,
        },
        "artifacts": {
            "topography": {
                "file_name": terrain_module.TOPO_FILE_NAME,
                "sha256": terrain_module.TOPO_SHA256,
                "size_bytes": FILE_SIZE_BYTES,
            },
            "landsea": {
                "file_name": terrain_module.LANDSEA_FILE_NAME,
                "sha256": terrain_module.LANDSEA_SHA256,
                "size_bytes": FILE_SIZE_BYTES,
            },
        },
        "license": {
            "spdx": terrain_module.LICENSE_SPDX,
            "url": terrain_module.LICENSE_URL,
            "source_file_name": terrain_module.LICENSE_SOURCE_FILE_NAME,
            "source_date": terrain_module.LICENSE_SOURCE_DATE,
            "issuer": terrain_module.LICENSE_ISSUER,
            "attribution": terrain_module.ATTRIBUTION,
        },
        "processing": {
            "source_artifacts_modified": False,
            "interpolated_from_model_grid": True,
        },
        "source_grid": {
            "projection": "regular_latitude_longitude",
            "nx": GRID_NX,
            "ny": GRID_NY,
            "first_latitude": 47.6,
            "first_longitude": 120.0,
            "latitude_step": -0.05,
            "longitude_step": 0.0625,
            "encoding": "IEEE754 float32",
            "byte_order": "big-endian",
            "storage_order": terrain_module.STORAGE_ORDER,
        },
    }


def _write_manifest(tmp_path: Path) -> Path:
    manifest_path = tmp_path / "topo-manifest.json"
    manifest_path.write_text(
        json.dumps(_manifest(), ensure_ascii=False), encoding="utf-8"
    )
    return manifest_path


def _write_sources(tmp_path: Path, monkeypatch, byte_order: str = ">"):
    topography_bytes, landsea_bytes = _source_bytes(byte_order)
    _patch_artifact_hashes(monkeypatch, topography_bytes, landsea_bytes)
    topography_path = tmp_path / terrain_module.TOPO_FILE_NAME
    landsea_path = tmp_path / terrain_module.LANDSEA_FILE_NAME
    topography_path.write_bytes(topography_bytes)
    landsea_path.write_bytes(landsea_bytes)
    return topography_path, landsea_path, _write_manifest(tmp_path)


def _write_distribution_archive(tmp_path: Path, monkeypatch):
    topography_bytes, landsea_bytes = _source_bytes()
    _patch_artifact_hashes(monkeypatch, topography_bytes, landsea_bytes)
    inner_buffer = io.BytesIO()
    with zipfile.ZipFile(inner_buffer, "w", zipfile.ZIP_DEFLATED) as inner:
        inner.writestr(
            f"{terrain_module.INNER_DIRECTORY}/{terrain_module.TOPO_FILE_NAME}",
            topography_bytes,
        )
        inner.writestr(
            f"{terrain_module.INNER_DIRECTORY}/{terrain_module.LANDSEA_FILE_NAME}",
            landsea_bytes,
        )
    inner_bytes = inner_buffer.getvalue()
    monkeypatch.setattr(
        terrain_module,
        "INNER_ARCHIVE_SHA256",
        hashlib.sha256(inner_bytes).hexdigest(),
    )
    outer_buffer = io.BytesIO()
    with zipfile.ZipFile(outer_buffer, "w", zipfile.ZIP_DEFLATED) as outer:
        outer.writestr(terrain_module.INNER_ARCHIVE_FILE_NAME, inner_bytes)
    outer_bytes = outer_buffer.getvalue()
    monkeypatch.setattr(
        terrain_module,
        "DISTRIBUTION_ARCHIVE_SHA256",
        hashlib.sha256(outer_bytes).hexdigest(),
    )
    archive_path = tmp_path / terrain_module.DISTRIBUTION_ARCHIVE_FILE_NAME
    archive_path.write_bytes(outer_bytes)
    return archive_path, _write_manifest(tmp_path)


def _provider(tmp_path: Path, monkeypatch, bounds=None):
    topography_path, landsea_path, manifest_path = _write_sources(
        tmp_path, monkeypatch
    )
    return InterpolatedMsmTopographyProvider.from_raw(
        topography_path,
        landsea_path,
        manifest_path,
        bounds=bounds
        or Bounds(lat_min=31, lat_max=32, lon_min=131, lon_max=132),
    )


def test_official_manifest_records_distribution_hashes_license_and_grid():
    manifest = InterpolatedTerrainSourceManifest.load(MANIFEST_PATH)

    assert manifest.distribution_archive_sha256 == (
        "6251a2494d8ac0ce6a26ee7c8a8dabc854010c5e9173d5b963ba4880791d242e"
    )
    assert manifest.inner_archive_sha256 == (
        "06f678659f8d01b7fc44fe3736da51d78cd398358eca51a34dedea8c9eec75bb"
    )
    assert manifest.topography_sha256 == (
        "6ce16ae3781399dad2d618220d81fc41938aa54d693174c33ba347ad976f5250"
    )
    assert manifest.landsea_sha256 == (
        "322bbb1a4086174f7ac813accc391118a1fcd7d13c3135878f52e716f6870483"
    )
    assert manifest.license_spdx == "CC-BY-4.0"
    assert manifest.license_source_file_name.endswith("README.txt")
    assert manifest.model_terrain_version == "2025-05-20"
    assert (manifest.grid_nx, manifest.grid_ny) == (481, 505)
    assert manifest.byte_order == "big-endian"


def test_manifest_rejects_reauthored_official_reference_hash():
    document = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    document["artifacts"]["topography"]["sha256"] = "0" * 64

    with pytest.raises(TerrainValidationError, match="pinned official release"):
        InterpolatedTerrainSourceManifest.from_mapping(document)


def test_interpolated_source_rejects_sha256_mismatch(tmp_path, monkeypatch):
    topography_path, landsea_path, manifest_path = _write_sources(
        tmp_path, monkeypatch
    )
    changed = bytearray(topography_path.read_bytes())
    changed[-1] ^= 1
    topography_path.write_bytes(changed)

    with pytest.raises(TerrainValidationError, match="SHA-256 mismatch"):
        InterpolatedMsmTopographyProvider.from_raw(
            topography_path, landsea_path, manifest_path
        )


def test_interpolated_source_rejects_wrong_size(tmp_path, monkeypatch):
    topography_path, landsea_path, manifest_path = _write_sources(
        tmp_path, monkeypatch
    )
    topography_path.write_bytes(topography_path.read_bytes()[:-4])

    with pytest.raises(TerrainValidationError, match="size mismatch"):
        InterpolatedMsmTopographyProvider.from_raw(
            topography_path, landsea_path, manifest_path
        )


def test_interpolated_source_rejects_little_endian_payload(tmp_path, monkeypatch):
    topography_path, landsea_path, manifest_path = _write_sources(
        tmp_path, monkeypatch, byte_order="<"
    )

    with pytest.raises(TerrainValidationError, match="big-endian"):
        InterpolatedMsmTopographyProvider.from_raw(
            topography_path, landsea_path, manifest_path
        )


def test_transposed_storage_is_rejected_even_at_expected_size(tmp_path, monkeypatch):
    topography_path, landsea_path, manifest_path = _write_sources(
        tmp_path, monkeypatch
    )
    topography, _ = _source_arrays()
    transposed_bytes = np.asarray(topography.T, dtype=">f4").tobytes()
    assert len(transposed_bytes) == FILE_SIZE_BYTES
    topography_path.write_bytes(transposed_bytes)

    with pytest.raises(TerrainValidationError, match="SHA-256 mismatch"):
        InterpolatedMsmTopographyProvider.from_raw(
            topography_path, landsea_path, manifest_path
        )


def test_distribution_archive_verifies_full_chain_and_point_bounds(
    tmp_path, monkeypatch
):
    archive_path, manifest_path = _write_distribution_archive(
        tmp_path, monkeypatch
    )
    provider = InterpolatedMsmTopographyProvider.from_distribution_archive(
        archive_path,
        manifest_path,
        bounds=Bounds(
            lat_min=31.877,
            lat_max=31.877,
            lon_min=131.449,
            lon_max=131.449,
        ),
    )

    assert provider(31.877, 131.449) == pytest.approx(450.219, abs=1e-5)
    assert provider.provenance["terrain_distribution_chain_verified"] is True
    assert provider.topography_m.shape == (4, 4)


def test_interpolated_cache_round_trip_and_kyushu_interpolation(
    tmp_path, monkeypatch
):
    provider = _provider(tmp_path, monkeypatch)
    cache_path = provider.save(tmp_path / "interpolated-terrain.npz")
    restored = InterpolatedMsmTopographyProvider.load(cache_path)

    assert restored(31.5, 131.5) == pytest.approx(446.5, abs=1e-5)
    assert restored.land_fraction_at(31.5, 131.449) == pytest.approx(
        0.184, abs=1e-3
    )
    assert restored.provenance["terrain_source_type"] == (
        "interpolated_msm_gpv_topography"
    )
    assert restored.provenance["interpolated_from_model_grid"] is True
    assert restored.provenance["terrain_cache_modified"] is True
    assert restored.provenance["terrain_cache_serialized"] is True
    assert restored.provenance["terrain_license"] == "CC-BY-4.0"
    assert cache_path.with_suffix(".npz.json").exists()

    with pytest.raises(TerrainValidationError):
        GridTerrainProvider.load(cache_path)


def test_cache_rejects_incomplete_provenance(tmp_path, monkeypatch):
    provider = _provider(tmp_path, monkeypatch)
    cache_path = provider.save(tmp_path / "interpolated-terrain.npz")
    with np.load(cache_path) as data:
        metadata = json.loads(str(data["metadata"].item()))
        arrays = {
            "topography_m": np.asarray(data["topography_m"]),
            "land_fraction": np.asarray(data["land_fraction"]),
            "latitudes": np.asarray(data["latitudes"]),
            "longitudes": np.asarray(data["longitudes"]),
        }
    del metadata["terrain_attribution"]
    tampered = tmp_path / "tampered.npz"
    with tampered.open("wb") as handle:
        np.savez_compressed(
            handle,
            **arrays,
            metadata=json.dumps(metadata, ensure_ascii=False),
        )

    with pytest.raises(TerrainValidationError, match="terrain_attribution"):
        InterpolatedMsmTopographyProvider.load(tampered)


def test_pzs_validator_rejects_regular_latlon_topography_grid():
    message = SimpleNamespace(
        edition=2,
        discipline=0,
        parameterCategory=3,
        parameterNumber=33,
        gridDefinitionTemplateNumber=0,
        gridType="regular_ll",
        Nx=481,
        Ny=505,
        numberOfPoints=481 * 505,
        forecastTime=0,
        productDefinitionTemplateNumber=0,
        typeOfFirstFixedSurface=1,
        analDate=datetime(2026, 7, 27, 12),
    )

    with pytest.raises(TerrainValidationError, match="Lambert"):
        validate_pzs_message(message, datetime(2026, 7, 27, 12, tzinfo=UTC))


def test_cli_requires_explicit_interpolated_cache_selector():
    args = build_parser().parse_args(
        [
            "query-qnh",
            "--time",
            "2026-07-27T12:00:00Z",
            "--lat",
            "31.877",
            "--lon",
            "131.449",
            "--elevation-m-msl",
            "6",
            "--interpolated-terrain-cache",
            "terrain.npz",
        ]
    )

    assert args.interpolated_terrain_cache == Path("terrain.npz")
    assert args.terrain_cache is None


def test_qnh_reports_interpolated_terrain_and_coastal_diagnostics(
    tmp_path, monkeypatch
):
    provider = _provider(tmp_path, monkeypatch)
    valid_time = datetime(2026, 7, 27, 12, tzinfo=UTC)
    latitudes = np.array([[31.8, 31.8], [32.0, 32.0]])
    longitudes = np.array([[131.4, 131.5], [131.4, 131.5]])
    surface = {}
    for level, name, value in (
        (0, "sp", 100000.0),
        (2, "tmp_surface", 298.0),
        (2, "rh", 75.0),
        (0, "mslp", 100900.0),
    ):
        surface[valid_time, level, name] = (
            np.full((2, 2), value),
            latitudes,
            longitudes,
        )
    remote = RemoteFile(
        "surface.bin",
        "https://example.test/surface.bin",
        valid_time,
        "Lsurf",
        0,
        15,
    )
    forecast = PreparedForecast(
        RunSelection(valid_time, (remote,)),
        surface,
        {},
        {},
        terrain_provider=provider,
    )

    result = forecast.query(
        EstimatedQnhQuery(31.877, 131.449, valid_time, elevation_msl_m=6)
    )

    assert result.availability == Availability.AVAILABLE
    assert "INTERPOLATED_MODEL_TERRAIN" in result.warnings
    assert "COASTAL_MIXED_LAND_FRACTION" in result.warnings
    assert result.values["terrain_land_fraction"] == pytest.approx(0.184, abs=1e-3)
    assert result.provenance.trace["terrain_source_type"] == (
        "interpolated_msm_gpv_topography"
    )
    assert result.provenance.trace["interpolated_from_model_grid"] is True
    assert result.provenance.trace["terrain_coastal_mixed_fraction"] is True
