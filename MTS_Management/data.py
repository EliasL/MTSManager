"""Strict current-format readers, independent of plotting and old field aliases."""

import csv
from pathlib import Path


def read_macros(path):
    """Return CSV rows as string dictionaries; convert only the fields you need."""
    with Path(path).open(newline="") as stream:
        reader = csv.DictReader(stream)
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

    def matrix(self, field):
        """T is total deformation; T_p is plastic. Missing components are errors."""
        import numpy as np
        components = []
        for suffix in ("11", "12", "21", "22"):
            components.append(np.concatenate(self.mesh.cell_data[field + suffix]).reshape(-1))
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
