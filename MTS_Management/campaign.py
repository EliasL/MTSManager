"""Campaign validation shared by Python callers and the CLI."""

from .config import Config
from .files import digest, read_json
import re


def campaign_entries(path):
    data = read_json(path)
    if data.keys() != {"name", "runs"} or not data["runs"]:
        raise ValueError("Campaign requires name and nonempty runs")
    result = []
    seen = set()
    for entry in data["runs"]:
        if entry.keys() != {"label", "settings"}:
            raise ValueError("Campaign runs require label and complete settings")
        config = Config(**entry["settings"])
        if entry["settings"].keys() != config.as_dict().keys():
            raise ValueError("Campaign settings must be complete")
        config.folder_name(entry["label"])
        if config.config_hash in seen:
            raise ValueError("Duplicate configuration in campaign")
        seen.add(config.config_hash)
        result.append((config, entry["label"]))
    return data["name"], result



def campaign_identity(path):
    """Read saved plan identities without altering settings or submitting jobs."""
    data = read_json(path)
    if data.keys() != {"name", "runs"} or not data["runs"]:
        raise ValueError("Campaign requires name and nonempty runs")
    expected = {}
    for entry in data["runs"]:
        if entry.keys() != {"label", "settings"} or not isinstance(entry["settings"], dict):
            raise ValueError("Campaign runs require a label and saved settings")
        label = entry["label"]
        if not isinstance(label, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", label):
            raise ValueError("Invalid campaign label")
        identity = digest({"format": "mtscripts-config-v1", "settings": entry["settings"]})
        if identity in expected:
            raise ValueError("Duplicate configuration in campaign")
        expected[identity] = f"{label}[{identity[:20]}]"
    return data["name"], expected
