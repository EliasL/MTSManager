"""Strict current-format readers, independent of plotting and old field aliases."""

import csv
import hashlib
import json
import math
from pathlib import Path
import re
from xml.parsers import expat

from .errors import ExpectedError
from .files import digest


MESH_SNAPSHOT = re.compile(r".*_load=(?P<load>[-+\d.eE]+)_nrM=(?P<plastic>\d+)_\.(?P<step>\d+)\.vtu")


def duration_seconds(value):
    """Parse the native simulator's duration text."""
    tokens = value.split()
    if not tokens or any(not re.fullmatch(r"\d+(?:\.\d+)?[dhms]", token) for token in tokens):
        raise ValueError(f"Invalid simulator duration: {value!r}")
    units = [token[-1] for token in tokens]
    if len(set(units)) != len(units) or units != sorted(units, key="dhms".index):
        raise ValueError(f"Invalid duration unit order: {value!r}")
    seconds = sum(float(token[:-1]) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[token[-1]]
                  for token in tokens)
    if not math.isfinite(seconds):
        raise ValueError(f"Non-finite simulator duration: {value!r}")
    return seconds


def mesh_snapshots(folder, *, through_step=None):
    """List native accepted-state snapshots in load-step order."""
    frames = []
    for path in (Path(folder) / "data").glob("*.vtu"):
        match = MESH_SNAPSHOT.fullmatch(path.name)
        if (match and (through_step is None or int(match["step"]) <= through_step)
                and path.is_file() and not path.is_symlink()):
            frames.append({"name": path.name, "step": int(match["step"]), "load": float(match["load"])})
    frames.sort(key=lambda frame: frame["step"])
    if len({frame["step"] for frame in frames}) != len(frames):
        raise ValueError(f"Multiple mesh snapshots for the same load step in {folder}")
    return frames


def vtu_fields(path):
    """Read native VTU field names and component counts without loading mesh arrays."""
    fields = {"cell": {}, "point": {}}
    associations = {"CellData": "cell", "PointData": "point"}
    stack = []

    def start(tag, attrs):
        if tag == "DataArray" and len(stack) >= 2 and stack[-2] == "Piece" and stack[-1] in associations:
            name = attrs["Name"]
            components = int(attrs.get("NumberOfComponents", 1))
            group = fields[associations[stack[-1]]]
            if components < 1 or (name in group and group[name] != components):
                raise ValueError(f"Invalid VTU field declaration for {name!r} in {path}.")
            group[name] = components
        stack.append(tag)

    parser = expat.ParserCreate()
    parser.StartElementHandler = start
    parser.EndElementHandler = lambda tag: stack.pop()
    with Path(path).open("rb") as stream:
        parser.ParseFile(stream)
    return fields


def seed_group_fields(settings):
    return {"settings_group": digest({key: value for key, value in settings.items() if key != "seed"}),
            "seed": settings["seed"]}


def plot_seed_group(output_root, folder, manifest_sha256):
    """Find compatible sibling runs without reading attempt state or simulation tables."""
    root = Path(output_root).resolve()
    folder = Path(folder)
    anchor_path = folder / "manifest.json"
    if folder.parent != root or folder.is_symlink() or not anchor_path.is_file():
        raise ExpectedError("The selected run changed. Refresh Monitor data and select it again.")
    anchor_content = anchor_path.read_bytes()
    if hashlib.sha256(anchor_content).hexdigest() != manifest_sha256:
        raise ExpectedError("The selected run changed. Refresh Monitor data and select it again.")
    anchor = json.loads(anchor_content)
    group = seed_group_fields(anchor["settings"])
    members = []
    for path in sorted(root.glob("*/manifest.json")):
        if path.parent.is_symlink():
            continue
        content = anchor_content if path == anchor_path else path.read_bytes()
        record = anchor if path == anchor_path else json.loads(content)
        candidate = seed_group_fields(record["settings"])
        if (candidate["settings_group"] == group["settings_group"] and
                record["executable_sha256"] == anchor["executable_sha256"]):
            members.append({"folder": str(path.parent), "config_hash": record["config_hash"],
                            "manifest_sha256": hashlib.sha256(content).hexdigest(), "seed": candidate["seed"]})
    if len({row["seed"] for row in members}) != len(members):
        raise ValueError(f"Multiple compatible runs have the same seed in {root}")
    return members


def macro_reader(stream, *, allow_incomplete_tail=False):
    lines = (line for line in stream if line.endswith("\n")) if allow_incomplete_tail else stream
    return csv.DictReader(lines)


def last_macro_row(path, *, limit=65536):
    """Read the last complete native macro row with bounded I/O."""
    path = Path(path)
    if not path.exists():
        return None
    with path.open("rb") as stream:
        first = stream.readline(limit)
        if not first.endswith(b"\n"):
            raise ValueError(f"Missing/big CSV header: {path}")
        header = next(csv.reader([first.decode().strip()]))
        if "load" not in header or len(set(header)) != len(header):
            raise ValueError(f"Invalid current CSV header: {path}")
        stream.seek(0, 2)
        offset = max(len(first), stream.tell() - limit)
        if offset > len(first):
            stream.seek(offset - 1)
            if stream.read(1) != b"\n":
                stream.readline()  # Discard a partial first row.
        else:
            stream.seek(offset)
        tail = stream.read(limit)
    for line in reversed(tail.split(b"\n")[:-1]):
        if not line.strip() or line.startswith(b"#"):
            continue
        row = next(csv.reader([line.decode()]))
        if len(row) != len(header):
            raise ValueError(f"Malformed complete CSV row: {path}")
        return dict(zip(header, row))
    return None


def read_macros(path, *, allow_incomplete_tail=False):
    """Return CSV rows as string dictionaries; convert only the fields you need."""
    with Path(path).open(newline="") as stream:
        reader = macro_reader(stream, allow_incomplete_tail=allow_incomplete_tail)
        if not reader.fieldnames or "load" not in reader.fieldnames:
            raise ValueError("Missing current macro CSV header/load column")
        if len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ValueError("Duplicate macro CSV columns")
        rows = []
        for row in reader:
            if next(iter(row.values()), "").startswith("#"):
                raise ValueError("Embedded CSV metadata/header changes are unsupported; inspect the run")
            if None in row or any(v is None for v in row.values()):
                raise ValueError("Malformed/incomplete macro CSV row")
            rows.append(row)
        return rows


def read_vtu(path):
    """Load a triangular current VTU. Install the [mesh] extra to use this."""
    import meshio
    mesh = meshio.read(path)
    return MeshData(mesh)


class MeshData:
    def __init__(self, mesh):
        if any(block.type != "triangle" for block in mesh.cells):
            raise ValueError("Expected triangular MTS2D mesh")
        self.mesh = mesh

    @property
    def cells(self):
        import numpy as np
        return np.concatenate([block.data for block in self.mesh.cells])

    def field(self, name):
        import numpy as np
        return np.concatenate(self.mesh.cell_data[name]).reshape(-1)

    def matrix(self, field):
        """T is total deformation; T_p is plastic. Missing components are errors."""
        import numpy as np
        components = []
        for suffix in ("11", "12", "21", "22"):
            components.append(self.field(field + suffix))
        matrix = np.stack(components, axis=-1).reshape(-1, 2, 2)
        if not np.all(np.isfinite(matrix)):
            raise ValueError(f"Non-finite {field} matrix")
        return matrix

    @property
    def T(self):
        return self.matrix("T")

    @property
    def T_p(self):
        return self.matrix("T_p")
