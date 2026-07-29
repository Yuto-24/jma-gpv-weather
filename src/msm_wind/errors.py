class MsmError(RuntimeError):
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


class TerrainValidationError(MsmError):
    pass
