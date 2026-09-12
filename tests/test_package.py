"""Installed-distribution/API/entry-point checks, also run against the built wheel."""
import importlib.metadata
import importlib.util
import json
import subprocess
import sys

import jma_gpv_weather


def test_distribution_and_canonical_api():
    distribution = importlib.metadata.distribution('jma-gpv-weather')
    assert distribution.version == jma_gpv_weather.__version__ == '0.3.0'
    assert importlib.util.find_spec('msm_wind') is None
    assert all(getattr(jma_gpv_weather, name) is not None for name in jma_gpv_weather.__all__)
    assert jma_gpv_weather.MsmClient.__module__ == 'jma_gpv_weather.msm.client'
    entries = {entry.name: entry.value for entry in distribution.entry_points
               if entry.group == 'console_scripts'}
    assert entries == {
        'jma-gpv-weather': 'jma_gpv_weather.cli:main',
        'jma-gpv-msm-csv': 'jma_gpv_weather.msm.csv_cli:main',
    }
    assert not any(str(path).startswith('msm_wind/') for path in distribution.files)


def test_installed_entry_points_outside_checkout(tmp_path):
    # Isolated Python proves imports are supplied by the installed distribution.
    code = '''
from importlib.metadata import distribution
import sys
entry = next(e for e in distribution('jma-gpv-weather').entry_points if e.name == sys.argv[1])
sys.argv = [entry.name, *sys.argv[2:]]
raise SystemExit(entry.load()())
'''
    for name in ('jma-gpv-weather', 'jma-gpv-msm-csv'):
        result = subprocess.run([sys.executable, '-I', '-c', code, name, '--help'],
                                cwd=tmp_path, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert name in result.stdout
    result = subprocess.run([
        sys.executable, '-I', '-c', code, 'jma-gpv-weather',
        '--cache-dir', str(tmp_path / 'cache'), 'cache-verify',
    ], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'valid': True, 'files': []}
