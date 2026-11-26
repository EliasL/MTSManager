"""Mesh preview generation and initial VTU input validation."""

from hashlib import sha256
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

from .errors import ExpectedError
from .files import file_digest

MESH_FILE = "initialMesh.vtu"
MAX_MESH_BYTES = 16 * 1024 * 1024


def mesh_bytes(text):
    if not isinstance(text, str):
        raise ExpectedError("Supply an ASCII VTU file for the initial mesh.")
    try:
        data = text.encode("ascii")
    except UnicodeEncodeError as error:
        raise ExpectedError("Supply an ASCII VTU file for the initial mesh.") from error
    if not data or len(data) > MAX_MESH_BYTES:
        raise ExpectedError("The initial VTU must contain data and be at most 16 MiB.")
    return data


def mesh_checksum(text):
    return sha256(mesh_bytes(text)).hexdigest()


def read_mesh(path):
    with Path(path).open("rb") as stream:
        data = stream.read(MAX_MESH_BYTES + 1)
    if len(data) > MAX_MESH_BYTES:
        raise ExpectedError("The initial VTU must be at most 16 MiB.")
    try:
        return data.decode("ascii")
    except UnicodeDecodeError as error:
        raise ExpectedError("Supply an ASCII VTU file for the initial mesh.") from error


def verify_mesh(folder, config):
    checksum = config["initialMeshSha256"]
    if not checksum:
        return
    path = Path(folder) / MESH_FILE
    if path.is_symlink() or not path.is_file() or file_digest(path) != checksum:
        raise ExpectedError(f"Restore {MESH_FILE} matching config.conf in {folder}.")


def preview(executable, config):
    if config["initialMeshSha256"]:
        raise ExpectedError("Generate the preview from the sample settings, then edit the mesh.")
    rows, cols = config["rows"], config["cols"]
    if config["boundary"] not in {"periodic", "periodicAnchored"}:
        rows, cols = rows - 1, cols - 1
    if not 0 < 2 * rows * cols <= 200_000:
        raise ExpectedError("Use sample dimensions that produce between 1 and 200,000 preview triangles.")
    if not Path(executable).is_file():
        raise ExpectedError("Build MTS2D on the preview site in Setup and tools.")
    with TemporaryDirectory(prefix="mts-mesh-preview-") as temporary:
        folder = Path(temporary)
        conf, vtu = folder / "config.conf", folder / "preview.vtu"
        config.write(conf, "MeshPreview")
        try:
            process = subprocess.run([executable, "-c", conf, "--preview-mesh", vtu],
                                     capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired as error:
            raise ExpectedError("Generating the mesh preview timed out. Try smaller sample dimensions.") from error
        if process.returncode:
            error = process.stderr.strip()
            if "unrecognized option" in error or "illegal option" in error:
                raise ExpectedError("Rebuild MTS2D on the preview site to enable the mesh preview.")
            if config["energyFunction"] == "conti2Phase" and "Invalid energy function: conti2Phase" in error:
                raise ExpectedError("Rebuild MTS2D on the preview site to use Conti2Phase.")
            if config["energyFunction"] == "triangularCubic" and "Invalid energy function: triangularCubic" in error:
                raise ExpectedError("Rebuild MTS2D on the preview site to use triangularCubic.")
            if "Unknown configuration key: conti_" in error:
                raise ExpectedError("Rebuild MTS2D on the preview site to use Conti material overrides.")
            if process.returncode < 0:
                raise RuntimeError(f"MTS2D preview exited with signal {-process.returncode}: {error}")
            raise ExpectedError(error or "MTS2D could not generate the mesh preview.")
        return vtu.read_text(encoding="ascii")
