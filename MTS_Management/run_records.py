"""Verify the configuration and manifest that identify a managed run."""

from pathlib import Path

from .config import Config
from .files import read_json


def read_run(folder, *, expected_name=None):
    """Verify a recorded run and interpret its settings for analysis or inventory."""
    return _read_run(folder, expected_name, Config.read_saved)


def read_run_for_execution(folder):
    """Verify a run using the current execution schema."""
    return _read_run(folder, None, Config.read)


def _read_run(folder, expected_name, read_config):
    folder = Path(folder)
    config = read_config(folder / "config.conf", expected_name=expected_name or folder.name)
    manifest = read_json(folder / "manifest.json")
    if manifest["config_hash"] != config.config_hash or manifest["settings"] != config.as_dict():
        raise ValueError(f"Manifest/configuration mismatch: {folder}")
    return config, manifest
