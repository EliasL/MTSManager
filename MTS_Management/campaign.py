"""Campaign validation shared by Python callers and the CLI."""

from .config import Config
from .files import digest, read_json
import re
from pathlib import Path
from functools import lru_cache


def campaign_entries(path):
    """Read a campaign for submission using the current execution schema."""
    return _read_campaign(path, Config.saved)


def saved_campaign_entries(path):
    """Interpret a recorded campaign for analysis and retain its run identities."""
    return _read_campaign(path, Config.from_saved_settings)


def campaign_member(record, name, planned_hashes):
    return record["config_hash"] in planned_hashes or (name is not None and record.get("campaign") == name)


def _read_campaign(path, read_settings):
    metadata = Path(path).with_name("metadata.json")
    supplement = read_json(metadata).get("missing_settings", {}) if metadata.exists() else {}
    return _validate_campaign(read_json(path), supplement, read_settings)


def validate_campaign(data, supplement=None):
    """Validate a campaign definition and return its name and labelled configurations."""
    return _validate_campaign(data, supplement, Config.saved)


def _validate_campaign(data, supplement, read_settings):
    if data.keys() != {"name", "runs"} or not data["runs"]:
        raise ValueError("Campaign requires name and nonempty runs")
    result = []
    seen = set()
    for entry in data["runs"]:
        if entry.keys() != {"label", "settings"}:
            raise ValueError("Campaign runs require label and complete settings")
        config = read_settings(entry["settings"], supplement)
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


@lru_cache(maxsize=4)
def _supplements(stamps):
    result = {}
    for definition, _, metadata, _ in stamps:
        data = read_json(definition)
        extra = read_json(metadata).get("missing_settings", {})
        for run in data["runs"]:
            h = digest({"format": "mtscripts-config-v1", "settings": run["settings"]})
            if h in result and result[h] != extra:
                raise ValueError("Conflicting campaign supplemental settings")
            result[h] = extra
    return result


def supplemental_settings(settings):
    campaign_root = Path(__file__).resolve().parent.parent / "campaigns"
    stamps = []
    for definition in sorted(campaign_root.rglob("simulations.json")):
        metadata = definition.with_name("metadata.json")
        if metadata.exists():
            stamps.append((str(definition), definition.stat().st_mtime_ns,
                           str(metadata), metadata.stat().st_mtime_ns))
    return _supplements(tuple(stamps)).get(
        digest({"format": "mtscripts-config-v1", "settings": settings}), {})


def record_executable(campaign, site, executable, checksum=None):
    from .files import file_digest, locked, write_json
    root = Path(__file__).resolve().parent.parent / "campaigns"
    for definition in root.rglob("simulations.json"):
        if read_json(definition)["name"] != campaign:
            continue
        path = definition.with_name("metadata.json")
        with locked(path.with_suffix(".lock")):
            metadata = read_json(path) if path.exists() else {}
            entry = {"site": site, "path": str(executable), "sha256": checksum or file_digest(executable)}
            entries = metadata.setdefault("executables", [])
            if entry not in entries:
                entries.append(entry)
                write_json(path, metadata)


def campaign_files(names):
    root = Path(__file__).resolve().parent.parent / "campaigns"
    result = []
    for definition in root.rglob("simulations.json"):
        if read_json(definition)["name"] in names:
            result.append({p.name: read_json(p) for p in
                           (definition, definition.with_name("resources.json"), definition.with_name("metadata.json"))
                           if p.exists()})
    return result


def install_campaign_files(records):
    from .files import write_json
    root = Path(__file__).resolve().parent.parent / "campaigns"
    for record in records:
        definition = record["simulations.json"]
        matches = [p for p in root.rglob("simulations.json") if read_json(p)["name"] == definition["name"]]
        if matches and any(read_json(p) != definition for p in matches):
            raise ValueError("Retrieved campaign definition conflicts with local definition")
        directory = matches[0].parent if matches else root / ("retrieved-" + digest(definition["name"])[:20])
        for name, data in record.items():
            if name not in {"simulations.json", "resources.json", "metadata.json"}:
                raise ValueError("Unexpected campaign file")
            path = directory / name
            if name == "metadata.json" and path.exists():
                data = dict(data)
                previous = read_json(path)
                local = previous.get("executables", [])
                data["executables"] = local + [e for e in data.get("executables", []) if e not in local]
                if "sweep" in previous:
                    data.setdefault("sweep", previous["sweep"])
            write_json(path, data)
