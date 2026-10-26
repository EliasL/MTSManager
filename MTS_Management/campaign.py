"""Campaign validation shared by Python callers and the CLI."""

from .config import Config
from .files import read_json


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

