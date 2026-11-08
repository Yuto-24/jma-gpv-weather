# Legacy desktop cache recovery

This maintenance build is based on 0.5.0 (`585bca6`). The distribution is
`0.5.0+legacycache.1`; `__version__` retains the base 0.5.0 API contract.
`MsmClient`, GSM and portable consumers keep their default acquisition policy.

Use `jma_gpv_weather.msm.cached_client.CachedMsmClient` only in the native Legacy
runtime. Call `begin_calculation()` once per user action, not per iteration.
Do not share the client between sessions or with the background prewarmer.

A listing refresh transport failure activates cache-only discovery for that
action. Saved listing timestamps are not extended. Missing listings contribute
no candidates; only Runs with all requested local raw files are candidates.
The original interpolation brackets and Run selection rules apply unchanged.
`prepare_run` does not download or rename corrupt files while recovering. GRIB
validation, source hashing, decoding and field validation remain mandatory.
Missing or corrupt data fails the attempt, and the application retains its last
good result. A fixed Run is never replaced by another cached Run.

`acquisition_metadata` identifies cached acquisition and `LISTING_REFRESH_FAILED`.
It does not assert that cached discovery is the latest upstream inventory.
The application must show this status and retain it with the actual Run in the
saved calculation. The next action retries ordinary listing refresh.

Build with `uv build --wheel`. Run `pytest` without opting into real-data tests;
`tests/test_cached_client.py` covers stale recovery, partial caches, fixed Runs,
next-action recovery, corruption, missing files and unchanged default behavior.
