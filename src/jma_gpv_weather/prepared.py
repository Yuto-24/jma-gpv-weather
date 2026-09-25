"""Bounded model-neutral NPZ transport for portable prepared records.

Meteorological validation and assembly remain in each model's prepared-data API.
"""
from dataclasses import asdict
from datetime import datetime
import hashlib
from io import BytesIO
import json
import math
import re
import struct
from zipfile import BadZipFile, ZipFile, ZIP_STORED, ZIP_DEFLATED
import zlib

import numpy as np

from .models import RemoteFile, RunSelection

# Portable input budgets, independent of the desktop raw/NetCDF cache. Bound both
# compressed input and advertised allocations before NumPy reads any arrays.
_MAX_PAYLOAD_BYTES = 32 * 1024**2
_MAX_MEMBERS = 2048
_MAX_MEMBER_BYTES = 32 * 1024**2
_MAX_TOTAL_BYTES = 128 * 1024**2
_MAX_METADATA_BYTES = 1024**2
_MAX_NPY_HEADER_BYTES = 10000


def _validate_archive(payload, error_type):
    """Preflight ZIP sizes and NPY allocation headers without loading arrays."""
    # Bound directory parsing before ZipFile allocates one ZipInfo per entry.
    # Our bounded snapshots need neither multidisk nor ZIP64 central directories
    # (NumPy's ZIP64 local member headers are still supported).
    end = payload.rfind(b"PK\x05\x06", max(0, len(payload) - 65557))
    if end < 0 or end + 22 > len(payload):
        raise error_type("Invalid GPV prepared ZIP directory")
    _, disk, start_disk, disk_count, count, size, offset, comment_size = struct.unpack_from(
        "<4s4H2IH", payload, end
    )
    if count > _MAX_MEMBERS:
        raise error_type("GPV prepared archive exceeds member count limit")
    if (disk or start_disk or disk_count != count or offset + size != end
            or end + 22 + comment_size != len(payload)):
        raise error_type("Unsupported GPV prepared ZIP directory")
    position, actual_count = offset, 0
    while position < end:
        if position + 46 > end or payload[position:position + 4] != b"PK\x01\x02":
            raise error_type("Invalid GPV prepared ZIP directory entry")
        name_size, extra_size, entry_comment_size = struct.unpack_from("<HHH", payload, position + 28)
        actual_count += 1
        if actual_count > _MAX_MEMBERS:
            raise error_type("GPV prepared archive exceeds member count limit")
        if name_size > 64:
            raise error_type("Invalid GPV prepared ZIP member name size")
        position += 46 + name_size + extra_size + entry_comment_size
    if position != end or actual_count != count:
        raise error_type("Inconsistent GPV prepared ZIP member count")
    with ZipFile(BytesIO(payload)) as archive:
        members = archive.infolist()
        if len(members) > _MAX_MEMBERS:
            raise error_type("GPV prepared archive exceeds member count limit")
        if any(member.file_size > _MAX_MEMBER_BYTES for member in members):
            raise error_type("GPV prepared archive exceeds member size limit")
        if sum(member.file_size for member in members) > _MAX_TOTAL_BYTES:
            raise error_type("GPV prepared archive exceeds total expanded size limit")
        names = [member.filename for member in members]
        if any(re.fullmatch(r"(metadata|(surface|pressure)_(latitude|longitude)|r[0-9]+)\.npy", name)
               is None for name in names):
            raise error_type("GPV prepared archive has noncanonical member names")
        if len(set(names)) != len(names) or "metadata.npy" not in names:
            raise error_type("GPV prepared archive has duplicate or missing members")
        if archive.getinfo("metadata.npy").file_size > _MAX_METADATA_BYTES:
            raise error_type("GPV prepared archive exceeds metadata size limit")
        if any(member.compress_type not in (ZIP_STORED, ZIP_DEFLATED) for member in members):
            raise error_type("Unsupported GPV prepared compression method")
        for member in members:
            with archive.open(member) as stream:
                # NumPy checks header length after reading it. A bounded buffer
                # prevents a forged length from requesting unlimited inflation.
                header = BytesIO(stream.read(_MAX_NPY_HEADER_BYTES + 12))
                version = np.lib.format.read_magic(header)
                readers = {(1, 0): np.lib.format.read_array_header_1_0,
                           (2, 0): np.lib.format.read_array_header_2_0}
                if version not in readers:
                    raise error_type("Unsupported GPV prepared NPY version")
                shape, _, dtype = readers[version](header, max_header_size=_MAX_NPY_HEADER_BYTES)
                # A tiny NPY file can claim an enormous shape even when all ZIP
                # sizes pass. Match its allocation to the bounded member size.
                if (dtype.kind not in "fiu" or any(size < 0 for size in shape)
                        or math.prod(shape) * dtype.itemsize != member.file_size - header.tell()):
                    raise error_type("GPV prepared array size/dtype does not match ZIP member")


def encode_prepared(data, model) -> bytes:
    """Versioned NPZ snapshot, with JSON metadata and no pickle/object arrays."""
    data.validate()
    metadata = {"schema": 1, "model": model, "selection": asdict(data.selection),
                "source_hashes": data.source_hashes, "records": []}
    arrays = {}
    for group, records in (("surface", data.surface), ("pressure", data.pressure)):
        if records:
            _, lat, lon = next(iter(records.values()))
            arrays[f"{group}_latitude"] = lat
            arrays[f"{group}_longitude"] = lon
        for (valid, level, name), record in sorted(records.items()):
            index = len(metadata["records"])
            metadata["records"].append([group, valid.isoformat(), level, name])
            arrays[f"r{index}"] = np.asarray(record[0], dtype=float)
    arrays["metadata"] = np.frombuffer(
        json.dumps(metadata, default=lambda value: value.isoformat(), sort_keys=True).encode(),
        dtype=np.uint8,
    )
    output = BytesIO()
    np.savez_compressed(output, **arrays)
    return output.getvalue()


def decode_prepared(cls, payload: bytes, *, model, error_type, expected_sha256=None):
    """Load and validate a snapshot; optionally verify its externally stored hash.

    Source hashes identify GRIB inputs; expected_sha256 identifies this payload.
    Neither a self-contained manifest nor a hash authenticates its publisher.
    Input budgets: 32 MiB compressed, 2048 members, 32 MiB per member,
    128 MiB total expanded, 1 MiB metadata. Oversize data must be partitioned
    by the producer. ZIP and NPY sizes are checked before array allocation.
    """
    if len(payload) > _MAX_PAYLOAD_BYTES:
        raise error_type("GPV prepared payload exceeds compressed size limit")
    if expected_sha256 is not None and hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise error_type("GPV prepared payload SHA-256 mismatch")
    try:
        _validate_archive(payload, error_type)
        with np.load(BytesIO(payload), allow_pickle=False) as archive:
            metadata = json.loads(archive["metadata"].tobytes())
            if metadata["schema"] != 1 or metadata["model"] != model:
                raise ValueError("unsupported schema/model")
            selection = metadata["selection"]
            files = tuple(RemoteFile(**{**item, "run_utc": datetime.fromisoformat(item["run_utc"])})
                          for item in selection["files"])
            selection = RunSelection(datetime.fromisoformat(selection["run_utc"]), files)
            groups = {"surface": {}, "pressure": {}}
            grids = {}
            for index, (group, valid, level, name) in enumerate(metadata["records"]):
                key = (datetime.fromisoformat(valid), level, name)
                if key in groups[group]:
                    raise ValueError("duplicate record")
                if group not in grids:
                    grids[group] = (archive[f"{group}_latitude"], archive[f"{group}_longitude"])
                groups[group][key] = (archive[f"r{index}"], *grids[group])
            result = cls(selection, groups["surface"], groups["pressure"], metadata["source_hashes"])
            result.validate()
            return result
    except error_type:
        raise
    # zipfile uses RuntimeError for encrypted members and unsupported codecs.
    except (ValueError, TypeError, KeyError, OSError, EOFError, BadZipFile,
            AttributeError, zlib.error, RuntimeError) as exc:
        raise error_type(f"Invalid GPV prepared payload: {exc}") from exc
