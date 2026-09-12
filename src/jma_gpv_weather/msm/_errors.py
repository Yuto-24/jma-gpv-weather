"""Translate shared failures only where they enter the MSM API."""
from contextlib import contextmanager

from ..errors import GpvError, MsmError


@contextmanager
def msm_error_boundary():
    try:
        yield
    except MsmError:
        # Keep specific MSM errors (and their identity) intact.
        raise
    except GpvError as exc:
        raise MsmError(str(exc)) from exc
