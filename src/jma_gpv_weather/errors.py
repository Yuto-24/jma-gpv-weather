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
