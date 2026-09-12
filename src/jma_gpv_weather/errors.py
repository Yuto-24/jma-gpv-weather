class GpvError(RuntimeError):
    """Model-neutral base for JMA GPV acquisition and processing failures."""


class MsmError(GpvError):
    """Base error for MSM acquisition and preparation failures."""


class InvalidQueryError(MsmError):
    pass


class NoCompatibleRunError(MsmError):
    pass


class SelectedRunCoverageError(MsmError):
    pass


class DownloadError(MsmError):
    pass


class CacheIntegrityError(MsmError):
    pass


class MissingVariableError(MsmError):
    pass


class StaticTerrainUnavailableError(MsmError):
    pass


class GsmError(GpvError):
    """GSM boundary; never a subtype of MsmError."""


class GsmCoverageError(GsmError):
    """Only deterministic specification exclusions."""

    def __init__(self, coverage):
        self.coverage = coverage
        super().__init__(", ".join(coverage.reason_codes))


class GsmDiscoveryError(GsmError):
    """Listing/transport failure: specification coverage is unchanged."""


class GsmRunUnavailableError(GsmError):
    """Successful discovery did not find the requested compatible run/files."""


class GsmProcessingError(GsmError):
    """Download, GRIB, normalized cache, or processing failure."""
