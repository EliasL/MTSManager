"""Verify the configuration and manifest that identify a managed run."""

from pathlib import Path

from .config import Config
from .files import read_json


def read_run(folder, *, expected_name=None, allow_legacy_vtu=False):
    """Return the verified configuration and manifest without modifying the run."""
    folder = Path(folder)
    config = Config.read(folder / "config.conf", expected_name=expected_name or folder.name,
                         allow_legacy_vtu=allow_legacy_vtu)
    manifest = read_json(folder / "manifest.json")
    if manifest["config_hash"] != config.config_hash or manifest["settings"] != config.as_dict():
        raise ValueError(f"Manifest/configuration mismatch: {folder}")
    return config, manifest
