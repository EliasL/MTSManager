"""Read verified inputs for a fresh simulation template."""

from pathlib import Path

from .errors import ExpectedError
from .files import file_digest
from .initial_mesh import MAX_MESH_BYTES, MESH_FILE, mesh_checksum
from .run_records import read_run


def read_template(folder, selection):
    folder = Path(folder)
    manifest = folder / "manifest.json"
    config_file = folder / "config.conf"
    if (folder.is_symlink() or any(path.is_symlink() or not path.is_file()
                                   for path in (manifest, config_file)) or
            file_digest(manifest) != selection["manifest_sha256"]):
        raise ExpectedError("The source run changed. Refresh Monitor data and select it again.")
    config, _ = read_run(folder)
    if config.config_hash != selection["config_hash"]:
        raise ExpectedError("The source run has inconsistent saved settings. Refresh Monitor data.")
    settings = config.effective_dict()
    result = {"settings": settings, "label": folder.name.split("[", 1)[0]}
    if checksum := settings.pop("initialMeshSha256", ""):
        asset = folder / MESH_FILE
        if asset.is_symlink() or not asset.is_file():
            raise ExpectedError(f"Restore {MESH_FILE} in the source run to use it as a template.")
        with asset.open("rb") as stream:
            data = stream.read(MAX_MESH_BYTES + 1)
        try:
            mesh = data.decode("ascii")
        except UnicodeDecodeError as error:
            raise ExpectedError(f"Restore the original ASCII {MESH_FILE} in the source run.") from error
        if mesh_checksum(mesh) != checksum:
            raise ExpectedError(f"Restore {MESH_FILE} matching the source configuration.")
        result["initial_mesh"] = mesh
    return result
