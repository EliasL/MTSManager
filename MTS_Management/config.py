"""Complete current MTS2D configurations and deterministic identities."""

from itertools import product
import json
import math
from pathlib import Path
import re
from types import MappingProxyType

from .files import digest

DEFAULTS = json.loads(Path(__file__).with_name("defaults.json").read_text())
SCHEMA_REVISION = "23e61580faa0c3a4d6c0ca7d730dfbe00d1da95f"
RECTANGLE_EDGES = ("top", "bottom", "left", "right")
RECTANGLE_SETTINGS = {"drivenEdge", *RECTANGLE_EDGES}
INDENTATION_SETTINGS = {"indenterRadius", "indentationContact"}


class Config:
    def __init__(self, **settings):
        unknown = settings.keys() - DEFAULTS.keys()
        if unknown:
            raise ValueError(f"Unknown current MTS2D settings: {sorted(unknown)}")
        values = DEFAULTS | settings
        if values["deformation"] == "nanoindentation" and "boundary" not in settings:
            values["boundary"] = "fixedBottom"
        if values["deformation"] == "sequentialSlip" and isinstance(values["directions"], str):
            for key, value in (("segmentLoad", 1.0), ("totalLoad", values["directions"].count(",") + 1.0)):
                if key not in settings:
                    values[key] = value
        for key, default in DEFAULTS.items():
            value = values[key]
            if isinstance(default, float) and type(value) in (float, int):
                value = values[key] = float(value)
            if type(value) is not type(default):
                raise TypeError(f"{key} must be {type(default).__name__}")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"{key} must be finite")
            if isinstance(value, str) and any(c in value for c in "\r\n#="):
                raise ValueError(f"Invalid characters in {key}")
        self._validate(values)
        self._values = MappingProxyType(values)

    @staticmethod
    def _validate(v):
        if v["initialMeshSha256"]:
            if not re.fullmatch(r"[a-f0-9]{64}", v["initialMeshSha256"]):
                raise ValueError("initialMeshSha256 must be a lowercase SHA-256 checksum")
            if v["reconnectionMethod"] == "delaunay":
                raise ValueError("Use None or Edge flip reconnection for a custom initial mesh")
        choices = {
            "stopWhen": {"all", "any"},
            "loading": {"relaxation", "reversibility", "noRelaxation"},
            "deformation": {"simpleShear", "directDisplacement", "sequentialSlip", "compression", "tension",
                            "nanoindentation"},
            "boundary": {"fixed", "periodic", "anchored", "periodicAnchored", "rectangle", "fixedBottom"},
            "indentationContact": {"penalty", "projection"},
            "reconnectionMethod": {"none", "edgeFlip", "delaunay"},
            "minimizer": {"LBFGS", "CG", "FIRE"},
            "preconditioner": {"none", "exactTangent", "diagonal"},
            "tangentBackend": {"cholmod", "eigen"},
            "preconditionerUpdate": {"adaptive", "every", "frozen"},
            "energyFunction": {"contiSquare", "contiTriangular"},
            "constitutiveReference": {"areaNormalizedTriangular"},
            "meshDiagonal": {"major", "minor"},
        }
        for key, allowed in choices.items():
            if v[key] not in allowed:
                raise ValueError(f"{key} must be one of {sorted(allowed)}")
        if any(v[k] < 1 for k in ("rows", "cols", "nrThreads", "LBFGSNrCorrections")):
            raise ValueError("rows, cols, nrThreads and LBFGSNrCorrections must be positive")
        if v["loadIncrement"] == 0 or v["totalLoad"] < 0 or v["segmentLoad"] < 0:
            raise ValueError("Invalid loading budget/increment")
        rectangle = v["boundary"] == "rectangle"
        Config._validate_rectangle(v)
        Config._validate_indentation(v)
        directions = [] if rectangle else "".join(v["directions"].split()).split(",")
        if v["deformation"] == "sequentialSlip":
            if v["boundary"] != "anchored" or min(v["rows"], v["cols"]) < 2:
                raise ValueError("sequentialSlip requires an anchored mesh of at least 2 by 2 nodes")
            if (v["startLoad"] != 0 or v["segmentLoad"] != 1 or v["repeat"] or
                    v["totalLoad"] != len(directions) or v["loadIncrement"] <= 0 or
                    any(v[k] for k in ("stopAfterSteps", "stopAfterPlasticSteps", "stopAfterElasticSteps",
                                       "stopAfterEnergyDrops", "stopAfterEnergyIncreases"))):
                raise ValueError("sequentialSlip requires zero startLoad, positive loadIncrement, "
                                 "one unit per entry, and a full sequence without repetition or early stops")
            signs = {}
            for token in directions:
                match = re.fullmatch(r"([+-]?)([12])(?:\.([0-9]{1,9}))?", token)
                if not match:
                    raise ValueError(f"Invalid slip entry: {token}")
                sign, system, fraction = match.groups()
                if signs.setdefault(system, sign == "-") != (sign == "-"):
                    raise ValueError("Each slip system must use a single sign")
                size = v["rows" if system == "1" else "cols"]
                fraction = fraction or "5"
                if not 1 <= size * int(fraction) // 10**len(fraction) < size:
                    raise ValueError(f"Slip entry must select an interior split on the edge: {token}")
        elif any(d not in {"horizontal", "vertical", "+horizontal", "+vertical",
                           "-horizontal", "-vertical"} for d in directions):
            raise ValueError("Invalid directions")
        if (len(directions) > 1 or v["repeat"]) and v["segmentLoad"] <= 0:
            raise ValueError("Multiple/repeated directions require positive segmentLoad")
        if v["deformation"] == "directDisplacement" and v["boundary"] == "periodic":
            raise ValueError("directDisplacement requires driven fixed/anchored nodes")
        if not math.isfinite(v["startLoad"] + v["totalLoad"]):
            raise ValueError("Invalid startLoad/totalLoad")
        if v["totalLoad"] > 0 and v["startLoad"] + v["totalLoad"] <= v["startLoad"]:
            raise ValueError("totalLoad is too small to advance startLoad")
        if not rectangle and v["deformation"] in {"compression", "tension"} and (
                v["boundary"] not in {"periodic", "fixed"} or
                len(directions) != 1 or directions[0].startswith("-") or
                v["repeat"] or v["segmentLoad"] != 0 or v["loadIncrement"] <= 0 or v["startLoad"] < 0 or
                (v["deformation"] == "compression" and v["startLoad"] + v["totalLoad"] >= 1)):
            raise ValueError("Affine compression and tension require periodic or fixed boundaries, "
                             "one unsigned direction, and nonnegative engineering strain with positive stretch and increment")
        if v["reversibilityTolerance"] <= 0 or v["energyDropThreshold"] < 0:
            raise ValueError("Invalid reversibilityTolerance/energyDropThreshold")
        if any(v[k] < 0 for k in ("stopAfterSteps", "stopAfterPlasticSteps",
                                  "stopAfterElasticSteps", "stopAfterEnergyDrops",
                                  "stopAfterEnergyIncreases", "stepEnergyTolerance")):
            raise ValueError("Step targets and stepEnergyTolerance must be nonnegative")
        if (v["stopAfterPlasticSteps"] or v["stopAfterElasticSteps"]) and (
                v["loading"] != "relaxation" or v["deformation"] != "simpleShear" or
                v["energyFunction"] != "contiSquare"):
            raise ValueError("Plastic/elastic targets require relaxation, simpleShear and contiSquare")
        if v["preconditionerDisplacementThreshold"] <= 0:
            raise ValueError("preconditionerDisplacementThreshold must be positive")
        if v["forceReRun"]:
            raise ValueError("forceReRun is disabled; create a separate output root for reruns")
        if (v["vtuFilesPerUnitLoad"] < 0 or (v["writeMeshVTUs"] and v["vtuFilesPerUnitLoad"] == 0)
                or not math.isfinite(v["vtuFilesPerUnitLoad"] * v["totalLoad"])):
            raise ValueError("VTU files per unit load must be finite and positive when mesh output is enabled")
        for k in ("checkpointInterval", "maximumSavedElasticReversibilityStates",
                  "LBFGSMaxIterations", "CGMaxIterations", "maxIt", "epsR", "QDSD",
                  "initialGuessNoise", "stepNoise", "reversibilityTolerance"):
            if v[k] < 0:
                raise ValueError(f"{k} must be nonnegative")

    @staticmethod
    def _validate_rectangle(v):
        if v["boundary"] != "rectangle":
            if any(v[key] != DEFAULTS[key] for key in RECTANGLE_SETTINGS):
                raise ValueError("Edge settings require boundary = rectangle")
            return
        if v["deformation"] not in {"compression", "tension"} or min(v["rows"], v["cols"]) < 2:
            raise ValueError("Rectangle boundary displacement requires compression or tension "
                             "on a mesh of at least 2 by 2 nodes")
        if v["reconnectionMethod"] == "delaunay":
            raise ValueError("Delaunay reconnection requires periodic boundaries. "
                             "Use edgeFlip or none for a rectangle")
        driven = v["drivenEdge"]
        if driven not in RECTANGLE_EDGES:
            raise ValueError("drivenEdge must be top, bottom, left, or right")
        for edge in RECTANGLE_EDGES:
            if v[edge] not in {"free", "fixed_x", "fixed_y", "fixed"}:
                raise ValueError(f"{edge} must be free, fixed_x, fixed_y, or fixed")
        vertical = driven in {"top", "bottom"}
        triangular = v["energyFunction"] == "contiTriangular"
        if triangular and not vertical:
            raise ValueError("Choose the top or bottom driven edge for a triangular sample")
        adjacent = ("left", "right") if vertical else ("top", "bottom")
        normal_constraint = "fixed_y" if vertical else "fixed_x"
        for edge in adjacent:
            if v[edge] in {"fixed", normal_constraint}:
                raise ValueError(f"{edge} fixes the driven component at a shared corner")
        if v["repeat"] or v["segmentLoad"] != 0 or v["loadIncrement"] <= 0 or v["startLoad"] < 0:
            raise ValueError("Boundary displacement requires a nonnegative start, a positive "
                             "increment, and no loading segments or repetition")
        span = (v["rows" if vertical else "cols"] - 1) * (math.sqrt(3) / 2 if triangular else 1)
        if v["deformation"] == "compression" and v["startLoad"] + v["totalLoad"] >= span:
            raise ValueError("Final compression displacement must be less than "
                             "the initial distance between loaded edges")

    @staticmethod
    def _validate_indentation(v):
        if v["deformation"] != "nanoindentation":
            if v["boundary"] == "fixedBottom" or any(v[key] != DEFAULTS[key] for key in INDENTATION_SETTINGS):
                raise ValueError("Indenter settings and fixedBottom require nanoindentation")
            return
        if (v["boundary"] != "fixedBottom" or min(v["rows"], v["cols"]) < 2 or
                v["loading"] != "relaxation" or v["reconnectionMethod"] == "delaunay" or
                v["segmentLoad"] != 0 or v["repeat"]):
            raise ValueError("Nanoindentation requires bottom fixation, relaxation, at least two rows and "
                             "columns, and an unsegmented loading path")
        spacing = math.sqrt(3) / 2 if v["energyFunction"] == "contiTriangular" else 1
        if (v["indenterRadius"] <= 0 or v["loadIncrement"] <= 0 or v["startLoad"] < 0 or
                v["startLoad"] + v["totalLoad"] >= (v["rows"] - 1) * spacing):
            raise ValueError("Nanoindentation requires a positive radius and increment, a nonnegative "
                             "initial depth, and a final depth below the sample height")

    def __getitem__(self, key):
        return self._values[key]

    def as_dict(self):
        if hasattr(self, "_original"):
            return dict(self._original)
        excluded = self.inactive_settings(self._values)
        return {key: value for key, value in self._values.items() if key not in excluded}

    @staticmethod
    def inactive_settings(values):
        excluded = set()
        if not values.get("initialMeshSha256"):
            excluded.add("initialMeshSha256")
        if values.get("boundary") != "rectangle":
            excluded.update(RECTANGLE_SETTINGS)
        if values.get("deformation") != "nanoindentation":
            excluded.update(INDENTATION_SETTINGS)
        if values.get("energyFunction") != "contiTriangular":
            excluded.add("constitutiveReference")
        return excluded

    @property
    def config_hash(self):
        # This saved identity format is independent of the Python package's name.
        return digest({"format": "mtscripts-config-v1", "settings": self.as_dict()})

    def folder_name(self, label=None):
        dimensions = str(self['rows']) if self['rows'] == self['cols'] else f"{self['rows']}x{self['cols']}"
        label = label or f"{self['deformation']}L{dimensions}s{self['seed']}"
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", label):
            raise ValueError("Label must be 1–120 ASCII letters, digits, '.', '_' or '-'")
        return f"{label}[{self.config_hash[:20]}]"

    def text(self, name):
        if name != self.folder_name(name.split("[")[0]):
            raise ValueError("Configuration name must contain its current hash")
        def render(value):
            return str(value).lower() if isinstance(value, bool) else str(value)
        return "\n".join([f"name = {name}"] +
                         [f"{k} = {render(v)}" for k, v in sorted(self.as_dict().items())]) + "\n"

    def write(self, path, label=None):
        Path(path).write_text(self.text(self.folder_name(label)))

    @classmethod
    def saved(cls, values, supplement=None):
        """Resolve absent campaign settings without changing the saved identity."""
        missing = DEFAULTS.keys() - values.keys() - cls.inactive_settings(values)
        if supplement is None:
            supplement = {}
        if missing and not supplement:
            from .campaign import supplemental_settings
            supplement = supplemental_settings(values)
        if missing - supplement.keys() or values.keys() - DEFAULTS.keys():
            raise ValueError(f"Incomplete/unknown settings: {sorted((missing - supplement.keys()) | (values.keys() - DEFAULTS.keys()))}")
        result = cls(**(supplement | values))
        result._original = {k: result[k] for k in values}
        return result

    @classmethod
    def parse_settings(cls, text):
        """Parse native configuration text without assigning a run identity."""
        values = {}
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            key, value = (s.strip() for s in line.split("=", 1))
            if key in values:
                raise ValueError(f"Duplicate setting: {key}")
            values[key] = value
        name = values.pop("name", None)
        for key in values:
            if key not in DEFAULTS:
                raise ValueError(f"Unknown setting: {key}")
            default = DEFAULTS[key]
            if type(default) is bool:
                if values[key].lower() not in ("true", "false", "1", "0"):
                    raise ValueError(f"{key} must be true, false, 1, or 0")
                values[key] = values[key].lower() in ("true", "1")
            else:
                values[key] = type(default)(values[key])
        return name, values

    @classmethod
    def read(cls, path, *, expected_name=None):
        """Read original settings, resolving omissions through campaign metadata."""
        name, values = cls.parse_settings(Path(path).read_text())
        result = cls.saved(values)
        if name is None:
            raise ValueError("Missing saved configuration name")
        if name != result.folder_name(name.split("[")[0]):
            raise ValueError("Configuration name/hash mismatch")
        if expected_name is not None and name != expected_name:
            raise ValueError("Saved configuration name does not match the output folder")
        return result


def sweep(base=None, **axes):
    """Cartesian product. The convenience axis L sets both rows and cols."""
    base = base or Config()
    if "L" in axes and ({"rows", "cols"} & axes.keys()):
        raise ValueError("Use either L or rows/cols sweep axes")
    result = []
    for values in product(*axes.values()):
        settings = dict(zip(axes, values))
        if "L" in settings:
            size = settings.pop("L")
            settings.update(rows=size, cols=size)
        values = base.as_dict() | settings
        if values["deformation"] == "sequentialSlip" and "directions" in settings and "totalLoad" not in settings:
            values.pop("totalLoad")
        result.append(Config(**values))
    return result
