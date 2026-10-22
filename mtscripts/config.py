"""Complete current MTS2D configurations and deterministic identities."""

from itertools import product
import json
import math
from pathlib import Path
import re
from types import MappingProxyType

from .files import digest

DEFAULTS = json.loads(Path(__file__).with_name("defaults.json").read_text())
SCHEMA_REVISION = "11100d124633ca9f766abd9f255e418eac1409d5"


class Config:
    def __init__(self, **settings):
        unknown = settings.keys() - DEFAULTS.keys()
        if unknown:
            raise ValueError(f"Unknown current MTS2D settings: {sorted(unknown)}")
        values = DEFAULTS | settings
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
        choices = {
            "stopWhen": {"all", "any"},
            "loading": {"relaxation", "reversibility", "noRelaxation"},
            "deformation": {"simpleShear", "directDisplacement"},
            "boundary": {"fixed", "periodic", "anchored", "periodicAnchored"},
            "reconnectionMethod": {"none", "edgeFlip", "delaunay"},
            "minimizer": {"LBFGS", "CG", "FIRE"},
            "preconditioner": {"none", "exactTangent", "diagonal"},
            "preconditionerUpdate": {"adaptive", "every", "frozen"},
            "energyFunction": {"contiSquare", "contiTriangular"},
            "meshDiagonal": {"major", "minor"},
        }
        for key, allowed in choices.items():
            if v[key] not in allowed:
                raise ValueError(f"{key} must be one of {sorted(allowed)}")
        if any(v[k] < 1 for k in ("rows", "cols", "nrThreads", "LBFGSNrCorrections")):
            raise ValueError("rows, cols, nrThreads and LBFGSNrCorrections must be positive")
        if v["loadIncrement"] == 0 or v["totalLoad"] < 0 or v["segmentLoad"] < 0:
            raise ValueError("Invalid loading budget/increment")
        directions = v["directions"].split(",")
        if any(d not in {"horizontal", "vertical", "+horizontal", "+vertical",
                         "-horizontal", "-vertical"} for d in directions):
            raise ValueError("Invalid directions")
        if (len(directions) > 1 or v["repeat"]) and v["segmentLoad"] <= 0:
            raise ValueError("Multiple/repeated directions require positive segmentLoad")
        if v["deformation"] == "directDisplacement" and v["boundary"] == "periodic":
            raise ValueError("directDisplacement requires driven fixed/anchored nodes")
        if v["startLoad"] < 0 or not math.isfinite(v["startLoad"] + v["totalLoad"]):
            raise ValueError("Invalid startLoad/totalLoad")
        if v["totalLoad"] > 0 and v["startLoad"] + v["totalLoad"] <= v["startLoad"]:
            raise ValueError("totalLoad is too small to advance startLoad")
        if v["stepNoise"] and v["loading"] != "relaxation":
            raise ValueError("stepNoise requires relaxation")
        if v["loading"] == "noRelaxation" and v["deformation"] == "simpleShear" and not v["affineInitialGuess"]:
            raise ValueError("noRelaxation shear requires affineInitialGuess")
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
        for k in ("checkpointInterval", "maximumSavedElasticReversibilityStates", "nrVTUFrames",
                  "LBFGSMaxIterations", "CGMaxIterations", "maxIt", "epsR", "QDSD",
                  "initialGuessNoise", "stepNoise", "reversibilityTolerance"):
            if v[k] < 0:
                raise ValueError(f"{k} must be nonnegative")

    def __getitem__(self, key):
        return self._values[key]

    def as_dict(self):
        return dict(self._values)

    @property
    def config_hash(self):
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
                         [f"{k} = {render(v)}" for k, v in sorted(self._values.items())]) + "\n"

    def write(self, path, label=None):
        Path(path).write_text(self.text(self.folder_name(label)))

    @classmethod
    def read(cls, path, *, expected_name=None):
        """Only complete new-format files are accepted; never fill missing settings."""
        values = {}
        for line in Path(path).read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            key, value = (s.strip() for s in line.split("=", 1))
            if key in values:
                raise ValueError(f"Duplicate setting: {key}")
            values[key] = value
        name = values.pop("name")
        if values.keys() != DEFAULTS.keys():
            raise ValueError(f"Incomplete/unknown settings: {sorted(values.keys() ^ DEFAULTS.keys())}")
        for key, default in DEFAULTS.items():
            if type(default) is bool:
                if values[key] not in ("true", "false"):
                    raise ValueError(f"{key} must be true or false")
                values[key] = values[key] == "true"
            else:
                values[key] = type(default)(values[key])
        result = cls(**values)
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
        result.append(Config(**(base.as_dict() | settings)))
    return result
