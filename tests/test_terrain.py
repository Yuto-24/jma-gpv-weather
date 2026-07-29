from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import numpy as np
import pytest

from msm_wind.errors import TerrainValidationError
from msm_wind.terrain import (
    GridTerrainProvider,
    TerrainSourceManifest,
    validate_pzs_message,
)

UTC = timezone.utc
PZS_FILE_NAME = (
    "Z__C_RJTD_20260727120000_MSM_GPV_Rjp_"
    "Glm5km_Lm1-39_Pzs_FH00_grib2.bin"
)
TECHNICAL_REFERENCES = [
    "https://www.data.jma.go.jp/suishin/jyouhou/pdf/619.pdf",
    "https://www.data.jma.go.jp/suishin/jyouhou/pdf/648.pdf",
]


def source_manifest(sha256: str) -> dict:
    return {
        "schema_version": 1,
        "artifact_type": "jma_msm_gpv_model_terrain_pzs",
        "file_name": PZS_FILE_NAME,
        "sha256": sha256,
        "initial_time_utc": "2026-07-27T12:00:00Z",
        "model_terrain_version": "2025-05-20",
        "source": {
            "kind": "official",
            "provider": (
                "Japan Meteorological Business Support Center (JMBSC)"
            ),
            "url": "https://www.jmbsc.or.jp/jp/online/x-online0.html",
            "acquired_at_utc": "2026-07-29T00:00:00Z",
        },
        "terms": {
            "private_repository_bundling": "permitted",
            "usage": "Private development use permitted.",
            "redistribution": "Subject to the cited JMBSC terms.",
            "reference": "JMBSC written response",
        },
        "technical_references": TECHNICAL_REFERENCES,
    }


class FakePzsMessage:
    def __init__(self, **overrides):
        values = {
            "edition": 2,
            "discipline": 0,
            "parameterCategory": 3,
            "parameterNumber": 33,
            "gridDefinitionTemplateNumber": 30,
            "gridType": "lambert",
            "Nx": 817,
            "Ny": 661,
            "numberOfPoints": 817 * 661,
            "forecastTime": 0,
            "productDefinitionTemplateNumber": 0,
            "typeOfFirstFixedSurface": 1,
            "analDate": datetime(2026, 7, 27, 12),
        }
        values.update(overrides)
        for key, value in values.items():
            setattr(self, key, value)


def test_source_manifest_detects_hash_mismatch(tmp_path):
    artifact = tmp_path / PZS_FILE_NAME
    artifact.write_bytes(b"not-the-authoritative-grib")
    manifest = TerrainSourceManifest.from_mapping(source_manifest("0" * 64))

    with pytest.raises(TerrainValidationError, match="SHA-256 mismatch"):
        manifest.verify_file(artifact)


def test_source_manifest_hash_and_initial_time_round_trip(tmp_path):
    artifact = tmp_path / PZS_FILE_NAME
    artifact.write_bytes(b"authoritative-grib-placeholder")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    manifest_path = tmp_path / "source-manifest.json"
    manifest_path.write_text(
        json.dumps(source_manifest(digest)),
        encoding="utf-8",
    )

    manifest = TerrainSourceManifest.load(manifest_path)

    assert manifest.verify_file(artifact) == digest
    assert manifest.initial_time_utc == datetime(2026, 7, 27, 12, tzinfo=UTC)
    assert manifest.model_terrain_version == "2025-05-20"


def test_pqc_parameter_is_rejected():
    message = FakePzsMessage(parameterCategory=1, parameterNumber=83)

    with pytest.raises(TerrainValidationError, match="not Pzs"):
        validate_pzs_message(
            message,
            datetime(2026, 7, 27, 12, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"gridType": "regular_ll"}, "Lambert"),
        ({"Nx": 481, "Ny": 505}, "817x661"),
        ({"forecastTime": 1}, "FH00"),
    ],
)
def test_noncanonical_pzs_grid_is_rejected(overrides, message):
    with pytest.raises(TerrainValidationError, match=message):
        validate_pzs_message(
            FakePzsMessage(**overrides),
            datetime(2026, 7, 27, 12, tzinfo=UTC),
        )


def test_valid_pzs_identity_is_accepted():
    validate_pzs_message(
        FakePzsMessage(),
        datetime(2026, 7, 27, 12, tzinfo=UTC),
    )


def test_legacy_v1_cache_remains_readable(tmp_path):
    path = tmp_path / "legacy-terrain.npz"
    latitudes = np.array([[30.0, 30.0], [31.0, 31.0]])
    longitudes = np.array([[130.0, 131.0], [130.0, 131.0]])
    with path.open("wb") as handle:
        np.savez_compressed(
            handle,
            values_m=np.array([[0.0, 100.0], [200.0, 300.0]]),
            latitudes=latitudes,
            longitudes=longitudes,
            metadata=json.dumps(
                {
                    "schema_version": 1,
                    "source": "legacy Pzs cache",
                    "source_sha256": "c" * 64,
                }
            ),
        )

    restored = GridTerrainProvider.load(path)

    assert restored.cache_schema_version == 1
    assert restored.source == "legacy Pzs cache"
    assert restored(30.5, 130.5) == pytest.approx(150)


def test_curvilinear_native_grid_uses_bilinear_interpolation():
    provider = GridTerrainProvider(
        values_m=np.array([[0.0, 10.0], [20.0, 30.0]]),
        latitudes=np.array([[30.0, 30.1], [31.0, 31.1]]),
        longitudes=np.array([[130.0, 131.0], [130.1, 131.1]]),
    )

    assert provider(30.55, 130.55) == pytest.approx(15)
